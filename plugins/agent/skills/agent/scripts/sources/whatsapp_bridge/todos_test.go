package main

// The "all contacts" mode (todos.go) over a real session store, never connected. Fake ids, names and texts only.

import (
	"context"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

const salt = "sal-de-teste"

var groupTest = types.JID{User: "120363000000000005", Server: types.GroupServer}

func allOpts(o Options) Options {
	o.Mode, o.Salt, o.Media = "all", salt, true
	return o
}

func docMsg(chat, sender types.JID, id, file string) *events.Message {
	m := msg(chat, sender, id, "", "")
	m.Message = &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String(file),
		Mimetype: proto.String("application/pdf"), FileLength: proto.Uint64(2048)}}
	return m
}

func TestAllModeDropsAKnownChatWithoutASignalAndWritesNothing(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Known: []string{hashID(salt, stranger.String())},
		Signals: Signals{Words: []string{"concreto"}}}))
	if !r.handle(msg(stranger, stranger, "MSGALL1", "", "a festa e no sabado")) {
		t.Fatal("a dropped message is decided (acked)")
	}
	if got := spool(t, r); len(got) != 0 || r.sum.Descartadas != 1 {
		t.Fatalf("spool = %+v descartadas = %d", got, r.sum.Descartadas)
	}
}

func TestAllModeNewChatIsACandidateUntilAVerdict(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{}))
	if !r.handle(msg(stranger, stranger, "MSGALL2", "", "bom dia, sou da serralheria")) {
		t.Fatal("ack")
	}
	got := spool(t, r)
	if len(got) != 1 || got[0].Candidata != "novo" || got[0].Texto != "bom dia, sou da serralheria" || r.sum.Candidatas != 1 {
		t.Fatalf("spool = %+v", got)
	}
	// "irrelevant" verdict: no longer new; without a signal it is dropped
	r2, _ := testReader(t, allOpts(Options{Judged: []string{stranger.String()}, Signals: Signals{Words: []string{"concreto"}}}))
	r2.handle(msg(stranger, stranger, "MSGALL3", "", "e ai, tudo bem?"))
	if got := spool(t, r2); len(got) != 0 {
		t.Fatalf("judged irrelevant still new: %+v", got)
	}
	// ...but a signal of the objective still brings it back as a candidate
	r2.handle(msg(stranger, stranger, "MSGALL4", "", "o concreto chega amanha"))
	if got := spool(t, r2); len(got) != 1 || got[0].Candidata != "sinal" || !reflect.DeepEqual(got[0].Sinais, []string{"palavra concreto"}) {
		t.Fatalf("spool = %+v", got)
	}
}

func TestAllModeBaselineHasNothingNewAndReportsTheKnownChats(t *testing.T) {
	r, dev := testReader(t, allOpts(Options{Baseline: true}))
	if err := dev.Contacts.PutContactName(context.Background(), friend, "Fulano", "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	r.handle(msg(stranger, stranger, "MSGALL5", "", "oi"))
	if got := spool(t, r); len(got) != 0 {
		t.Fatalf("baseline wrote a new chat: %+v", got)
	}
	r.groups, r.groupsKnown = map[string]string{groupTest.String(): "Grupo Teste"}, true
	if err := dev.AppState.PutAppStateVersion(context.Background(), string(appstate.WAPatchCriticalUnblockLow), 7, [128]byte{}); err != nil {
		t.Fatal(err)
	}
	s := r.finish(false)
	if !s.BaseOK {
		t.Fatalf("groups listed and address book synced: the baseline is complete (%+v)", s.Avisos)
	}
	want := map[string]bool{hashID(salt, stranger.String()): true, hashID(salt, friend.String()): true, hashID(salt, groupTest.String()): true}
	got := map[string]bool{}
	for _, h := range s.Conhecidos {
		got[h] = true
		if h == stranger.String() || len(h) != 16 {
			t.Fatalf("known chats must be keyed hashes: %q", h)
		}
	}
	for h := range want {
		if !got[h] {
			t.Fatalf("known = %v, missing %s", s.Conhecidos, h)
		}
	}
}

func TestAllModeSignalsIgnoreAccentsAndCaseAndNeedWholeWords(t *testing.T) {
	sg := Signals{Names: []string{"João Exemplo"}, Words: []string{"concreto"}, Acronyms: []string{"ART"},
		Domains: []string{"exemplo.com.br"}, AttachmentWords: []string{"orçamento"}, Phones: []string{"+55 0000000101"}}
	cases := []struct {
		text, file string
		users      []string
		want       []string
	}{
		{"falei com o JOAO exemplo hoje", "", nil, []string{"nome João Exemplo"}},
		{"Concreto usinado", "", nil, []string{"palavra concreto"}},
		{"concretos e concretagem", "", nil, []string{}},
		{"a ART saiu", "", nil, []string{"sigla ART"}},
		{"uma arte bonita, art", "", nil, []string{}},
		{"veja www.exemplo.com.br/x", "", nil, []string{"dominio exemplo.com.br"}},
		{"", "Orcamento_v2.pdf", nil, []string{"anexo orçamento"}},
		{"oi", "", []string{"550000000101"}, []string{"telefone"}},
	}
	for _, c := range cases {
		got := sg.hits(c.text, "", nil, c.file, c.users)
		if len(got) == 0 && len(c.want) == 0 {
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("hits(%q, %q) = %v, want %v", c.text, c.file, got, c.want)
		}
	}
}

func TestAllModeMutedChatIsNeverRead(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Muted: []string{stranger.String()}, Signals: Signals{Words: []string{"concreto"}}}))
	if !r.handle(msg(stranger, stranger, "MSGALL6", "", "o concreto chega amanha")) {
		t.Fatal("ack")
	}
	if got := spool(t, r); len(got) != 0 {
		t.Fatalf("muted chat written: %+v", got)
	}
}

func TestAllModeIgnoreWinsOverTheListSignalsAndNovelty(t *testing.T) {
	r, dev := testReader(t, allOpts(Options{Chats: []string{stranger.String()}, Ignore: []string{"+55 0000000101", "Fulano Teste"},
		Signals: Signals{Words: []string{"concreto"}}}))
	if err := dev.Contacts.PutContactName(context.Background(), friend, "Fulano", "Fulano Teste"); err != nil {
		t.Fatal(err)
	}
	r.handle(msg(stranger, stranger, "MSGALL7", "", "o concreto chega amanha")) // ignored by number, even listed
	r.handle(msg(friend, friend, "MSGALL8", "", "o concreto chega amanha"))     // ignored by the address book name
	if got := spool(t, r); len(got) != 0 || r.sum.Ignoradas != 2 {
		t.Fatalf("spool = %+v ignoradas = %d", got, r.sum.Ignoradas)
	}
}

func TestAllModeIgnoredGroupByNameFailsClosedUntilTheSubjectIsKnown(t *testing.T) {
	o := allOpts(Options{Ignore: []string{"Familia Teste"}})
	r, _ := testReader(t, o)
	if r.handle(msg(groupTest, friend, "MSGALL9", "", "almoco domingo?")) {
		t.Fatal("subject unknown and the ignore list names groups: no ack, nothing written")
	}
	if got := spool(t, r); len(got) != 0 || r.sum.Indecididas != 1 {
		t.Fatalf("spool = %+v indecididas = %d", got, r.sum.Indecididas)
	}
	// next run: the listing names it
	r2, _ := testReader(t, o)
	listed(r2, map[string]string{groupTest.String(): "Familia Teste"})
	if !r2.handle(msg(groupTest, friend, "MSGALL9", "", "almoco domingo?")) || len(spool(t, r2)) != 0 || r2.sum.Ignoradas != 1 {
		t.Fatalf("ignored group: spool = %+v", spool(t, r2))
	}
	// a group missing from the listing: its info is asked, then decided by the same rule
	r3, _ := testReader(t, o)
	listed(r3, map[string]string{})
	fakeInfo(r3, map[string]string{groupTest.String(): "Familia Teste"}, nil)
	if !r3.handle(msg(groupTest, friend, "MSGALL10", "", "almoco domingo?")) || len(spool(t, r3)) != 0 {
		t.Fatalf("ignored group found by its info: spool = %+v", spool(t, r3))
	}
	// another group is a new chat: a candidate
	r4, _ := testReader(t, o)
	listed(r4, map[string]string{groupTest.String(): "Fornecedores Teste"})
	r4.handle(msg(groupTest, friend, "MSGALL11", "", "segue a cotacao"))
	if got := spool(t, r4); len(got) != 1 || got[0].Candidata != "novo" || got[0].ChatNome != "Fornecedores Teste" {
		t.Fatalf("spool = %+v", got)
	}
}

func TestAllModeRelevantChatIsReadWithItsAttachmentsAndACandidateIsNot(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Signals: Signals{AttachmentWords: []string{"orcamento"}}}))
	r.handle(docMsg(stranger, stranger, "MSGALL12", "orcamento-janela.pdf"))
	got := spool(t, r)
	if len(got) != 1 || got[0].Candidata != "sinal" || got[0].Anexo == nil || got[0].Anexo.Caminho != "" ||
		got[0].Anexo.Erro == "" || got[0].Anexo.Nome != "orcamento-janela.pdf" {
		t.Fatalf("candidate attachment must be listed, not downloaded: %+v", got)
	}
	if _, err := os.Stat(r.media); !os.IsNotExist(err) {
		t.Fatalf("nothing may be written to the media folder for a candidate (%v)", err)
	}
	// relevant (passed in chats): written as usual, the download is tried (here it has no download data)
	r2, _ := testReader(t, allOpts(Options{Chats: []string{stranger.String()}}))
	r2.handle(docMsg(stranger, stranger, "MSGALL13", "orcamento-janela.pdf"))
	got = spool(t, r2)
	if len(got) != 1 || got[0].Candidata != "" || got[0].Anexo == nil || got[0].Anexo.Erro == "candidata: anexo so e baixado de chat julgado relevante" {
		t.Fatalf("relevant chat: %+v", got)
	}
}

func TestAllModeHistoryOlderThanTheLimitIsDropped(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{HistoryDays: 3}))
	old := msg(stranger, stranger, "MSGALL14", "", "conversa antiga")
	old.Info.Timestamp = time.Now().Add(-10 * 24 * time.Hour)
	recent := msg(friend, friend, "MSGALL15", "", "conversa recente")
	recent.Info.Timestamp = time.Now().Add(-24 * time.Hour)
	if !r.message(old, "historico") || !r.message(recent, "historico") {
		t.Fatal("history messages are decided")
	}
	got := spool(t, r)
	if len(got) != 1 || got[0].ID != "MSGALL15" {
		t.Fatalf("spool = %+v", got)
	}
}

func TestListModeIgnoresTheAllModeFields(t *testing.T) {
	r, _ := testReader(t, Options{Contatos: []string{"Outro Nome"}, Signals: Signals{Words: []string{"concreto"}}})
	r.handle(msg(stranger, stranger, "MSGALL16", "", "o concreto chega amanha"))
	if got := spool(t, r); len(got) != 0 || r.all != nil {
		t.Fatalf("list mode wrote a chat outside the list: %+v", got)
	}
}

func TestAllModeBaselineIsIncompleteWithoutTheGroupsOrTheAddressBook(t *testing.T) {
	baseOK := func(s summary) bool {
		for _, a := range s.Avisos {
			if strings.Contains(a, "linha de base do modo todos incompleta") {
				if s.BaseOK {
					t.Fatalf("a warning and a complete flag: %+v", s)
				}
				return false
			}
		}
		return s.BaseOK
	}
	// the listing of the joined groups failed (or never came): incomplete, even with the address book synced
	r, dev := testReader(t, allOpts(Options{Baseline: true}))
	if err := dev.AppState.PutAppStateVersion(context.Background(), string(appstate.WAPatchCriticalUnblockLow), 3, [128]byte{}); err != nil {
		t.Fatal(err)
	}
	if s := r.finish(false); baseOK(s) || len(s.Conhecidos) != 0 {
		t.Fatalf("groups unknown: %+v", s)
	}
	// groups listed, the address book never synced: incomplete
	r2, _ := testReader(t, allOpts(Options{Baseline: true}))
	r2.groups, r2.groupsKnown = map[string]string{groupTest.String(): "Grupo Teste"}, true
	if s := r2.finish(false); baseOK(s) {
		t.Fatalf("address book not synced: %+v", s)
	}
	// the pairing: the full sync event of this run is what counts, a version alone is not enough
	r3, dev3 := testReader(t, allOpts(Options{Baseline: true}))
	r3.groups, r3.groupsKnown = map[string]string{groupTest.String(): "Grupo Teste"}, true
	if err := dev3.AppState.PutAppStateVersion(context.Background(), string(appstate.WAPatchCriticalUnblockLow), 3, [128]byte{}); err != nil {
		t.Fatal(err)
	}
	if s := r3.finish(true); baseOK(s) {
		t.Fatalf("pairing without the sync event: %+v", s)
	}
	r3.handle(&events.AppStateSyncComplete{Name: appstate.WAPatchCriticalUnblockLow})
	if s := r3.finish(true); !baseOK(s) {
		t.Fatalf("pairing with the sync event: %+v", s)
	}
	// not the first run: no flag, no warning
	r4, _ := testReader(t, allOpts(Options{}))
	if s := r4.finish(false); s.BaseOK || baseOK(s) {
		t.Fatalf("not a baseline: %+v", s)
	}
}

var lidChat = types.JID{User: "100000000000077", Server: types.HiddenUserServer}

func TestAllModeLidChatWithoutANumberIsNeverACandidateWithAnIgnoreList(t *testing.T) {
	// the reviewer's case: an @lid chat, no SenderAlt, no stored mapping, a number in the ignore list
	o := allOpts(Options{Ignore: []string{"+55 0000000777"}, Signals: Signals{Words: []string{"concreto"}}})
	r, _ := testReader(t, o)
	if !r.handle(msg(lidChat, lidChat, "MSGLID1", "Fulano", "bom dia")) { // would be "novo"
		t.Fatal("dropped is decided (acked), never left in the session")
	}
	if !r.handle(msg(lidChat, lidChat, "MSGLID2", "Fulano", "o concreto chega amanha")) { // would be "sinal"
		t.Fatal("ack")
	}
	if got := spool(t, r); len(got) != 0 || r.sum.Candidatas != 0 || r.sum.SemNumero != 2 || r.sum.Descartadas != 2 {
		t.Fatalf("spool = %+v sum = %+v", got, r.sum)
	}
	if s := r.finish(false); len(s.Avisos) != 1 || !strings.Contains(s.Avisos[0], "sem numero conhecido") ||
		!strings.Contains(s.Avisos[0], lidChat.String()) { // the dropped id is listed, so it can go to contatos
		t.Fatalf("avisos = %+v", s.Avisos)
	}
	// without an ignore list nothing is lost by it: still a new chat
	r2, _ := testReader(t, allOpts(Options{}))
	r2.handle(msg(lidChat, lidChat, "MSGLID3", "Fulano", "bom dia"))
	if got := spool(t, r2); len(got) != 1 || got[0].Candidata != "novo" {
		t.Fatalf("no ignore list: %+v", got)
	}
	// with the mapping in the store the number is known and the ignore list wins
	r3, dev3 := testReader(t, o)
	pn := types.JID{User: "550000000777", Server: types.DefaultUserServer}
	if err := dev3.LIDs.PutLIDMapping(context.Background(), lidChat, pn); err != nil {
		t.Fatal(err)
	}
	r3.handle(msg(lidChat, lidChat, "MSGLID4", "Fulano", "o concreto chega amanha"))
	if got := spool(t, r3); len(got) != 0 || r3.sum.Ignoradas != 1 || r3.sum.SemNumero != 0 {
		t.Fatalf("mapped and ignored: %+v %+v", got, r3.sum)
	}
	// the @lid listed in contatos is still read
	r4, _ := testReader(t, allOpts(Options{Contatos: []string{lidChat.String()}, Ignore: []string{"+55 0000000777"}}))
	r4.handle(msg(lidChat, lidChat, "MSGLID5", "Fulano", "bom dia"))
	if got := spool(t, r4); len(got) != 1 || got[0].Candidata != "" {
		t.Fatalf("listed @lid: %+v", got)
	}
}

func TestAllModeNoNumberWarningListsOnlyTheIdsWithACap(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Ignore: []string{"Fulano Teste"}}))
	for i := 0; i < maxNoNumberIDs+2; i++ {
		lid := types.JID{User: fmt.Sprintf("1000000000002%02d", i), Server: types.HiddenUserServer}
		r.handle(msg(lid, lid, fmt.Sprintf("MSGCAP%d", i), "Beltrano", "texto que nunca aparece"))
	}
	s := r.finish(false)
	if len(s.Avisos) != 1 || s.SemNumero != maxNoNumberIDs+2 {
		t.Fatalf("sum = %+v", s)
	}
	w := s.Avisos[0]
	if strings.Count(w, "@lid") != maxNoNumberIDs+1 || !strings.Contains(w, "e mais 2") || // +1: the "(so o id @lid)"
		strings.Contains(w, "texto que nunca aparece") || strings.Contains(w, "Beltrano") {
		t.Fatalf("aviso = %q", w)
	}
}

func TestAllModeIgnoreWithOnlyIdsKeepsALidChatWithoutANumber(t *testing.T) {
	// the reviewer's probe: only a group id in ignorar, nothing to check by number or name
	for _, ig := range [][]string{{groupTest.String()}, {"100000000000099@lid"}} {
		r, _ := testReader(t, allOpts(Options{Ignore: ig}))
		r.handle(msg(lidChat, lidChat, "MSGIDS1", "Fulano", "bom dia"))
		if got := spool(t, r); len(got) != 1 || got[0].Candidata != "novo" || r.sum.SemNumero != 0 {
			t.Fatalf("ignore %v: %+v %+v", ig, got, r.sum)
		}
	}
	// a ...@s.whatsapp.net id does need the number
	r, _ := testReader(t, allOpts(Options{Ignore: []string{"550000000777@s.whatsapp.net"}}))
	r.handle(msg(lidChat, lidChat, "MSGIDS2", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 0 || r.sum.SemNumero != 1 {
		t.Fatalf("pn id: %+v %+v", got, r.sum)
	}
}

func TestAllModeChatByNumberIsAlsoMatchedByItsLid(t *testing.T) {
	// the reviewer's probe: the @lid shown in the candidates block put in ignorar, the chat comes by the number
	lid := types.JID{User: "100000000000088", Server: types.HiddenUserServer}
	pn := types.JID{User: "550000000888", Server: types.DefaultUserServer}
	reader := func(o Options) *reader {
		r, dev := testReader(t, o)
		if err := dev.LIDs.PutLIDMapping(context.Background(), lid, pn); err != nil {
			t.Fatal(err)
		}
		return r
	}
	r := reader(allOpts(Options{Ignore: []string{lid.String()}}))
	r.handle(msg(pn, pn, "MSGPNL1", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 0 || r.sum.Ignoradas != 1 || r.sum.Candidatas != 0 {
		t.Fatalf("ignored by lid: %+v %+v", got, r.sum)
	}
	// muted and judged by the lid (a verdict given while the chat came as @lid)
	r = reader(allOpts(Options{Muted: []string{lid.String()}, Signals: Signals{Words: []string{"concreto"}}}))
	r.handle(msg(pn, pn, "MSGPNL2", "Fulano", "o concreto chega amanha"))
	if got := spool(t, r); len(got) != 0 || r.sum.Descartadas != 1 {
		t.Fatalf("muted by lid: %+v %+v", got, r.sum)
	}
	r = reader(allOpts(Options{Judged: []string{lid.String()}}))
	r.handle(msg(pn, pn, "MSGPNL3", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 0 {
		t.Fatalf("judged by lid: %+v", got)
	}
	// known by the lid's hash: not new
	r = reader(allOpts(Options{Known: []string{hashID(salt, lid.String())}}))
	r.handle(msg(pn, pn, "MSGPNL4", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 0 {
		t.Fatalf("known by lid: %+v", got)
	}
	// listed by the lid: read, and the line carries the lid so the source's second check agrees
	r = reader(allOpts(Options{Contatos: []string{lid.String()}}))
	r.handle(msg(pn, pn, "MSGPNL5", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 1 || got[0].Candidata != "" || got[0].ChatLID != lid.String() {
		t.Fatalf("listed by lid: %+v", got)
	}
	// with no mapping, the lid comes from SenderAlt
	r2, _ := testReader(t, allOpts(Options{Ignore: []string{lid.String()}}))
	m := msg(pn, pn, "MSGPNL6", "Fulano", "bom dia")
	m.Info.SenderAlt = lid
	r2.handle(m)
	if got := spool(t, r2); len(got) != 0 || r2.sum.Ignoradas != 1 {
		t.Fatalf("lid from SenderAlt: %+v %+v", got, r2.sum)
	}
}

func TestAllModeChatByNumberWithAnUnknownLidIsNeverACandidateWithALidInIgnorar(t *testing.T) {
	// the reviewer's probe: an @lid in ignorar, the chat comes by the number, no SenderAlt and no stored mapping
	lid := types.JID{User: "100000000000066", Server: types.HiddenUserServer}
	pn := types.JID{User: "550000000666", Server: types.DefaultUserServer}
	o := allOpts(Options{Ignore: []string{lid.String()}, Signals: Signals{Words: []string{"concreto"}}})
	r, _ := testReader(t, o)
	if !r.handle(msg(pn, pn, "MSGNOLID1", "Fulano", "bom dia")) { // would be "novo"
		t.Fatal("dropped is decided (acked), never left in the session")
	}
	if !r.handle(msg(pn, pn, "MSGNOLID2", "Fulano", "o concreto chega amanha")) { // would be "sinal"
		t.Fatal("ack")
	}
	if got := spool(t, r); len(got) != 0 || r.sum.Candidatas != 0 || r.sum.Ignoradas != 0 || r.sum.SemLID != 2 ||
		r.sum.Descartadas != 2 {
		t.Fatalf("spool = %+v sum = %+v", got, r.sum)
	}
	if s := r.finish(false); len(s.Avisos) != 1 || !strings.Contains(s.Avisos[0], "sem o id @lid conhecido") ||
		!strings.Contains(s.Avisos[0], "2 msgs") || strings.Contains(s.Avisos[0], pn.User) ||
		strings.Contains(s.Avisos[0], "concreto") { // only the count, never the number or the text
		t.Fatalf("avisos = %+v", s.Avisos)
	}
	// with SenderAlt (or RecipientAlt, from me) the lid is known and the ignore list wins
	r2, _ := testReader(t, o)
	m := msg(pn, pn, "MSGNOLID3", "Fulano", "o concreto chega amanha")
	m.Info.SenderAlt = lid
	r2.handle(m)
	if got := spool(t, r2); len(got) != 0 || r2.sum.Ignoradas != 1 || r2.sum.SemLID != 0 {
		t.Fatalf("lid from SenderAlt: %+v %+v", got, r2.sum)
	}
	// with the mapping in the store, too
	r3, dev3 := testReader(t, o)
	if err := dev3.LIDs.PutLIDMapping(context.Background(), lid, pn); err != nil {
		t.Fatal(err)
	}
	r3.handle(msg(pn, pn, "MSGNOLID4", "Fulano", "bom dia"))
	if got := spool(t, r3); len(got) != 0 || r3.sum.Ignoradas != 1 || r3.sum.SemLID != 0 {
		t.Fatalf("mapped and ignored: %+v %+v", got, r3.sum)
	}
	if s := r3.finish(false); len(s.Avisos) != 0 {
		t.Fatalf("avisos = %+v", s.Avisos)
	}
	// with no @lid in ignorar nothing is lost by it: still a new chat
	for _, ig := range [][]string{nil, {groupTest.String()}, {"+55 0000000999"}, {"Fulano Teste"}} {
		r4, _ := testReader(t, allOpts(Options{Ignore: ig}))
		r4.handle(msg(pn, pn, "MSGNOLID5", "Fulano", "bom dia"))
		if got := spool(t, r4); len(got) != 1 || got[0].Candidata != "novo" || r4.sum.SemLID != 0 {
			t.Fatalf("ignore %v: %+v %+v", ig, got, r4.sum)
		}
	}
	// the number listed in contatos is still read
	r5, _ := testReader(t, allOpts(Options{Contatos: []string{"+55 0000000666"}, Ignore: []string{lid.String()}}))
	r5.handle(msg(pn, pn, "MSGNOLID6", "Fulano", "bom dia"))
	if got := spool(t, r5); len(got) != 1 || got[0].Candidata != "" || r5.sum.SemLID != 0 {
		t.Fatalf("listed number: %+v %+v", got, r5.sum)
	}
}

func TestAllModeLidInIgnorarSilencesTheNoNumberWarning(t *testing.T) {
	// a name in ignorar makes a chat with no number a drop; its @lid in ignorar makes it ignored, with no warning
	r, _ := testReader(t, allOpts(Options{Ignore: []string{"Fulano Teste", lidChat.String()}}))
	r.handle(msg(lidChat, lidChat, "MSGSIL1", "Fulano", "bom dia"))
	if got := spool(t, r); len(got) != 0 || r.sum.Ignoradas != 1 || r.sum.SemNumero != 0 {
		t.Fatalf("spool = %+v sum = %+v", got, r.sum)
	}
	if s := r.finish(false); len(s.Avisos) != 0 {
		t.Fatalf("avisos = %+v", s.Avisos)
	}
}
