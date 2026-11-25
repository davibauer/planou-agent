package main

// Fake ids and names only.

import (
	"errors"
	"io"
	"os"
	"sort"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

const (
	groupA = "120363000000000001@g.us"
	groupB = "120363000000000002@g.us"
	pnX    = "550000000001@s.whatsapp.net"
	lidX   = "100000000000001@lid"
	groupC = "120363000000000003@g.us"
)

func TestEmptyAllowlistReadsNothing(t *testing.T) {
	m := newMatcher(Options{Chats: []string{" ", ""}, Contatos: nil})
	if !m.empty() {
		t.Fatal("blank entries must leave the list empty")
	}
	if m.group(groupA, "Obra Exemplo") || m.direct([]string{pnX}, []string{"550000000001"}, []string{"Fulano Teste"}) {
		t.Fatal("an empty list must not allow any chat")
	}
}

func TestGroupByIdOrExactSubject(t *testing.T) {
	m := newMatcher(Options{Chats: []string{groupA, "Obra Exemplo"}})
	if !m.group(groupA, "") {
		t.Fatal("group by id")
	}
	if !m.group(groupB, "Obra Exemplo") {
		t.Fatal("group by exact subject")
	}
	if m.group(groupB, "obra exemplo") || m.group(groupB, "Obra Exemplo 2") {
		t.Fatal("subject must match exactly")
	}
	if m.direct([]string{pnX}, []string{"550000000001"}, []string{"Obra Exemplo"}) {
		t.Fatal("a group subject in chats must not allow a 1:1 chat by name")
	}
}

func TestContactByNumberJidOrName(t *testing.T) {
	m := newMatcher(Options{Contatos: []string{"+55 (00) 0000-0001", lidX, "Fulano Teste"}})
	if !m.direct([]string{"x@s.whatsapp.net"}, []string{"550000000001"}, nil) {
		t.Fatal("contact by phone number, formatting ignored")
	}
	if !m.direct([]string{lidX}, nil, nil) {
		t.Fatal("contact by LID")
	}
	if !m.direct(nil, nil, []string{"", " Fulano Teste "}) {
		t.Fatal("contact by exact name")
	}
	if m.direct([]string{pnX}, []string{"550000000999"}, []string{"Ciclano Teste"}) {
		t.Fatal("unlisted contact allowed")
	}
	if m.group(groupA, "Fulano Teste") {
		t.Fatal("a contact name must not allow a group")
	}
}

func TestUnmatched(t *testing.T) {
	m := newMatcher(Options{Chats: []string{groupA, groupB, "Obra Exemplo", "Grupo Sumido"}, Contatos: []string{"Fulano Teste", "Ciclano Teste", "550000000001"}})
	got := m.unmatched(map[string]string{groupA: "Obra Exemplo"}, map[string]bool{"Fulano Teste": true}, true)
	sort.Strings(got)
	want := []string{groupB, "Ciclano Teste", "Grupo Sumido"}
	sort.Strings(want)
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("unmatched = %v, want %v", got, want)
	}
	if len(m.unmatched(nil, nil, false)) != 0 {
		t.Fatal("unknown groups and contacts: nothing to report")
	}
}

func TestFlatten(t *testing.T) {
	text, att, ok := flatten(&waE2E.Message{Conversation: proto.String("ola")})
	if !ok || text != "ola" || att != nil {
		t.Fatal("plain text")
	}
	doc := &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("orcamento exemplo.pdf"),
		Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(2048), Caption: proto.String("segue")}}
	text, att, ok = flatten(doc)
	if !ok || text != "segue" || att == nil || att.Kind != "documento" || att.Size != 2048 {
		t.Fatalf("document: %q %+v", text, att)
	}
	if got := fileName("2026-01-02", "ABC/123", att); got != "2026-01-02_ABC_123_orcamento_exemplo.pdf" {
		t.Fatalf("fileName = %q", got)
	}
	_, att, _ = flatten(&waE2E.Message{ImageMessage: &waE2E.ImageMessage{Mimetype: proto.String("image/jpeg")}})
	if got := fileName("2026-01-02", "X1", att); got != "2026-01-02_X1_imagem.jpeg" {
		t.Fatalf("image fileName = %q", got)
	}
	if got := fileName("d", "i", &media{Kind: "documento", Name: "../../etc/passwd"}); strings.Contains(got, "/") {
		t.Fatalf("path traversal in %q", got)
	}
	if _, _, ok := flatten(&waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String("x")}}); ok {
		t.Fatal("reactions are not shown")
	}
	if _, _, ok := flatten(&waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{}}); ok {
		t.Fatal("protocol messages are not shown")
	}
	if chatDir(groupA) != "120363000000000001" {
		t.Fatal("chatDir")
	}
}

func TestDecideGroupWithoutTheSubjects(t *testing.T) {
	m := newMatcher(Options{Chats: []string{groupA, "Obra Exemplo"}})
	if d := m.decideGroup(groupA, "", false, nil); !d.Allowed || !d.Decided {
		t.Fatal("a group listed by id never waits for the names")
	}
	if d := m.decideGroup(groupB, "", false, nil); d.Decided {
		t.Fatal("names unknown and a listed name not resolved yet: undecided (left unacked)")
	}
	cache := map[string]string{"Obra Exemplo": groupB}
	if d := m.decideGroup(groupB, "", false, cache); !d.Allowed || !d.Decided || d.Name != "Obra Exemplo" {
		t.Fatalf("a name resolved in an earlier run allows its group by JID: %+v", d)
	}
	if d := m.decideGroup(groupC, "", false, cache); d.Allowed || !d.Decided {
		t.Fatal("every listed name resolved: any other group is dropped")
	}
	if d := m.decideGroup(groupB, "Outro Grupo", true, nil); d.Allowed || !d.Decided {
		t.Fatal("subjects known: other subject dropped")
	}
	stale := map[string]string{"Nome Retirado": groupB}
	if d := m.decideGroup(groupB, "Outro", true, stale); d.Allowed {
		t.Fatal("a cached name no longer in the list allows nothing")
	}
}

func TestListedSubjectFindsTheGroupOnceThenOnlyItsId(t *testing.T) {
	m := newMatcher(Options{Chats: []string{"Obra Exemplo"}})
	cache, warns := m.resolve(map[string]string{groupA: "Obra Exemplo", groupB: "Outro Grupo"}, nil)
	if len(cache) != 1 || cache["Obra Exemplo"] != groupA || len(warns) != 0 {
		t.Fatalf("resolve = %v %v (only listed subjects are cached)", cache, warns)
	}
	if d := m.decideGroup(groupA, "Obra Exemplo", true, cache); !d.Allowed {
		t.Fatal("the resolved group is allowed")
	}
	// a stranger creates a group with the same subject and adds the account (or a member renames another group)
	groups := map[string]string{groupA: "Obra Exemplo", groupC: "Obra Exemplo"}
	cache, warns = m.resolve(groups, cache)
	if cache["Obra Exemplo"] != groupA {
		t.Fatalf("a resolved subject keeps its group: %v", cache)
	}
	if len(warns) != 1 || !strings.Contains(warns[0], groupC) {
		t.Fatalf("the homonym is reported: %v", warns)
	}
	if d := m.decideGroup(groupC, "Obra Exemplo", true, cache); d.Allowed || !d.Decided || d.Warn == "" {
		t.Fatalf("the homonym stays out with a warning: %+v", d)
	}
	if d := m.decideGroup(groupA, "Nome Novo", true, cache); !d.Allowed || d.Name != "Obra Exemplo" {
		t.Fatalf("the resolved group renamed is still allowed by its id, under the listed name: %+v", d)
	}
	// two groups with the subject before it was ever resolved: none is read, and the pairing says which ids
	cache, warns = m.resolve(groups, nil)
	if len(cache) != 0 || len(warns) != 1 || !strings.Contains(warns[0], groupA) || !strings.Contains(warns[0], groupC) {
		t.Fatalf("ambiguous subject: %v %v", cache, warns)
	}
	if d := m.decideGroup(groupA, "Obra Exemplo", true, cache); d.Allowed || d.Warn == "" {
		t.Fatalf("ambiguous subject allows neither group: %+v", d)
	}
	// a subject taken off the list leaves the cache
	if c, _ := newMatcher(Options{Chats: []string{"Outro"}}).resolve(groups, map[string]string{"Obra Exemplo": groupA}); len(c) != 0 {
		t.Fatalf("stale subject kept: %v", c)
	}
}

func TestOnlyAddressBookNamesAllowAContact(t *testing.T) {
	m := newMatcher(Options{Contatos: []string{"Fulano Teste"}})
	stranger := types.ContactInfo{Found: true, PushName: "Fulano Teste", BusinessName: "Fulano Teste"}
	if ok, label := m.directChat([]string{pnX}, []string{"550000000001"}, stranger, "550000000001"); ok || label == "Fulano Teste" {
		t.Fatalf("a push or business name equal to a listed name must not allow the chat (ok=%v label=%q)", ok, label)
	}
	saved := types.ContactInfo{Found: true, FirstName: "Fulano", FullName: "Fulano Teste", PushName: "Outro Nome"}
	if ok, label := m.directChat([]string{pnX}, nil, saved, "550000000001"); !ok || label != "Fulano Teste" {
		t.Fatalf("the address book name allows it and labels it (ok=%v label=%q)", ok, label)
	}
	if got := agendaNames(types.ContactInfo{Found: true, FullName: "A", FirstName: "B", PushName: "C", BusinessName: "D"}); strings.Join(got, ",") != "A,B" {
		t.Fatalf("agendaNames = %v", got)
	}
}

func TestChatDirAndCappedFile(t *testing.T) {
	for in, want := range map[string]string{"..@g.us": "_", ".@s.whatsapp.net": "_", "@x": "_", "a/b@x": "a_b", "5500@x": "5500"} {
		if got := chatDir(in); got != want {
			t.Fatalf("chatDir(%q) = %q, want %q", in, got, want)
		}
	}
	f, err := os.CreateTemp(t.TempDir(), "cap")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	c := cappedFile{f, 10}
	if _, err := c.Write([]byte("12345678")); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Write([]byte("abc")); !errors.Is(err, errTooBig) {
		t.Fatalf("write past the cap: %v", err)
	}
	if _, err := c.WriteAt([]byte("abc"), 9); !errors.Is(err, errTooBig) {
		t.Fatalf("WriteAt past the cap: %v", err)
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	// io.Copy is what whatsmeow uses; it must go through the cap, not *os.File's ReadFrom
	if _, err := io.Copy(c, strings.NewReader(strings.Repeat("x", 100))); !errors.Is(err, errTooBig) {
		t.Fatalf("io.Copy past the cap: %v", err)
	}
	if st, _ := f.Stat(); st.Size() > 10 {
		t.Fatalf("file grew to %d bytes", st.Size())
	}
}
