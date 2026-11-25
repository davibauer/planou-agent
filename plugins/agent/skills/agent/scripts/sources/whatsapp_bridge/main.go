// Command whatsapp-bridge is the READ-ONLY WhatsApp bridge of the agent plugin's whatsapp source (sources/whatsapp.py).
//
// It joins the account as a linked device (like WhatsApp Web) and only ever receives. There is no code path that sends
// anything a person sees: no message, reaction, edit, status, read receipt, typing state or presence. It never calls
// SendPresence, so the account is never shown online through this device and the delivery receipts whatsmeow has to
// send stay of type "inactive" (the protocol acks the server needs, not rendered as read in the apps). Read receipts
// (MarkRead) are never sent. tests/test_whatsapp_source.py scans this folder and fails if any send call shows up.
//
// Two commands, each one connection that ends:
//
//	pair  shows the QR (terminal and a PNG), pairs, keeps the one-time history of the allowed chats, disconnects
//	pull  connects, receives what the server queued since the last connection, disconnects
//
// The allowlist and limits come on stdin as JSON (Options); only messages of allowed chats are written, one JSON line
// each, to the spool file (0600), before the server gets the ack (SynchronousAck). A message the handler could not
// decide or write is left unacked and whatsmeow keeps its decrypted copy in session.db (EnableDecryptedEventBuffer)
// until a later run handles it: the server delivers it again and, without that copy, the Signal state would have moved
// on and the redelivery would be dropped. A handler that panics counts as not decided too (whatsmeow would ack it).
// A group message from a group missing from the joined-groups listing, while a listed name has no group yet, asks the
// server for that one group's info (GetGroupInfo, a query like GetJoinedGroups) to decide it in the same run.
// Attachments of allowed chats are downloaded to -media up to the size limit.
// The result is one JSON object on stdout; logs go to stderr at error level.
// Exit codes: 0 ok, 1 failure, 2 usage, 3 not paired, 4 session closed on the phone, 5 already paired.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
	"rsc.io/qr"
)

// record is one line of the spool, read by sources/whatsapp.py.
type record struct {
	ID       string `json:"id"`
	Chat     string `json:"chat"`
	ChatPN   string `json:"chat_pn,omitempty"`
	ChatLID  string `json:"chat_lid,omitempty"` // a 1:1 chat addressed by its number: its @lid, when known (same person)
	ChatNome string `json:"chat_nome"`
	Grupo    bool   `json:"grupo"`
	De       string `json:"de"`
	DeNome   string `json:"de_nome"`
	Eu       bool   `json:"eu"`
	Quando   string `json:"quando"`
	Texto    string `json:"texto"`
	Anexo    *anexo `json:"anexo,omitempty"`
	Origem   string `json:"origem"`
	Recebido string `json:"recebido"` // when this bridge wrote it: the source's per-chat cursor
	// mode "all": why a chat outside the list was written ("novo" or "sinal") and which signals it carried
	Candidata string   `json:"candidata,omitempty"`
	Sinais    []string `json:"sinais,omitempty"`
}

type anexo struct {
	Tipo    string `json:"tipo"`
	Nome    string `json:"nome,omitempty"`
	Mime    string `json:"mime,omitempty"`
	Bytes   int64  `json:"bytes"`
	Caminho string `json:"caminho,omitempty"`
	Erro    string `json:"erro,omitempty"`
}

type summary struct {
	OK          bool     `json:"ok"`
	Erro        string   `json:"erro,omitempty"`
	Gravadas    int      `json:"gravadas"`
	Descartadas int      `json:"descartadas"`
	Anexos      int      `json:"anexos"`
	Historico   int      `json:"historico"`
	Indecididas int      `json:"indecididas"`
	Ignoradas   int      `json:"ignoradas,omitempty"`              // mode "all": dropped by the ignore list
	Candidatas  int      `json:"candidatas,omitempty"`             // mode "all": written as candidates
	Conhecidos  []string `json:"conhecidos,omitempty"`             // mode "all", first run: keyed hashes of the chats known now
	BaseOK      bool     `json:"linha_de_base_completa,omitempty"` // mode "all", first run: groups and address book both complete
	SemNumero   int      `json:"sem_numero,omitempty"`             // mode "all": 1:1 @lid chats with no known number, dropped
	SemLID      int      `json:"sem_lid,omitempty"`                // mode "all": 1:1 chats by number with no known @lid, dropped
	SemPar      []string `json:"sem_par"`
	Grupos      []string `json:"grupos,omitempty"`
	Avisos      []string `json:"avisos"`
}

func emit(s summary) {
	if s.SemPar == nil {
		s.SemPar = []string{}
	}
	if s.Avisos == nil {
		s.Avisos = []string{}
	}
	_ = json.NewEncoder(os.Stdout).Encode(s)
}

func fail(code int, msg string) {
	emit(summary{OK: false, Erro: msg})
	os.Exit(code)
}

// stderrLog is the whatsmeow logger: errors and warnings only, one short line each, on stderr.
type stderrLog struct{ module string }

func (l stderrLog) Errorf(msg string, args ...any) { l.line("ERROR", msg, args...) }
func (l stderrLog) Warnf(msg string, args ...any)  {}
func (l stderrLog) Infof(msg string, args ...any)  {}
func (l stderrLog) Debugf(msg string, args ...any) {}
func (l stderrLog) Sub(m string) waLog.Logger      { return stderrLog{l.module + "/" + m} }
func (l stderrLog) line(level, msg string, args ...any) {
	s := fmt.Sprintf(msg, args...)
	if len(s) > 300 {
		s = s[:300]
	}
	fmt.Fprintf(os.Stderr, "%s [%s] %s\n", level, l.module, s)
}

// session opens the device store under dir (0700, files 0600) holding an exclusive lock, so a tick and a pairing never
// connect at the same time (two connections of one device replace each other's stream).
func session(ctx context.Context, dir string) (*sqlstore.Container, *store.Device, func(), error) {
	if dir == "" {
		return nil, nil, nil, errors.New("-session faltando")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, nil, nil, err
	}
	_ = os.Chmod(dir, 0o700)
	lock, err := os.OpenFile(filepath.Join(dir, "session.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, nil, nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, nil, nil, errors.New("outra conexao do WhatsApp desta instancia em andamento (tick ou pareamento)")
	}
	db := filepath.Join(dir, "session.db")
	if f, err := os.OpenFile(db, os.O_CREATE|os.O_RDWR, 0o600); err == nil {
		f.Close()
	}
	c, err := sqlstore.New(ctx, "sqlite3", dsn(db), stderrLog{"store"})
	if err != nil {
		lock.Close()
		return nil, nil, nil, fmt.Errorf("sessao: %v", err)
	}
	dev, err := c.GetFirstDevice(ctx)
	if err != nil {
		c.Close()
		lock.Close()
		return nil, nil, nil, fmt.Errorf("sessao: %v", err)
	}
	closer := func() {
		c.Close()
		for _, suf := range []string{"", "-wal", "-shm", "-journal"} {
			_ = os.Chmod(db+suf, 0o600)
		}
		lock.Close()
	}
	return c, dev, closer, nil
}

// dsn of the session store. secure_delete overwrites what sqlite deletes: the event buffer keeps a message's decrypted
// text only until the handler is done with it (also for chats outside the list, decrypted before the filter), and that
// text must not stay behind in free pages of session.db.
func dsn(db string) string {
	return "file:" + db + "?_foreign_keys=on&_busy_timeout=5000&_secure_delete=on"
}

// handlerGrace: after the pull's deadline, how long a handler already running (an attachment download, a history
// block) still has to finish before the bridge disconnects anyway. The source waits timeout+30 s for the process.
const handlerGrace = 20 * time.Second

// reader receives events and writes the allowed messages to the spool.
type reader struct {
	cli   *whatsmeow.Client
	opts  Options
	match *matcher
	spool string
	media string
	cache string // allowed group subjects -> JID from earlier runs (session folder, 0600)

	groupInfo func(context.Context, types.JID) (*types.GroupInfo, error) // cli.GetGroupInfo (a query); tests swap it
	lookupMu  sync.Mutex                                                 // one group info query at a time
	saveMu    sync.Mutex                                                 // one writer of the cache file at a time

	mu          sync.Mutex
	known       map[string]string // the cache, loaded at start and refreshed when the groups are listed
	readyWait   time.Duration
	groups      map[string]string // joined groups: JID -> subject
	groupsKnown bool
	gone        map[string]bool // groups the server says the account is not in (or that no longer exist): this run
	lookupFail  map[string]bool // groups whose info query failed in this run: not asked again until the next run
	homonyms    bool            // a listed subject is shared by more than one group: the pairing shows the groups with their ids
	ready       chan struct{}
	readyOnce   sync.Once
	last        time.Time
	inflight    int       // handlers running (events.Message); the pull waits for them before disconnecting
	closing     bool      // set when the pull leaves: new events are left unacked (and buffered) for the next run
	cutoff      time.Time // pull: downloads end by here; zero in the pairing
	warned      map[string]bool
	sum         summary
	fatal       chan string
	fatalCode   int
	offline     chan struct{}
	offlineOnce sync.Once
	history     time.Time
	all         *allMode        // mode "all" (todos.go); nil = only the allowlist
	seen        map[string]bool // mode "all", first run: keyed hashes of the chats seen in this run
	noNumberIDs map[string]bool // mode "all": the @lid ids of the 1:1 chats dropped for having no known number
	contactsOK  bool            // the address book (app state critical_unblock_low) finished a full sync in this run
}

func newReader(cli *whatsmeow.Client, o Options, sessionDir, spool, media string, readyWait time.Duration) *reader {
	r := &reader{cli: cli, opts: o, match: newMatcher(o), spool: spool, media: media, groups: map[string]string{},
		cache: filepath.Join(sessionDir, "grupos-permitidos.json"), known: map[string]string{}, readyWait: readyWait,
		ready: make(chan struct{}), fatal: make(chan string, 4), offline: make(chan struct{}), last: time.Now(),
		warned: map[string]bool{}, gone: map[string]bool{}, lookupFail: map[string]bool{},
		all: newAllMode(o), seen: map[string]bool{}, noNumberIDs: map[string]bool{}}
	r.groupInfo = cli.GetGroupInfo
	if b, err := os.ReadFile(r.cache); err == nil {
		_ = json.Unmarshal(b, &r.known)
	}
	return r
}

func (r *reader) loadGroups(ctx context.Context) {
	defer r.readyOnce.Do(func() { close(r.ready) })
	if !r.cli.WaitForConnection(20 * time.Second) {
		r.warn("sem conexao para ler os nomes dos grupos; so as entradas por id valem neste tick")
		return
	}
	var gs []*types.GroupInfo
	var err error
	for try := 0; try < 3; try++ {
		if gs, err = r.cli.GetJoinedGroups(ctx); err == nil {
			break
		}
		time.Sleep(3 * time.Second)
	}
	if err != nil {
		r.warn("nomes dos grupos indisponiveis (" + short(err.Error()) + "); valem os ids e os grupos ja resolvidos antes")
		return
	}
	r.mu.Lock()
	for _, g := range gs {
		r.groups[g.JID.String()] = g.Name
	}
	known, warns := r.match.resolve(r.groups, r.known) // a name resolved before keeps its group; one off the list leaves
	r.known, r.groupsKnown = known, true
	r.mu.Unlock()
	r.saveKnown()
	r.resolveWarnings(warns)
}

// resolveWarnings reports what resolve() left out (homonyms); the pairing then shows the groups with their ids.
func (r *reader) resolveWarnings(warns []string) {
	for _, w := range warns {
		r.warn(w)
	}
	if len(warns) > 0 {
		r.mu.Lock()
		r.homonyms = true
		r.mu.Unlock()
	}
}

// saveKnown writes the resolved names (r.known) to the cache file. The listing and a group info query in a handler can
// both change it, so the write is serialized and atomic (temporary file, then rename).
func (r *reader) saveKnown() {
	r.saveMu.Lock()
	defer r.saveMu.Unlock()
	r.mu.Lock()
	b, err := json.Marshal(r.known)
	r.mu.Unlock()
	if err != nil {
		return
	}
	tmp := r.cache + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		_ = os.Remove(tmp)
		return
	}
	_ = os.Rename(tmp, r.cache)
}

// lookupGroup decides a message of a group missing from the joined-groups listing while a listed name has no group yet
// (decideGroup said "not decided"). It asks the server for that one group's info (GetGroupInfo, a query: nothing is
// sent to a person) and decides with the same rules as the listing: the group joins r.groups for the rest of the run,
// resolve() binds a listed name only when no other known group has it, and a new binding goes to the cache file. When
// the query fails the message stays undecided (unacked, as before) with a warning, and the group is not asked again in
// this run. When the server answers that the account is not in the group (or that it no longer exists), its name
// cannot be known: the message is dropped with a warning naming the group's id (to keep it, list the id).
func (r *reader) lookupGroup(chat types.JID) groupDecision {
	jid := chat.String()
	r.lookupMu.Lock() // a second message of the same group waits here and then finds the answer of the first one
	defer r.lookupMu.Unlock()
	r.mu.Lock()
	subject, listed := r.groups[jid]
	gone, failed := r.gone[jid], r.lookupFail[jid]
	r.mu.Unlock()
	switch {
	case gone:
		return groupDecision{Decided: true}
	case failed:
		return groupDecision{}
	case !listed:
		ctx, cancel, _ := r.opCtx(15 * time.Second)
		gi, err := r.groupInfo(ctx, chat)
		cancel()
		switch {
		case err == nil && gi != nil:
			r.mu.Lock()
			r.groups[jid] = gi.Name
			known, warns := r.match.resolve(r.groups, r.known)
			r.known = known
			r.mu.Unlock()
			r.saveKnown()
			r.resolveWarnings(warns)
			subject = gi.Name
		case errors.Is(err, whatsmeow.ErrNotInGroup) || errors.Is(err, whatsmeow.ErrGroupNotFound):
			r.mu.Lock()
			r.gone[jid] = true
			r.mu.Unlock()
			r.warn(fmt.Sprintf("grupo %s fora da lista de grupos da conta e o servidor nao da o nome dele (a conta nao "+
				"participa ou o grupo nao existe mais): mensagens descartadas; para ler esse grupo, ponha o id dele na lista", jid))
			return groupDecision{Decided: true}
		default:
			msg := "sem resposta"
			if err != nil {
				msg = short(err.Error())
			}
			r.mu.Lock()
			r.lookupFail[jid] = true
			r.mu.Unlock()
			r.warn(fmt.Sprintf("nome do grupo %s indisponivel (%s): as mensagens dele ficam sem decisao, guardadas na "+
				"sessao sem confirmacao, e voltam na proxima conexao", jid, msg))
			return groupDecision{}
		}
	}
	r.mu.Lock()
	cache := r.known
	r.mu.Unlock()
	return r.match.decideGroup(jid, subject, true, cache)
}

// groupsKnownNow: the joined groups were listed in this run.
func (r *reader) groupsKnownNow() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.groupsKnown
}

// warn adds a warning to the summary once (a homonym group would otherwise repeat it for every message).
func (r *reader) warn(s string) {
	r.mu.Lock()
	if !r.warned[s] {
		r.warned[s] = true
		r.sum.Avisos = append(r.sum.Avisos, s)
	}
	r.mu.Unlock()
}

func short(s string) string {
	if len(s) > 120 {
		return s[:120]
	}
	return s
}

func (r *reader) touch() {
	r.mu.Lock()
	r.last = time.Now()
	r.mu.Unlock()
}

// begin counts a handler in; false once the pull is leaving (the event is then left unacked for the next run).
func (r *reader) begin() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return false
	}
	r.inflight++
	r.last = time.Now()
	return true
}

func (r *reader) end() {
	r.mu.Lock()
	r.inflight--
	r.last = time.Now()
	r.mu.Unlock()
}

// activity: handlers running and the time since the last event started or ended.
func (r *reader) activity() (int, time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.inflight, time.Since(r.last)
}

// drain waits for the handlers in progress before the caller disconnects, so a message already decrypted finishes,
// is written and acked in this run. Until deadline it also waits for quiet without events (the last messages of the
// queue); after deadline only for the handlers running, up to hard. Then it closes: later events are left unacked.
// Returns a fatal message when one arrives meanwhile.
func (r *reader) drain(deadline, hard time.Time, quiet time.Duration) (string, bool) {
	for {
		closed, running := r.closeIfDone(time.Now(), deadline, hard, quiet)
		if closed {
			if running > 0 {
				r.warn(fmt.Sprintf("%d mensagens ainda em processamento no fim do prazo: ficam guardadas na sessao sem "+
					"confirmacao e voltam no proximo tick", running))
			}
			return "", false
		}
		select {
		case msg := <-r.fatal:
			return msg, true
		case <-time.After(250 * time.Millisecond):
		}
	}
}

// closeIfDone decides whether the pull may leave and, when it may, sets closing in the same critical section: a
// begin() can no longer slip in between the check and the flag and run during the Disconnect. running is how many
// handlers were still in progress when the hard limit closed it anyway.
func (r *reader) closeIfDone(now, deadline, hard time.Time, quiet time.Duration) (closed bool, running int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	n, idle := r.inflight, now.Sub(r.last)
	if (n == 0 && (idle >= quiet || now.After(deadline))) || now.After(hard) {
		r.closing = true
		return true, n
	}
	return false, n
}

// opCtx is the context of one network operation inside a handler: at most max, and never past the pull's cutoff.
// cut says the cutoff is what bounds it (a timeout then means "no time left in this run", not a failed download).
func (r *reader) opCtx(max time.Duration) (context.Context, context.CancelFunc, bool) {
	end := time.Now().Add(max)
	if !r.cutoff.IsZero() && r.cutoff.Before(end) {
		ctx, cancel := context.WithDeadline(context.Background(), r.cutoff)
		return ctx, cancel, true
	}
	ctx, cancel := context.WithDeadline(context.Background(), end)
	return ctx, cancel, false
}

func (r *reader) stop(code int, msg string) {
	r.mu.Lock()
	if r.fatalCode == 0 {
		r.fatalCode = code
	}
	r.mu.Unlock()
	select {
	case r.fatal <- msg:
	default:
	}
}

// handle runs for every event before whatsmeow acks it (SynchronousAck). It returns false when a message could not be
// decided or written: whatsmeow then sends no ack and keeps the decrypted copy in session.db (EnableDecryptedEventBuffer),
// so the server's redelivery on the next connection reaches this handler again. The history comes as a notification
// message downloaded here, inside the handler (ManualHistorySyncDownload): a block that fails is left unacked the same
// way and is not deleted from the server, so the redelivered notification downloads it again.
//
// A panic inside it is recovered here and also returns false: whatsmeow's own recover would count it as success and ack
// the event, losing the message. The warning names only the event type (a panic value could carry message text).
func (r *reader) handle(evt any) (ok bool) {
	defer func() {
		if p := recover(); p != nil {
			ok = false
			r.warn(fmt.Sprintf("falha interna ao tratar um evento (%T): sem confirmacao, volta na proxima conexao", evt))
		}
	}()
	switch v := evt.(type) {
	case *events.Message:
		if !r.begin() {
			return false
		}
		defer r.end()
		if n := v.Message.GetProtocolMessage().GetHistorySyncNotification(); n != nil {
			if !v.Info.IsFromMe {
				return true
			}
			return r.historyNotification(n)
		}
		return r.message(v, "tick")
	case *events.AppStateSyncComplete:
		if v.Name == appstate.WAPatchCriticalUnblockLow { // the address book: the known set of mode "all" needs it whole
			r.mu.Lock()
			r.contactsOK = true
			r.mu.Unlock()
		}
	case *events.OfflineSyncCompleted:
		r.touch()
		r.offlineOnce.Do(func() { close(r.offline) })
	case *events.LoggedOut:
		r.stop(4, "sessao encerrada no celular (aparelho desconectado); pareie de novo")
	case *events.StreamReplaced:
		r.stop(1, "outra conexao com a mesma sessao tomou o lugar desta")
	case *events.TemporaryBan:
		r.stop(1, "conta com bloqueio temporario do WhatsApp: "+v.String())
	case *events.ClientOutdated:
		r.stop(1, "versao do cliente recusada pelo WhatsApp; atualize a whatsmeow fixada")
	case *events.ConnectFailure:
		r.stop(1, fmt.Sprintf("falha ao conectar (%d %s)", v.Reason, v.Message))
	}
	return true
}

// historyNotification downloads one history block sent by the phone and writes its allowed messages. Only after all of
// them are written is the block deleted from the media server, as whatsmeow does by default (the phone uploaded it
// for this device only).
func (r *reader) historyNotification(n *waE2E.HistorySyncNotification) bool {
	r.mu.Lock()
	r.history = time.Now()
	r.mu.Unlock()
	ctx, cancel, _ := r.opCtx(2 * time.Minute)
	defer cancel()
	blob, err := r.cli.DownloadHistorySync(ctx, n, true)
	if err != nil {
		r.warn("um bloco do historico nao baixou (" + short(err.Error()) + "); sem confirmacao, volta na proxima conexao")
		return false
	}
	failed := false
	for _, conv := range blob.GetConversations() {
		chat, err := types.ParseJID(conv.GetID())
		if err != nil {
			continue
		}
		for _, hm := range conv.GetMessages() {
			m, err := r.cli.ParseWebMessage(chat, hm.GetMessage())
			if err != nil {
				continue
			}
			if !r.message(m, "historico") {
				failed = true
			}
		}
	}
	if failed {
		return false
	}
	_ = r.cli.DeleteMedia(ctx, whatsmeow.MediaHistory, n.GetDirectPath(), n.GetFileEncSHA256(), n.GetEncHandle())
	return true
}

// contact is what the account's store knows about a person (empty when nothing).
func (r *reader) contact(ctx context.Context, jid types.JID) types.ContactInfo {
	if jid.IsEmpty() {
		return types.ContactInfo{}
	}
	c, err := r.cli.Store.Contacts.GetContact(ctx, jid)
	if err != nil {
		return types.ContactInfo{}
	}
	return c
}

func first(xs ...string) string {
	for _, x := range xs {
		if strings.TrimSpace(x) != "" {
			return x
		}
	}
	return ""
}

// message writes an allowed message to the spool. false = not decided (group names unknown, or the info query of a
// group missing from the listing failed), attachment cut by the
// end of the run, or not written: the caller leaves it unacked.
func (r *reader) message(evt *events.Message, origin string) bool {
	ctx := context.Background()
	select { // the group names come right after connecting; a tick waits at most readyWait for them
	case <-r.ready:
	case <-time.After(r.readyWait):
	}
	info := evt.Info
	chat := info.Chat.ToNonAD()
	rec := record{ID: info.ID, Chat: chat.String(), Eu: info.IsFromMe, Quando: info.Timestamp.UTC().Format(time.RFC3339), Origem: origin}
	allowed := false
	var ids, users, names []string // mode "all": every address of the chat, its phone numbers, address book names
	noNumber := false              // a 1:1 chat known only by its @lid: no phone number to check the ignore list with
	noLID := false                 // a 1:1 chat by number whose @lid is not known: no @lid to check the ignore list with
	switch chat.Server {
	case types.GroupServer:
		r.mu.Lock()
		subject, listed := r.groups[chat.String()]
		known, cache := r.groupsKnown && listed, r.known // a group joined after the listing is not known yet
		r.mu.Unlock()
		d := r.match.decideGroup(chat.String(), subject, known, cache)
		r.mu.Lock()
		ask := !d.Decided && r.groupsKnown // the listing came and this group is not in it; when it did not come, wait
		r.mu.Unlock()
		if ask {
			d = r.lookupGroup(chat)
		}
		if d.Warn != "" {
			r.warn(d.Warn)
		}
		if !d.Decided {
			r.mu.Lock()
			r.sum.Indecididas++
			r.mu.Unlock()
			return false
		}
		allowed = d.Allowed
		r.mu.Lock()
		subject, listed = r.groups[chat.String()] // a group info query above may have found it
		r.mu.Unlock()
		rec.Grupo, rec.ChatNome = true, first(d.Name, subject, chat.User) // the listed name, so the source's check agrees
		ids = []string{chat.String()}
		if r.all != nil {
			ignored, decided := r.all.ignoredGroup(chat.String(), subject, listed)
			if !decided && !listed && r.groupsKnownNow() {
				r.lookupGroup(chat) // a query, like the allowlist's: the ignore list names groups by subject
				r.mu.Lock()
				subject, listed = r.groups[chat.String()]
				gone := r.gone[chat.String()]
				r.mu.Unlock()
				ignored, decided = r.all.ignoredGroup(chat.String(), subject, listed)
				if !decided && gone { // the server gives no subject: fail closed, never read
					ignored, decided = true, true
				}
			}
			if !decided {
				r.mu.Lock()
				r.sum.Indecididas++
				r.mu.Unlock()
				return false
			}
			if ignored {
				r.mu.Lock()
				r.sum.Ignoradas++
				r.mu.Unlock()
				return true
			}
		}
	case types.DefaultUserServer, types.HiddenUserServer:
		pn, lid := chat, types.JID{}
		if chat.Server == types.DefaultUserServer { // the same person's @lid, so an @lid in the lists also covers this chat
			if alt := info.SenderAlt; !info.IsFromMe && alt.Server == types.HiddenUserServer {
				lid = alt.ToNonAD()
			} else if alt := info.RecipientAlt; info.IsFromMe && alt.Server == types.HiddenUserServer {
				lid = alt.ToNonAD()
			} else if l, err := r.cli.Store.LIDs.GetLIDForPN(ctx, chat); err == nil && l.Server == types.HiddenUserServer {
				lid = l.ToNonAD()
			}
		}
		if chat.Server == types.HiddenUserServer {
			if alt := info.SenderAlt; !info.IsFromMe && alt.Server == types.DefaultUserServer {
				pn = alt.ToNonAD()
			} else if alt := info.RecipientAlt; info.IsFromMe && alt.Server == types.DefaultUserServer {
				pn = alt.ToNonAD()
			} else if p, err := r.cli.Store.LIDs.GetPNForLID(ctx, chat); err == nil && !p.IsEmpty() {
				pn = p.ToNonAD()
			}
		}
		users = []string{}
		if pn.Server == types.DefaultUserServer {
			users = append(users, pn.User)
			rec.ChatPN = pn.String()
		} else {
			noNumber = true
		}
		ids = []string{chat.String(), pn.String()}
		if !lid.IsEmpty() {
			ids = append(ids, lid.String())
			rec.ChatLID = lid.String()
		} else if chat.Server == types.DefaultUserServer {
			noLID = true
		}
		c := r.contact(ctx, pn)
		names = agendaNames(c)
		allowed, rec.ChatNome = r.match.directChat(ids, users, c, pn.User)
		if r.all != nil && r.all.ignoredDirect(ids, users, names) {
			r.mu.Lock()
			r.sum.Ignoradas++
			r.mu.Unlock()
			return true
		}
	default: // status, channel, broadcast: never read, in any mode
		ids = nil
	}
	if r.all != nil && ids != nil {
		if r.all.baseline {
			r.mu.Lock()
			for _, id := range ids {
				if id != "" {
					r.seen[hashID(r.all.salt, id)] = true
				}
			}
			r.mu.Unlock()
		}
		old := origin == "historico" && r.opts.HistoryDays > 0 &&
			time.Since(info.Timestamp) > time.Duration(r.opts.HistoryDays)*24*time.Hour
		if old || (!allowed && anyIn(r.all.muted, ids)) { // history past the limit, or a muted chat: never written
			r.mu.Lock()
			r.sum.Descartadas++
			r.mu.Unlock()
			return true
		}
	}
	// mode "all": a 1:1 chat with no phone number (an @lid without SenderAlt/RecipientAlt or a stored mapping) cannot be
	// checked against the numbers and address book names of the ignore list (its ids were checked above), so with such
	// entries it is never a candidate: dropped and acked, nothing written, nothing kept in the session for later. Leaving
	// it undecided would keep it decrypted in session.db for good (and hold a whole history block). The tick lists the
	// dropped @lid ids (only the ids), so one can be put in contatos (read) or in ignorar (silenced).
	if r.all != nil && noNumber && !allowed && r.all.ignoreNeedsNumber() {
		r.mu.Lock()
		r.sum.Descartadas++
		r.sum.SemNumero++
		r.noNumberIDs[chat.String()] = true
		r.mu.Unlock()
		return true
	}
	// mode "all", the mirror case: a 1:1 chat by number whose @lid is not known (no SenderAlt/RecipientAlt, no stored
	// mapping) cannot be checked against the @lid ids of the ignore list, which are the ids the candidates block and the
	// warning show, so one is put there without knowing the number. With such entries it is never a candidate: dropped
	// and acked, nothing written. The tick says only how many (never the numbers).
	if r.all != nil && noLID && !allowed && r.all.ignoreHasLID() {
		r.mu.Lock()
		r.sum.Descartadas++
		r.sum.SemLID++
		r.mu.Unlock()
		return true
	}
	candidate := r.all != nil && ids != nil && !allowed
	if !allowed && !candidate { // not in the allowlist (or a status, channel, broadcast): dropped before anything is written
		r.mu.Lock()
		r.sum.Descartadas++
		r.mu.Unlock()
		return true
	}
	text, att, ok := flatten(evt.Message)
	if !ok {
		return true
	}
	rec.Texto = text
	sender := info.Sender.ToNonAD()
	rec.De = sender.String()
	if info.IsFromMe {
		rec.DeNome = "eu"
	} else { // a label only: the address book name first, then the name the sender chose
		alt := info.SenderAlt.ToNonAD()
		senderNames := append(agendaNames(r.contact(ctx, sender)), agendaNames(r.contact(ctx, alt))...)
		rec.DeNome = first(append(senderNames, info.PushName, alt.User, sender.User)...)
	}
	if candidate { // mode "all", a chat outside the list: written only with a signal of the objective or when new
		file := ""
		if att != nil {
			file = att.Name
		}
		phones := append([]string{}, users...)
		if s := info.Sender.ToNonAD(); s.Server == types.DefaultUserServer {
			phones = append(phones, s.User)
		}
		if a := info.SenderAlt.ToNonAD(); a.Server == types.DefaultUserServer {
			phones = append(phones, a.User)
		}
		subject := ""
		if rec.Grupo {
			subject = rec.ChatNome
		}
		hits := r.all.signals.hits(text, subject, []string{rec.DeNome, info.PushName}, file, phones)
		switch {
		case len(hits) > 0:
			rec.Candidata, rec.Sinais = "sinal", hits
		case r.all.isNew(ids):
			rec.Candidata = "novo"
		default: // no signal, not new: nothing is written, not even the attachment
			r.mu.Lock()
			r.sum.Descartadas++
			r.mu.Unlock()
			return true
		}
		if att != nil { // a candidate's attachment is listed, never downloaded: only a relevant chat's are
			rec.Anexo = &anexo{Tipo: att.Kind, Nome: att.Name, Mime: att.Mime, Bytes: att.Size,
				Erro: "candidata: anexo so e baixado de chat julgado relevante"}
			att = nil
		}
	}
	if att != nil {
		a, retry := r.download(rec, att, info.Timestamp, origin)
		if retry {
			r.warn("anexo sem tempo para terminar de baixar neste tick; a mensagem fica sem confirmacao e volta no proximo")
			return false
		}
		rec.Anexo = a
	}
	rec.Recebido = time.Now().UTC().Format(recebidoLayout)
	if err := r.write(rec); err != nil {
		r.stop(1, "spool: "+short(err.Error()))
		return false
	}
	r.mu.Lock()
	r.sum.Gravadas++
	if rec.Candidata != "" {
		r.sum.Candidatas++
	}
	if origin == "historico" {
		r.sum.Historico++
	}
	r.mu.Unlock()
	return true
}

// maxNoNumberIDs caps the @lid ids listed in the warning about 1:1 chats with no known number.
const maxNoNumberIDs = 10

// recebidoLayout is fixed width (nanoseconds always present), so the per-chat cursors also sort as text.
const recebidoLayout = "2006-01-02T15:04:05.000000000Z07:00"

// download saves an attachment of an allowed chat. retry=true when the pull's cutoff ended it: the message is then left
// unacked and comes again next run, instead of being written without its file.
func (r *reader) download(rec record, att *media, when time.Time, origin string) (*anexo, bool) {
	a := &anexo{Tipo: att.Kind, Nome: att.Name, Mime: att.Mime, Bytes: att.Size}
	tooBig := fmt.Sprintf("maior que o limite de %d MB", r.opts.MaxMediaBytes/(1024*1024))
	switch {
	case !r.opts.Media || r.media == "":
		a.Erro = "download de anexos desligado"
		return a, false
	case r.opts.MaxMediaBytes > 0 && att.Size > r.opts.MaxMediaBytes:
		a.Erro = tooBig
		return a, false
	case origin == "historico" && time.Since(when) > 7*24*time.Hour:
		a.Erro = "anexo do historico com mais de 7 dias (nao baixado)"
		return a, false
	}
	dm, ok := att.Msg.(whatsmeow.DownloadableMessage)
	if !ok {
		a.Erro = "anexo sem dados de download"
		return a, false
	}
	dir := filepath.Join(r.media, chatDir(rec.Chat))
	path := filepath.Join(dir, fileName(when.UTC().Format("2006-01-02"), rec.ID, att))
	if _, err := os.Stat(path); err == nil { // only complete files get this name (renamed from .part)
		a.Caminho = path
		return a, false
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		a.Erro = short(err.Error())
		return a, false
	}
	part := path + ".part"
	f, err := os.OpenFile(part, os.O_CREATE|os.O_TRUNC|os.O_RDWR, 0o600)
	if err != nil {
		a.Erro = short(err.Error())
		return a, false
	}
	limit := int64(0)
	if r.opts.MaxMediaBytes > 0 { // the declared size is the sender's word: the file itself is capped while it is written
		limit = r.opts.MaxMediaBytes + 64 // room for the padding and the MAC of the encrypted file
	}
	ctx, cancel, cut := r.opCtx(60 * time.Second)
	defer cancel()
	err = r.cli.DownloadToFile(ctx, dm, cappedFile{f, limit})
	var size int64
	if err == nil {
		if st, serr := f.Stat(); serr == nil {
			size = st.Size()
		}
	}
	f.Close()
	if err == nil && r.opts.MaxMediaBytes > 0 && size > r.opts.MaxMediaBytes {
		err = errTooBig
	}
	if err == nil {
		err = os.Rename(part, path)
	}
	if err != nil {
		_ = os.Remove(part)
		switch {
		case cut && ctx.Err() != nil:
			return a, true
		case errors.Is(err, errTooBig):
			a.Erro = tooBig
		default:
			a.Erro = "download falhou: " + short(err.Error())
		}
		return a, false
	}
	a.Caminho, a.Bytes = path, size
	r.mu.Lock()
	r.sum.Anexos++
	r.mu.Unlock()
	return a, false
}

func (r *reader) write(rec record) error {
	line, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := os.MkdirAll(filepath.Dir(r.spool), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(r.spool, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return err
	}
	return f.Sync() // on disk before the handler returns and the server gets the ack
}

func (r *reader) finish(includeGroups bool) summary {
	r.mu.Lock()
	defer r.mu.Unlock()
	var contacts map[string]bool
	if all, err := r.cli.Store.Contacts.GetAllContacts(context.Background()); err == nil && len(all) > 0 {
		contacts = map[string]bool{}
		for _, c := range all {
			for _, n := range agendaNames(c) { // the same names the list is compared with
				if n != "" {
					contacts[n] = true
				}
			}
		}
	}
	s := r.sum
	s.OK = true
	if s.Indecididas > 0 {
		s.Avisos = append(s.Avisos, fmt.Sprintf("%d msgs de grupos ficaram sem decisao (nomes dos grupos indisponiveis); "+
			"guardadas na sessao sem confirmacao ao servidor, voltam na proxima conexao", s.Indecididas))
	}
	if r.all != nil && s.SemNumero > 0 {
		lids := make([]string, 0, len(r.noNumberIDs))
		for id := range r.noNumberIDs {
			lids = append(lids, id)
		}
		sort.Strings(lids)
		more := ""
		if len(lids) > maxNoNumberIDs {
			more = fmt.Sprintf(" e mais %d", len(lids)-maxNoNumberIDs)
			lids = lids[:maxNoNumberIDs]
		}
		s.Avisos = append(s.Avisos, fmt.Sprintf("%d msgs de chat 1:1 sem numero conhecido (so o id @lid) descartadas sem "+
			"gravar: com numeros ou nomes em `ignorar` nao da para conferir se sao de alguem ignorado; para acompanhar um "+
			"desses chats, ponha o id em contatos (ou em ignorar, para calar o aviso): %s%s", s.SemNumero,
			strings.Join(lids, ", "), more))
	}
	if r.all != nil && s.SemLID > 0 {
		s.Avisos = append(s.Avisos, fmt.Sprintf("%d msgs de chat 1:1 pelo numero sem o id @lid conhecido descartadas sem "+
			"gravar: com ids @lid em `ignorar` nao da para conferir se sao de alguem ignorado; para acompanhar um desses "+
			"chats, ponha o numero em contatos (ou em ignorar, para calar o aviso)", s.SemLID))
	}
	if r.all != nil && r.all.baseline { // the chats known now: the address book, the joined groups, the chats seen
		seen := map[string]bool{}
		for h := range r.seen {
			seen[h] = true
		}
		ctx := context.Background()
		all, err := r.cli.Store.Contacts.GetAllContacts(ctx)
		if err == nil {
			for jid := range all {
				seen[hashID(r.all.salt, jid.String())] = true
			}
		}
		// complete only with the joined groups listed and the address book synced whole; otherwise the source keeps the
		// hashes as partial and the next run is a baseline again (nothing is new until then). The pairing needs the full
		// sync event of this run; a pull finds the synced version in the store.
		synced := r.contactsOK
		if !includeGroups {
			if v, _, verr := r.cli.Store.AppState.GetAppStateVersion(ctx, string(appstate.WAPatchCriticalUnblockLow)); verr == nil && v > 0 {
				synced = true
			}
		}
		missing := []string{}
		if !r.groupsKnown {
			missing = append(missing, "grupos da conta nao listados")
		}
		if err != nil || !synced {
			missing = append(missing, "agenda ainda nao sincronizada")
		}
		s.BaseOK = len(missing) == 0
		if !s.BaseOK {
			s.Avisos = append(s.Avisos, "linha de base do modo todos incompleta ("+strings.Join(missing, "; ")+"): o proximo "+
				"tick continua como linha de base e, ate completar, nenhum chat vira contato novo (so os sinais contam)")
		}
		for jid := range r.groups {
			seen[hashID(r.all.salt, jid)] = true
		}
		s.Conhecidos = make([]string, 0, len(seen))
		for h := range seen {
			s.Conhecidos = append(s.Conhecidos, h)
		}
		sort.Strings(s.Conhecidos)
	}
	s.SemPar = r.match.unmatched(r.groups, contacts, r.groupsKnown)
	if includeGroups && (len(s.SemPar) > 0 || r.homonyms) { // pairing only: to the terminal to fix the list, never to disk
		for jid, name := range r.groups {
			s.Grupos = append(s.Grupos, fmt.Sprintf("%s (%s)", name, jid))
		}
	}
	return s
}

func newClient(dev *store.Device, autoReconnect bool) *whatsmeow.Client {
	cli := whatsmeow.NewClient(dev, stderrLog{"client"})
	cli.EnableAutoReconnect = autoReconnect
	cli.AutomaticMessageRerequestFromPhone = false // would send a request to the phone; undecryptable messages are skipped
	cli.SynchronousAck = true                      // ack only after the handler wrote the message to the spool
	cli.EnableDecryptedEventBuffer = true          // a message left unacked stays decrypted in session.db for the next run
	cli.ManualHistorySyncDownload = true           // history blocks are downloaded inside the handler, under the same rule
	return cli
}

func pull(o Options, sessionDir, spool, mediaDir string, timeout time.Duration) {
	ctx := context.Background()
	_, dev, closeStore, err := session(ctx, sessionDir)
	if err != nil {
		fail(1, err.Error())
	}
	defer closeStore()
	if dev.ID == nil { // never Connect() an unpaired store: that would start a new registration
		closeStore()
		fail(3, "nao pareado")
	}
	cli := newClient(dev, false)
	r := newReader(cli, o, sessionDir, spool, mediaDir, 20*time.Second)
	deadline := time.Now().Add(timeout)
	r.cutoff = deadline.Add(handlerGrace)
	cli.AddEventHandlerWithSuccessStatus(r.handle)
	abort := func(msg string) {
		cli.Disconnect()
		closeStore()
		fail(r.fatalCode, msg)
	}
	if err := cli.Connect(); err != nil {
		closeStore()
		fail(1, "conexao: "+short(err.Error()))
	}
	go r.loadGroups(ctx)
	select {
	case msg := <-r.fatal:
		abort(msg)
	case <-r.offline: // the server finished sending what was queued
	case <-time.After(time.Until(deadline)):
		r.warn("tempo esgotado antes do fim da fila; o que nao recebeu confirmacao volta no proximo tick")
	}
	// the last messages of the queue and the handlers still running (a download, a history block) finish before leaving
	if msg, bad := r.drain(deadline, r.cutoff, 3*time.Second); bad {
		abort(msg)
	}
	if r.all != nil && r.all.baseline { // the known set needs the group listing: wait for it, within the run's limit
		select {
		case <-r.ready:
		case <-time.After(time.Until(r.cutoff)):
		}
	}
	cli.Disconnect()
	emit(r.finish(false))
}

func pair(o Options, sessionDir, spool, mediaDir, qrPNG string, timeout, historyWait time.Duration) {
	ctx := context.Background()
	_, dev, closeStore, err := session(ctx, sessionDir)
	if err != nil {
		fail(1, err.Error())
	}
	defer closeStore()
	if dev.ID != nil {
		closeStore()
		fail(5, "ja pareado (para parear de novo: remova o aparelho no celular e apague a pasta da sessao)")
	}
	store.SetOSInfo("Agent (somente leitura)", [3]uint32{0, 1, 0})
	days := o.HistoryDays
	if days <= 0 {
		days = 30
	}
	store.DeviceProps.HistorySyncConfig.FullSyncDaysLimit = proto.Uint32(uint32(days))
	if o.Mode == "all" { // every chat's history would come: ask the phone for the short window only (and the handler cuts too)
		store.DeviceProps.HistorySyncConfig.RecentSyncDaysLimit = proto.Uint32(uint32(days))
	}
	store.DeviceProps.RequireFullSync = proto.Bool(false)
	cli := newClient(dev, true)                                      // the server restarts the stream right after pairing; reconnect only during pairing
	r := newReader(cli, o, sessionDir, spool, mediaDir, historyWait) // pairing: the one-time history waits for the names
	cli.AddEventHandlerWithSuccessStatus(r.handle)
	dropQR := func() {
		if qrPNG != "" {
			_ = os.Remove(qrPNG)
		}
	}
	abort := func(code int, msg string) { // fail() exits, so the QR image is removed here and not in a defer
		cli.Disconnect()
		closeStore()
		dropQR()
		fail(code, msg)
	}
	qctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	qrc, err := cli.GetQRChannel(qctx)
	if err != nil {
		abort(1, "qr: "+short(err.Error()))
	}
	if err := cli.Connect(); err != nil {
		abort(1, "conexao: "+short(err.Error()))
	}
	paired := false
	for item := range qrc {
		switch {
		case item.Event == "code":
			if qrPNG != "" {
				if code, err := qr.Encode(item.Code, qr.L); err == nil {
					_ = os.MkdirAll(filepath.Dir(qrPNG), 0o700)
					_ = os.WriteFile(qrPNG, code.PNG(), 0o600)
				}
			}
			fmt.Fprintf(os.Stderr, "\nWhatsApp > Aparelhos conectados > Conectar um aparelho, e escaneie (vale %ds; a imagem tambem esta em %s):\n\n",
				int(item.Timeout.Seconds()), qrPNG)
			qrterminal.GenerateHalfBlock(item.Code, qrterminal.L, os.Stderr)
		case item == whatsmeow.QRChannelSuccess:
			paired = true
		case item == whatsmeow.QRChannelTimeout:
			abort(1, "o QR expirou sem leitura; rode o pareamento de novo")
		case item.Error != nil:
			abort(1, "pareamento: "+short(item.Error.Error()))
		default:
			abort(1, "pareamento: "+item.Event)
		}
	}
	dropQR()
	if !paired {
		abort(1, "pareamento nao concluido")
	}
	fmt.Fprintln(os.Stderr, "\nPareado. Recebendo o historico dos chats permitidos (uma vez so)...")
	go r.loadGroups(ctx)
	// history: wait for the first batch (up to historyWait) and leave 30 s after the last one
	end := time.After(historyWait)
	for done := false; !done; {
		select {
		case msg := <-r.fatal:
			abort(r.fatalCode, msg)
		case <-end:
			done = true
		case <-time.After(time.Second):
			r.mu.Lock()
			h := r.history
			r.mu.Unlock()
			if n, idle := r.activity(); !h.IsZero() && n == 0 && idle > 30*time.Second {
				done = true
			}
		}
	}
	now := time.Now() // a history block still being written finishes before leaving
	if msg, bad := r.drain(now, now.Add(2*time.Minute), 0); bad {
		abort(r.fatalCode, msg)
	}
	cli.Disconnect()
	emit(r.finish(true))
}

func main() {
	if len(os.Args) < 2 || (os.Args[1] != "pair" && os.Args[1] != "pull") {
		fmt.Fprintln(os.Stderr, "uso: whatsapp-bridge pair|pull -session DIR -spool ARQ [-media DIR] [-qr-png ARQ] [-timeout 60s] < opcoes.json")
		os.Exit(2)
	}
	cmd := os.Args[1]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	sessionDir := fs.String("session", "", "pasta da sessao (secrets/ da instancia)")
	spool := fs.String("spool", "", "arquivo JSONL onde as mensagens permitidas sao gravadas")
	mediaDir := fs.String("media", "", "pasta dos anexos (cache/ da instancia)")
	qrPNG := fs.String("qr-png", "", "pair: onde salvar o QR em PNG")
	timeout := fs.Duration("timeout", 60*time.Second, "tempo maximo da conexao (pull) ou do QR (pair)")
	historyWait := fs.Duration("history-wait", 180*time.Second, "pair: espera maxima pelo historico")
	_ = fs.Parse(os.Args[2:])
	var o Options
	if err := json.NewDecoder(os.Stdin).Decode(&o); err != nil {
		fail(2, "opcoes em JSON no stdin: "+short(err.Error()))
	}
	if *spool == "" {
		fail(2, "-spool faltando")
	}
	if o.Mode != "" && o.Mode != "all" {
		fail(2, "modo desconhecido: "+short(o.Mode))
	}
	if o.Mode != "all" && newMatcher(o).empty() {
		fail(2, "lista de permissao vazia (chats e contatos): nada a ler")
	}
	if cmd == "pair" {
		pair(o, *sessionDir, *spool, *mediaDir, *qrPNG, *timeout, *historyWait)
		return
	}
	pull(o, *sessionDir, *spool, *mediaDir, *timeout)
}
