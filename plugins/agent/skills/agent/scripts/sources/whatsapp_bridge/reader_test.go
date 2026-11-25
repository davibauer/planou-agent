package main

// Handler tests over a real session store (sqlite in a temp folder), never connected. Fake ids and names only.

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAdv"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

var (
	stranger = types.JID{User: "550000000101", Server: types.DefaultUserServer}
	friend   = types.JID{User: "550000000102", Server: types.DefaultUserServer}
)

func testReader(t *testing.T, o Options) (*reader, *store.Device) {
	t.Helper()
	dir := t.TempDir()
	ctx := context.Background()
	c, err := sqlstore.New(ctx, "sqlite3", dsn(filepath.Join(dir, "session.db")), waLog.Noop)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	dev := c.NewDevice()
	dev.ID = &types.JID{User: "550000000099", Server: types.DefaultUserServer}
	dev.Account = &waAdv.ADVSignedDeviceIdentity{Details: []byte{0}, AccountSignature: make([]byte, 64),
		AccountSignatureKey: make([]byte, 32), DeviceSignature: make([]byte, 64)}
	if err := c.PutDevice(ctx, dev); err != nil {
		t.Fatal(err)
	}
	r := newReader(newClient(dev, false), o, dir, filepath.Join(dir, "spool.jsonl"), filepath.Join(dir, "media"), 10*time.Millisecond)
	return r, dev
}

func msg(chat, sender types.JID, id, push, text string) *events.Message {
	return &events.Message{
		Info: types.MessageInfo{MessageSource: types.MessageSource{Chat: chat, Sender: sender, IsGroup: chat.Server == types.GroupServer},
			ID: id, PushName: push, Timestamp: time.Now()},
		Message: &waE2E.Message{Conversation: proto.String(text)},
	}
}

func spool(t *testing.T, r *reader) []record {
	t.Helper()
	f, err := os.Open(r.spool)
	if os.IsNotExist(err) {
		return nil
	} else if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []record
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var rec record
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatal(err)
		}
		out = append(out, rec)
	}
	return out
}

func TestStrangerWithAListedPushNameStaysOut(t *testing.T) {
	r, dev := testReader(t, Options{Contatos: []string{"Fulano Teste"}})
	ctx := context.Background()
	// whatsmeow fills these two from the messages the stranger sends: both are the stranger's choice
	if _, _, err := dev.Contacts.PutPushName(ctx, stranger, "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := dev.Contacts.PutBusinessName(ctx, stranger, "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	if !r.handle(msg(stranger, stranger, "MSGTEST1", "Fulano Teste", "oi")) {
		t.Fatal("a dropped message is decided (acked)")
	}
	if got := spool(t, r); len(got) != 0 {
		t.Fatalf("stranger written to the spool: %+v", got)
	}
	if r.sum.Descartadas != 1 {
		t.Fatalf("descartadas = %d", r.sum.Descartadas)
	}
}

func TestSavedContactIsReadByTheAddressBookName(t *testing.T) {
	r, dev := testReader(t, Options{Contatos: []string{"Fulano Teste"}})
	if err := dev.Contacts.PutContactName(context.Background(), friend, "Fulano", "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	if !r.handle(msg(friend, friend, "MSGTEST2", "Apelido Qualquer", "segue o orcamento")) {
		t.Fatal("written message must be acked")
	}
	got := spool(t, r)
	if len(got) != 1 || got[0].ChatNome != "Fulano Teste" || got[0].DeNome != "Fulano Teste" || got[0].Texto != "segue o orcamento" {
		t.Fatalf("spool = %+v", got)
	}
	if len(got[0].Recebido) != len("2026-01-05T09:01:00.000000000Z") {
		t.Fatalf("recebido must be fixed width: %q", got[0].Recebido)
	}
}

func TestUndecidedGroupMessageIsLeftUnacked(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	g := types.JID{User: "120363000000000002", Server: types.GroupServer}
	if r.handle(msg(g, friend, "MSGTEST3", "", "chegou o cimento")) {
		t.Fatal("group names unknown and the listed name not resolved: no ack (kept decrypted for the next run)")
	}
	if got := spool(t, r); len(got) != 0 || r.sum.Indecididas != 1 {
		t.Fatalf("spool = %+v indecididas = %d", got, r.sum.Indecididas)
	}
	if !r.cli.SynchronousAck || !r.cli.EnableDecryptedEventBuffer || !r.cli.ManualHistorySyncDownload {
		t.Fatal("unacked messages must stay decrypted in the session (buffer) and history must be handled in the handler")
	}
}

func TestHomonymGroupStaysOutAfterTheNameIsResolved(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	a := types.JID{User: "120363000000000001", Server: types.GroupServer}
	c := types.JID{User: "120363000000000003", Server: types.GroupServer}
	r.groups = map[string]string{a.String(): "Obra Exemplo"}
	r.known, _ = r.match.resolve(r.groups, nil)
	r.groups[c.String()] = "Obra Exemplo" // created later by someone else, account added
	r.known, _ = r.match.resolve(r.groups, r.known)
	r.groupsKnown = true
	close(r.ready)
	if !r.handle(msg(c, stranger, "MSGTEST4", "", "golpe")) || !r.handle(msg(c, stranger, "MSGTEST5", "", "golpe")) {
		t.Fatal("the homonym is decided (dropped)")
	}
	if !r.handle(msg(a, friend, "MSGTEST6", "", "chegou o cimento")) {
		t.Fatal("the resolved group is written")
	}
	got := spool(t, r)
	if len(got) != 1 || got[0].Chat != a.String() || got[0].ChatNome != "Obra Exemplo" {
		t.Fatalf("spool = %+v", got)
	}
	if n := len(r.sum.Avisos); n != 1 || !strings.Contains(r.sum.Avisos[0], c.String()) {
		t.Fatalf("one warning naming the homonym, not one per message: %v", r.sum.Avisos)
	}
}

func TestPullWaitsForHandlersInProgressThenLeavesTheRestUnacked(t *testing.T) {
	r, dev := testReader(t, Options{Contatos: []string{"Fulano Teste"}})
	if !r.begin() {
		t.Fatal("begin")
	}
	go func() { time.Sleep(300 * time.Millisecond); r.end() }() // a download still running when the queue ends
	start := time.Now()
	if _, bad := r.drain(start, start.Add(5*time.Second), 0); bad {
		t.Fatal("no fatal expected")
	}
	if time.Since(start) < 300*time.Millisecond {
		t.Fatal("drain returned before the handler in progress finished")
	}
	if err := dev.Contacts.PutContactName(context.Background(), friend, "Fulano", "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	if r.handle(msg(friend, friend, "MSGTEST7", "", "tarde demais")) {
		t.Fatal("after the drain, a new event is left unacked for the next run")
	}
	if len(spool(t, r)) != 0 {
		t.Fatal("nothing written after the drain")
	}

	r2, _ := testReader(t, Options{Contatos: []string{"Fulano Teste"}})
	r2.begin() // never ends: the hard limit wins and says so
	now := time.Now()
	r2.drain(now, now.Add(300*time.Millisecond), 0)
	if len(r2.sum.Avisos) != 1 || !strings.Contains(r2.sum.Avisos[0], "voltam no proximo tick") {
		t.Fatalf("avisos = %v", r2.sum.Avisos)
	}
}

func TestSessionStoreSecureDeleteAndEventBuffer(t *testing.T) {
	db := filepath.Join(t.TempDir(), "session.db")
	conn, err := sql.Open("sqlite3", dsn(db))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	var on int
	if err := conn.QueryRow("PRAGMA secure_delete").Scan(&on); err != nil || on != 1 {
		t.Fatalf("secure_delete = %d (%v)", on, err)
	}
	_, dev := testReader(t, Options{Chats: []string{"x@g.us"}})
	ctx := context.Background()
	var h [32]byte
	h[0] = 1
	if err := dev.EventBuffer.PutBufferedEvent(ctx, h, []byte("texto"), time.Now()); err != nil {
		t.Fatal(err)
	}
	if b, err := dev.EventBuffer.GetBufferedEvent(ctx, h); err != nil || b == nil || string(b.Plaintext) != "texto" {
		t.Fatalf("event buffer: %+v %v", b, err)
	}
}

// listed: the joined groups came in this run (with the given JID -> subject) and r.known resolved from them.
func listed(r *reader, groups map[string]string) {
	r.groups = groups
	r.known, _ = r.match.resolve(r.groups, nil)
	r.groupsKnown = true
	close(r.ready)
}

// fakeInfo answers the group info query from subjects (or err) and counts the calls; it never connects.
func fakeInfo(r *reader, subjects map[string]string, err error) *atomic.Int32 {
	var calls atomic.Int32
	r.groupInfo = func(_ context.Context, jid types.JID) (*types.GroupInfo, error) {
		calls.Add(1)
		if err != nil {
			return nil, err
		}
		return &types.GroupInfo{JID: jid, GroupName: types.GroupName{Name: subjects[jid.String()]}}, nil
	}
	return &calls
}

func savedCache(t *testing.T, r *reader) map[string]string {
	t.Helper()
	b, err := os.ReadFile(r.cache)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]string{}
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

var (
	grpA = types.JID{User: "120363000000000011", Server: types.GroupServer}
	grpB = types.JID{User: "120363000000000012", Server: types.GroupServer}
	grpC = types.JID{User: "120363000000000013", Server: types.GroupServer}
)

func TestGroupMissingFromTheListingIsDecidedByItsInfo(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo", "Outro Grupo"}})
	listed(r, map[string]string{grpA.String(): "Outro Grupo"}) // "Obra Exemplo" has no group yet
	calls := fakeInfo(r, map[string]string{grpB.String(): "Obra Exemplo"}, nil)
	if !r.handle(msg(grpB, friend, "MSGTEST10", "", "chegou o cimento")) {
		t.Fatal("the group's info has the listed name: decided and written in this run")
	}
	if !r.handle(msg(grpB, friend, "MSGTEST11", "", "e a areia")) {
		t.Fatal("second message of the group")
	}
	got := spool(t, r)
	if len(got) != 2 || got[0].Chat != grpB.String() || got[0].ChatNome != "Obra Exemplo" || !got[0].Grupo {
		t.Fatalf("spool = %+v", got)
	}
	if n := calls.Load(); n != 1 {
		t.Fatalf("one query per group per run, got %d", n)
	}
	if c := savedCache(t, r); c["Obra Exemplo"] != grpB.String() || c["Outro Grupo"] != grpA.String() {
		t.Fatalf("the binding goes to the cache file: %v", c)
	}
	if r.sum.Indecididas != 0 {
		t.Fatalf("indecididas = %d", r.sum.Indecididas)
	}
}

func TestGroupMissingFromTheListingWithAnotherNameIsDropped(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	listed(r, map[string]string{grpA.String(): "Familia Teste"})
	calls := fakeInfo(r, map[string]string{grpB.String(): "Futebol Teste"}, nil)
	for i := 0; i < 2; i++ {
		if !r.handle(msg(grpB, stranger, fmt.Sprintf("MSGTEST2%d", i), "", "jogo hoje")) {
			t.Fatal("a group outside the list is decided (dropped and acked), not left in the buffer")
		}
	}
	if len(spool(t, r)) != 0 || r.sum.Descartadas != 2 || r.sum.Indecididas != 0 || calls.Load() != 1 {
		t.Fatalf("descartadas = %d indecididas = %d calls = %d", r.sum.Descartadas, r.sum.Indecididas, calls.Load())
	}
	if _, err := os.Stat(r.cache + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("no temporary file left behind")
	}
}

func TestGroupInfoFailureKeepsTheMessageUndecidedWithOneWarning(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	listed(r, map[string]string{grpA.String(): "Familia Teste"})
	calls := fakeInfo(r, nil, errors.New("fake timeout"))
	for i := 0; i < 2; i++ {
		if r.handle(msg(grpB, friend, fmt.Sprintf("MSGTEST3%d", i), "", "chegou o cimento")) {
			t.Fatal("query failed: no ack, the message comes back next run (as before)")
		}
	}
	if len(spool(t, r)) != 0 || r.sum.Indecididas != 2 || calls.Load() != 1 {
		t.Fatalf("indecididas = %d calls = %d", r.sum.Indecididas, calls.Load())
	}
	if len(r.sum.Avisos) != 1 || !strings.Contains(r.sum.Avisos[0], grpB.String()) {
		t.Fatalf("one warning naming the group: %v", r.sum.Avisos)
	}
}

func TestGroupInfoNotInGroupDropsWithAWarning(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	listed(r, map[string]string{grpA.String(): "Familia Teste"})
	calls := fakeInfo(r, nil, fmt.Errorf("wrapped: %w", whatsmeow.ErrNotInGroup))
	for i := 0; i < 2; i++ {
		if !r.handle(msg(grpB, friend, fmt.Sprintf("MSGTEST4%d", i), "", "historico antigo")) {
			t.Fatal("the server says the account is not in the group: decided (its name cannot be known)")
		}
	}
	if len(spool(t, r)) != 0 || r.sum.Descartadas != 2 || calls.Load() != 1 {
		t.Fatalf("descartadas = %d calls = %d", r.sum.Descartadas, calls.Load())
	}
	if len(r.sum.Avisos) != 1 || !strings.Contains(r.sum.Avisos[0], "ponha o id") {
		t.Fatalf("avisos = %v", r.sum.Avisos)
	}
}

func TestGroupInfoHomonymOfAnUnresolvedNameStaysOut(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	// two joined groups already share the listed name: none was resolved
	listed(r, map[string]string{grpA.String(): "Obra Exemplo", grpB.String(): "Obra Exemplo"})
	calls := fakeInfo(r, map[string]string{grpC.String(): "Obra Exemplo"}, nil)
	if !r.handle(msg(grpC, stranger, "MSGTEST50", "", "golpe")) {
		t.Fatal("a third group with the same name is decided (dropped)")
	}
	if len(spool(t, r)) != 0 || calls.Load() != 1 {
		t.Fatalf("spool = %+v calls = %d", spool(t, r), calls.Load())
	}
	r.mu.Lock()
	bound := r.known["Obra Exemplo"]
	r.mu.Unlock()
	if bound != "" {
		t.Fatalf("the name must stay unresolved, got %s", bound)
	}
}

func TestGroupInfoIsNotAskedWhenTheListingDidNotCome(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	calls := fakeInfo(r, map[string]string{grpB.String(): "Obra Exemplo"}, nil)
	if r.handle(msg(grpB, friend, "MSGTEST60", "", "chegou o cimento")) {
		t.Fatal("without the listing the other groups are unknown (homonyms): stays undecided, as before")
	}
	if calls.Load() != 0 {
		t.Fatal("no query without the listing")
	}
}

func TestPanicInTheHandlerIsNotAcked(t *testing.T) {
	r, _ := testReader(t, Options{Chats: []string{"Obra Exemplo"}})
	listed(r, map[string]string{grpA.String(): "Familia Teste"})
	r.groupInfo = func(context.Context, types.JID) (*types.GroupInfo, error) { panic("fake bug with private text") }
	if r.handle(msg(grpB, friend, "MSGTEST70", "", "chegou o cimento")) {
		t.Fatal("a panic must leave the event unacked (whatsmeow would count it as success)")
	}
	if len(spool(t, r)) != 0 {
		t.Fatal("nothing written")
	}
	if n, _ := r.activity(); n != 0 {
		t.Fatalf("inflight = %d after the panic: the drain would wait for nothing", n)
	}
	if len(r.sum.Avisos) != 1 || strings.Contains(r.sum.Avisos[0], "private text") || !strings.Contains(r.sum.Avisos[0], "events.Message") {
		t.Fatalf("one warning with the event type and not the panic value: %v", r.sum.Avisos)
	}
	if !r.begin() {
		t.Fatal("the reader keeps working after a panic")
	}
	r.end()
}

func TestDrainClosesInTheSameStepItDecides(t *testing.T) {
	for round := 0; round < 30; round++ {
		r, _ := testReader(t, Options{Contatos: []string{"Fulano Teste"}})
		stop := make(chan struct{})
		var wg sync.WaitGroup
		for g := 0; g < 2; g++ { // events arriving while the pull leaves, with gaps so it can find a quiet moment
			wg.Add(1)
			go func() {
				defer wg.Done()
				for {
					select {
					case <-stop:
						return
					default:
					}
					if r.begin() {
						time.Sleep(500 * time.Microsecond)
						r.end()
					}
					time.Sleep(2 * time.Millisecond)
				}
			}()
		}
		now := time.Now()
		if _, bad := r.drain(now, now.Add(time.Minute), 0); bad {
			t.Fatal("no fatal expected")
		}
		r.mu.Lock()
		n, closing := r.inflight, r.closing
		r.mu.Unlock()
		close(stop)
		wg.Wait()
		if !closing || n != 0 {
			t.Fatalf("round %d: drain returned with closing=%v and %d handlers started after the check", round, closing, n)
		}
		if r.begin() {
			t.Fatal("begin after the drain must refuse")
		}
	}
}
