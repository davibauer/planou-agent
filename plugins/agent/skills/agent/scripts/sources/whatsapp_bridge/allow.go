package main

// Allowlist matching, message flattening and file helpers: no connection (allow_test.go covers them).

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

// Options is what the Python source writes to stdin: the allowlist and the attachment limits. It never comes in argv,
// so the names of the chats do not show up in a process listing.
type Options struct {
	Chats         []string `json:"chats"`
	Contatos      []string `json:"contatos"`
	Media         bool     `json:"media"`
	MaxMediaBytes int64    `json:"max_media_bytes"`
	HistoryDays   int      `json:"history_days"`

	// mode "all" (todos.go): every chat may be read, the local filter decides what is written
	Mode     string   `json:"mode"`     // "" = only the allowlist (the default), "all" = all contacts
	Ignore   []string `json:"ignore"`   // never read, same forms as chats/contatos (ids, numbers, names, subjects)
	Judged   []string `json:"judged"`   // chat ids with an "irrelevant" verdict: no longer candidates for being new
	Muted    []string `json:"muted"`    // chat ids muted by verdicts: never read
	Known    []string `json:"known"`    // keyed hashes of the chats known when the mode started
	Salt     string   `json:"salt"`     // the key of those hashes (per instance)
	Baseline bool     `json:"baseline"` // first run of the mode: nothing is new, every chat seen goes to the known set
	Signals  Signals  `json:"signals"`

	// mode "all", on-demand history (ondemand.go): the chats judged relevant whose earlier messages are asked of the
	// account's own phone, and how old an answered message may be (days; 0 = no limit)
	HistoryRequests []HistoryRequest `json:"history_requests"`
	OnDemandDays    int              `json:"on_demand_days"`
	AwaitingHistory []string         `json:"awaiting_history"` // every id of the chats whose answer is awaited (now or from an earlier request)
}

// matcher answers "may this chat be read?". chats: group (or any chat) JIDs, or exact group subjects. contatos: 1:1
// chats only, by JID, by phone number (digits) or by the exact contact name.
type matcher struct {
	chatJIDs, chatNames                     map[string]bool
	contactJIDs, contactUsers, contactNames map[string]bool
}

var nonDigit = regexp.MustCompile(`[^0-9]`)
var phoneLike = regexp.MustCompile(`^\+?[0-9 ()\-.]{8,}$`)

func newMatcher(o Options) *matcher {
	m := &matcher{map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}, map[string]bool{}}
	for _, e := range o.Chats {
		e = strings.TrimSpace(e)
		switch {
		case e == "":
		case strings.Contains(e, "@"):
			m.chatJIDs[e] = true
		default:
			m.chatNames[e] = true
		}
	}
	for _, e := range o.Contatos {
		e = strings.TrimSpace(e)
		switch {
		case e == "":
		case strings.Contains(e, "@"):
			m.contactJIDs[e] = true
		case phoneLike.MatchString(e):
			m.contactUsers[nonDigit.ReplaceAllString(e, "")] = true
		default:
			m.contactNames[e] = true
		}
	}
	return m
}

func (m *matcher) empty() bool {
	return len(m.chatJIDs)+len(m.chatNames)+len(m.contactJIDs)+len(m.contactUsers)+len(m.contactNames) == 0
}

// group: a group chat matched by its JID or its exact subject, without the resolved cache (tests and simple checks).
func (m *matcher) group(jid, subject string) bool {
	return m.chatJIDs[jid] || (subject != "" && m.chatNames[subject])
}

// groupDecision is the answer for one group message. Name is the listed subject that allowed it (empty when allowed by
// JID); Warn says why a group carrying a listed subject was left out.
type groupDecision struct {
	Allowed, Decided bool
	Name, Warn       string
}

// decideGroup decides a group message. A listed subject only finds the group once: resolve() maps it to that group's
// JID and from then on only that JID is allowed (cache). subjectKnown says this group's subject is known in this run
// (the joined groups were listed and it is among them). When it is not, and some listed subject has no JID yet, the
// group might be that one: Decided=false, and the caller leaves the message unacked (whatsmeow keeps it decrypted in
// the session, EnableDecryptedEventBuffer) so it is decided again on the next connection instead of being lost.
func (m *matcher) decideGroup(jid, subject string, subjectKnown bool, cache map[string]string) groupDecision {
	if m.chatJIDs[jid] {
		return groupDecision{Allowed: true, Decided: true}
	}
	for n := range m.chatNames {
		if cache[n] == jid {
			return groupDecision{Allowed: true, Decided: true, Name: n}
		}
	}
	if subjectKnown {
		if subject == "" || !m.chatNames[subject] {
			return groupDecision{Decided: true}
		}
		if other := cache[subject]; other != "" {
			return groupDecision{Decided: true, Warn: fmt.Sprintf("grupo %s com o nome %q da lista ficou de fora: o nome ja "+
				"esta ligado a outro grupo (%s); para trocar, ponha o id do grupo na lista", jid, subject, other)}
		}
		return groupDecision{Decided: true, Warn: fmt.Sprintf("grupo %s com o nome %q da lista ficou de fora: mais de um "+
			"grupo da conta tem esse nome; ponha o id do grupo na lista", jid, subject)}
	}
	for n := range m.chatNames {
		if cache[n] == "" {
			return groupDecision{}
		}
	}
	return groupDecision{Decided: true}
}

// resolve maps the listed subjects to group JIDs (what goes to the cache). A subject already resolved keeps its JID
// even if another group takes the same name later (sticky: a stranger who creates a group with that name, or a member
// who renames another group, never takes its place). A subject not resolved yet is resolved only when exactly one
// joined group has it; when several have it, none is resolved and a warning names their ids. Subjects no longer in the
// list leave the cache.
func (m *matcher) resolve(groups map[string]string, cache map[string]string) (map[string]string, []string) {
	bySubject := map[string][]string{}
	for jid, subject := range groups {
		if m.chatNames[subject] {
			bySubject[subject] = append(bySubject[subject], jid)
		}
	}
	out, warns := map[string]string{}, []string{}
	for n := range m.chatNames {
		jids := bySubject[n]
		sort.Strings(jids)
		switch {
		case cache[n] != "":
			out[n] = cache[n]
			for _, j := range jids {
				if j != cache[n] {
					warns = append(warns, fmt.Sprintf("o grupo %s tambem se chama %q, mas o nome ja esta ligado a %s: "+
						"so esse vale; para trocar, ponha o id do grupo na lista", j, n, cache[n]))
				}
			}
		case len(jids) == 1:
			out[n] = jids[0]
		case len(jids) > 1:
			warns = append(warns, fmt.Sprintf("%d grupos da conta se chamam %q (%s): nenhum e lido; ponha o id do grupo "+
				"certo na lista", len(jids), n, strings.Join(jids, ", ")))
		}
	}
	sort.Strings(warns)
	return out, warns
}

// agendaNames are the names the account itself saved for a person (the address book synced from the phone). Only
// these are compared with contatos: PushName and BusinessName are chosen by the sender, so anyone could take a listed
// name. They stay labels, never criteria.
func agendaNames(c types.ContactInfo) []string {
	if !c.Found {
		return nil
	}
	return []string{c.FullName, c.FirstName}
}

// direct: a 1:1 chat. jids are every address known for the other person (LID and phone number forms), users the phone
// numbers (JID user part of the phone form), names the address book names for them (agendaNames, never a push name).
func (m *matcher) direct(jids, users, names []string) bool {
	for _, j := range jids {
		if j != "" && (m.chatJIDs[j] || m.contactJIDs[j]) {
			return true
		}
	}
	for _, u := range users {
		if u != "" && m.contactUsers[u] {
			return true
		}
	}
	for _, n := range names {
		if n = strings.TrimSpace(n); n != "" && m.contactNames[n] {
			return true
		}
	}
	return false
}

// directChat decides a 1:1 chat and gives its label. Allowed by an address (jids), a phone number (users) or an address
// book name of the contact (agendaNames: never the push name nor the business name, which the sender chooses). The
// label is the listed name that matched (so the source's second check sees the same one), else an address book name,
// else +number.
func (m *matcher) directChat(jids, users []string, c types.ContactInfo, number string) (bool, string) {
	names := agendaNames(c)
	label := first(append(names, "+"+number)...)
	for _, n := range names {
		if n = strings.TrimSpace(n); n != "" && m.contactNames[n] {
			label = n
			break
		}
	}
	return m.direct(jids, users, names), label
}

// unmatched: entries of the allowlist that name no joined group (chats) or no saved contact (contatos by name).
// Numbers and JIDs of contacts are not checked: a person not in the address book can still write.
func (m *matcher) unmatched(groups map[string]string, contactNames map[string]bool, groupsKnown bool) []string {
	out := []string{}
	if groupsKnown {
		subjects := map[string]bool{}
		for _, s := range groups {
			subjects[s] = true
		}
		for j := range m.chatJIDs {
			if strings.HasSuffix(j, "@g.us") && groups[j] == "" {
				out = append(out, j)
			}
		}
		for n := range m.chatNames {
			if !subjects[n] {
				out = append(out, n)
			}
		}
	}
	if contactNames != nil {
		for n := range m.contactNames {
			if !contactNames[n] {
				out = append(out, n)
			}
		}
	}
	return out
}

// media is the downloadable part of a message, when there is one.
type media struct {
	Kind, Name, Mime string
	Size             int64
	Msg              any // a whatsmeow.DownloadableMessage (image, video, audio or document message)
}

// flatten turns a message into one text line and its attachment. ok=false for what is not a message to show
// (reactions, edits and deletions, protocol messages, stickers, empty payloads).
func flatten(msg *waE2E.Message) (text string, att *media, ok bool) {
	if msg == nil {
		return "", nil, false
	}
	switch {
	case msg.GetConversation() != "":
		return msg.GetConversation(), nil, true
	case msg.GetExtendedTextMessage() != nil:
		t := msg.GetExtendedTextMessage().GetText()
		return t, nil, t != ""
	case msg.GetImageMessage() != nil:
		x := msg.GetImageMessage()
		return x.GetCaption(), &media{"imagem", "", x.GetMimetype(), int64(x.GetFileLength()), x}, true
	case msg.GetVideoMessage() != nil:
		x := msg.GetVideoMessage()
		return x.GetCaption(), &media{"video", "", x.GetMimetype(), int64(x.GetFileLength()), x}, true
	case msg.GetAudioMessage() != nil:
		x := msg.GetAudioMessage()
		label := fmt.Sprintf("[audio %ds]", x.GetSeconds())
		return label, &media{"audio", "", x.GetMimetype(), int64(x.GetFileLength()), x}, true
	case msg.GetDocumentMessage() != nil:
		x := msg.GetDocumentMessage()
		return x.GetCaption(), &media{"documento", x.GetFileName(), x.GetMimetype(), int64(x.GetFileLength()), x}, true
	case msg.GetDocumentWithCaptionMessage().GetMessage() != nil:
		return flatten(msg.GetDocumentWithCaptionMessage().GetMessage())
	case msg.GetLocationMessage() != nil:
		x := msg.GetLocationMessage()
		return strings.TrimSpace(fmt.Sprintf("[localizacao %s %s]", x.GetName(), x.GetAddress())), nil, true
	case msg.GetContactMessage() != nil:
		return "[contato: " + msg.GetContactMessage().GetDisplayName() + "]", nil, true
	case msg.GetPollCreationMessage() != nil:
		return "[enquete: " + msg.GetPollCreationMessage().GetName() + "]", nil, true
	case msg.GetPollCreationMessageV3() != nil:
		return "[enquete: " + msg.GetPollCreationMessageV3().GetName() + "]", nil, true
	}
	return "", nil, false
}

var unsafeName = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// fileName is the local name of an attachment: <day>_<message id>_<original name or kind.ext>, path-safe.
func fileName(day, id string, att *media) string {
	name := att.Name
	if name == "" {
		ext := ""
		if i := strings.Index(att.Mime, "/"); i >= 0 {
			ext = strings.SplitN(att.Mime[i+1:], ";", 2)[0]
		}
		name = att.Kind
		if ext != "" {
			name += "." + ext
		}
	}
	name = strings.Trim(unsafeName.ReplaceAllString(filepath.Base(name), "_"), "._")
	if len(name) > 80 {
		name = name[len(name)-80:]
	}
	if name == "" {
		name = att.Kind
	}
	return day + "_" + unsafeName.ReplaceAllString(id, "_") + "_" + name
}

// chatDir is the folder of a chat's attachments: the user part of its JID (no names on disk paths).
func chatDir(jid string) string {
	d := unsafeName.ReplaceAllString(strings.SplitN(jid, "@", 2)[0], "_")
	if strings.Trim(d, ".") == "" {
		return "_"
	}
	return d
}

var errTooBig = errors.New("anexo maior que o limite")

// cappedFile is the download target of an attachment (the whatsmeow.File methods, nothing else): writes past limit
// fail, so a sender who declares a small size and serves a big file cannot fill the disk. It does not embed *os.File on
// purpose: ReadFrom or WriteString promoted from it would let io.Copy write around the cap (and, not being an *os.File,
// whatsmeow does not preallocate the size the server declares).
type cappedFile struct {
	f     *os.File
	limit int64
}

func (c cappedFile) Read(p []byte) (int, error)                { return c.f.Read(p) }
func (c cappedFile) ReadAt(p []byte, off int64) (int, error)   { return c.f.ReadAt(p, off) }
func (c cappedFile) Seek(off int64, whence int) (int64, error) { return c.f.Seek(off, whence) }
func (c cappedFile) Truncate(size int64) error                 { return c.f.Truncate(size) }
func (c cappedFile) Stat() (os.FileInfo, error)                { return c.f.Stat() }
func (c cappedFile) over(off int64, n int) bool                { return c.limit > 0 && off+int64(n) > c.limit }

func (c cappedFile) Write(p []byte) (int, error) {
	pos, err := c.f.Seek(0, io.SeekCurrent)
	if err != nil {
		return 0, err
	}
	if c.over(pos, len(p)) {
		return 0, errTooBig
	}
	return c.f.Write(p)
}

func (c cappedFile) WriteAt(p []byte, off int64) (int, error) {
	if c.over(off, len(p)) {
		return 0, errTooBig
	}
	return c.f.WriteAt(p, off)
}
