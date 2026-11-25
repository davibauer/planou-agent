package main

// On-demand history (ondemand.go): the request goes only to the account's own phone, only for a followed chat, and the
// answer goes through the mode "all" filter under its own origin. Never connected; fake ids only.

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

type sentPeer struct {
	to types.JID
	m  *waE2E.Message
}

// capture swaps the reader's sender: nothing reaches the network, every call is recorded.
func capture(r *reader) *[]sentPeer {
	var got []sentPeer
	r.peerSend = func(_ context.Context, to types.JID, m *waE2E.Message) error {
		got = append(got, sentPeer{to, m})
		return nil
	}
	return &got
}

func TestHistoryRequestGoesOnlyToTheOwnPhone(t *testing.T) {
	r, dev := testReader(t, allOpts(Options{Chats: []string{friend.String()}}))
	dev.ID = &types.JID{User: "550000000099", Server: types.DefaultUserServer, Device: 7} // this linked device
	got := capture(r)
	ts := time.Date(2026, 1, 5, 9, 1, 0, 0, time.UTC).Unix()
	r.requestHistory([]HistoryRequest{{Chat: friend.String(), ID: "MSGANCHOR1", FromMe: true, TS: ts, Count: 30}})
	if len(*got) != 1 {
		t.Fatalf("sent %d requests, want 1 (avisos %v)", len(*got), r.sum.Avisos)
	}
	s := (*got)[0]
	own := types.JID{User: "550000000099", Server: types.DefaultUserServer}
	if s.to != own || s.to == friend || s.to.Device != 0 {
		t.Fatalf("destination %v, want only the own JID %v (never the contact, never a device)", s.to, own)
	}
	p := s.m.GetProtocolMessage()
	if p.GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE {
		t.Fatalf("type %v", p.GetType())
	}
	req := p.GetPeerDataOperationRequestMessage()
	if req.GetPeerDataOperationRequestType() != waE2E.PeerDataOperationRequestType_HISTORY_SYNC_ON_DEMAND {
		t.Fatalf("request type %v", req.GetPeerDataOperationRequestType())
	}
	h := req.GetHistorySyncOnDemandRequest()
	if h.GetChatJID() != friend.String() || h.GetOldestMsgID() != "MSGANCHOR1" || !h.GetOldestMsgFromMe() ||
		h.GetOnDemandMsgCount() != 30 || h.GetOldestMsgTimestampMS() != ts {
		t.Fatalf("anchor %+v", h)
	}
	// nothing else rides along: no text, no reaction, nothing a person could see
	if s.m.GetConversation() != "" || s.m.GetExtendedTextMessage() != nil || s.m.GetReactionMessage() != nil {
		t.Fatalf("message carries more than the request: %v", s.m)
	}
	if len(r.sum.Pedidos) != 1 || r.sum.Pedidos[0] != friend.String() {
		t.Fatalf("pedidos = %v", r.sum.Pedidos)
	}
}

func TestDefaultSenderRefusesAnyDestinationButTheOwnPhone(t *testing.T) {
	r, dev := testReader(t, allOpts(Options{Chats: []string{friend.String()}}))
	m := r.cli.BuildHistorySyncRequest(&types.MessageInfo{MessageSource: types.MessageSource{Chat: friend}, ID: "X",
		Timestamp: time.Now()}, 10)
	group := types.JID{User: "120363000000000001", Server: types.GroupServer}
	device := types.JID{User: dev.ID.User, Server: dev.ID.Server, Device: 3}
	for _, to := range []types.JID{friend, stranger, group, device, types.EmptyJID} {
		if err := r.sendToOwnPhone(context.Background(), to, m); !errors.Is(err, errNotOwnPhone) {
			t.Fatalf("sendToOwnPhone(%v) = %v, want refused", to, err)
		}
	}
	dev.ID = nil // not paired: there is no own JID, so nothing is sent at all
	if err := r.sendToOwnPhone(context.Background(), types.EmptyJID, m); !errors.Is(err, errNotOwnPhone) {
		t.Fatalf("unpaired: %v", err)
	}
}

func TestHistoryRequestIsNotSentForChatsTheInstanceDoesNotFollow(t *testing.T) {
	ignored := types.JID{User: "550000000103", Server: types.DefaultUserServer}
	ts := time.Now().Add(-time.Hour).Unix()
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String(), ignored.String(), "status@broadcast"},
		Ignore: []string{ignored.String()}}))
	got := capture(r)
	r.requestHistory([]HistoryRequest{
		{Chat: stranger.String(), ID: "A", TS: ts},            // outside the list, no relevant verdict
		{Chat: ignored.String(), ID: "B", TS: ts},             // in ignorar: wins over everything
		{Chat: "status@broadcast", ID: "C", TS: ts},           // status: never read
		{Chat: "not a jid", ID: "D", TS: ts},                  // malformed
		{Chat: friend.String(), ID: "", TS: ts},               // no anchor
		{Chat: friend.String(), ID: "E", TS: 0},               // no anchor time
		{Chat: friend.String(), ID: "F", TS: ts},              // the one that goes, with the default count
		{Chat: friend.String(), ID: "G", TS: ts, Count: 5000}, // cut to the maximum
	})
	if len(*got) != 2 {
		t.Fatalf("sent %d, want 2 (avisos %v)", len(*got), r.sum.Avisos)
	}
	for _, s := range *got {
		if s.m.GetProtocolMessage().GetPeerDataOperationRequestMessage().GetHistorySyncOnDemandRequest().GetChatJID() != friend.String() {
			t.Fatalf("request for %v", s.m)
		}
	}
	counts := []int32{50, 200}
	for i, s := range *got {
		if c := s.m.GetProtocolMessage().GetPeerDataOperationRequestMessage().GetHistorySyncOnDemandRequest().GetOnDemandMsgCount(); c != counts[i] {
			t.Fatalf("count %d, want %d", c, counts[i])
		}
	}
	if !strings.Contains(strings.Join(r.sum.Avisos, "\n"), "nao acompanhado") {
		t.Fatalf("avisos %v", r.sum.Avisos)
	}

	list, _ := testReader(t, Options{Chats: []string{friend.String()}}) // mode "lista": never asks
	sent := capture(list)
	list.requestHistory([]HistoryRequest{{Chat: friend.String(), ID: "H", TS: ts}})
	if len(*sent) != 0 || len(list.sum.Pedidos) != 0 {
		t.Fatalf("list mode sent %v", *sent)
	}
}

func TestFailedHistoryRequestIsNotReportedAsAsked(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}}))
	r.peerSend = func(context.Context, types.JID, *waE2E.Message) error { return errors.New("sem conexao") }
	r.requestHistory([]HistoryRequest{{Chat: friend.String(), ID: "A", TS: time.Now().Unix()}})
	if len(r.sum.Pedidos) != 0 || len(r.sum.Avisos) != 1 {
		t.Fatalf("pedidos %v avisos %v", r.sum.Pedidos, r.sum.Avisos)
	}
}

func webMsg(chat types.JID, id, text string, age time.Duration) *waHistorySync.HistorySyncMsg {
	return &waHistorySync.HistorySyncMsg{Message: &waWeb.WebMessageInfo{
		Key:              &waCommon.MessageKey{RemoteJID: proto.String(chat.String()), FromMe: proto.Bool(false), ID: proto.String(id)},
		MessageTimestamp: proto.Uint64(uint64(time.Now().Add(-age).Unix())),
		Message:          &waE2E.Message{Conversation: proto.String(text)},
	}}
}

func onDemandBlob(convs map[types.JID][]*waHistorySync.HistorySyncMsg) *waHistorySync.HistorySync {
	b := &waHistorySync.HistorySync{SyncType: waHistorySync.HistorySync_ON_DEMAND.Enum()}
	for chat, ms := range convs {
		b.Conversations = append(b.Conversations, &waHistorySync.Conversation{ID: proto.String(chat.String()), Messages: ms})
	}
	return b
}

func TestOnDemandBlockIsWrittenPastThePairingWindowWithItsOwnOrigin(t *testing.T) {
	day := 24 * time.Hour
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, HistoryDays: 3, OnDemandDays: 30,
		AwaitingHistory: []string{friend.String()}}))
	blob := onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{
		friend: {webMsg(friend, "OD10", "condicoes de pagamento", 10*day), webMsg(friend, "OD40", "muito antiga", 40*day)},
		// another chat in the answer (never asked): dropped, never a candidate, even though it is new
		stranger: {webMsg(stranger, "OD99", "outro assunto", day)},
	})
	if !r.historyBlock(blob, originOnDemand) {
		t.Fatal("a decided block is acked")
	}
	got := spool(t, r)
	if len(got) != 1 || got[0].ID != "OD10" || got[0].Origem != originOnDemand || got[0].Candidata != "" {
		t.Fatalf("spool = %+v", got)
	}
	if r.sum.SobDemanda[friend.String()] != 1 || r.sum.Historico != 0 || r.sum.Candidatas != 0 {
		t.Fatalf("sob_demanda %v historico %d candidatas %d", r.sum.SobDemanda, r.sum.Historico, r.sum.Candidatas)
	}
	if _, ok := r.sum.SobDemanda[stranger.String()]; ok || len(r.sum.SobDemanda) != 1 {
		t.Fatalf("a chat nobody asked for is reported: %v", r.sum.SobDemanda)
	}
	old, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, OnDemandDays: 30,
		AwaitingHistory: []string{friend.String()}}))
	old.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{friend: {webMsg(friend, "OD50", "x", 50*day)}}), originOnDemand)
	if n, ok := old.sum.SobDemanda[friend.String()]; !ok || n != 0 {
		t.Fatalf("an awaited chat is reported as answered even with nothing written: %v", old.sum.SobDemanda)
	}

	// the same messages as pairing history: the short window of mode "all" cuts them (that is why the origin differs)
	p, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, HistoryDays: 3, OnDemandDays: 30}))
	p.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{friend: {webMsg(friend, "OD10", "x", 10*day)}}), "historico")
	if got := spool(t, p); len(got) != 0 {
		t.Fatalf("pairing history past historico_dias written: %+v", got)
	}
}

func TestOnDemandBlockDropsAListedChatWhoseHistoryWasNotAsked(t *testing.T) {
	other := types.JID{User: "550000000777", Server: types.DefaultUserServer}
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String(), other.String(), groupTest.String()},
		OnDemandDays: 30, AwaitingHistory: []string{friend.String()}}))
	r.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{
		friend: {webMsg(friend, "A1", "pedido", time.Hour)},
		other:  {webMsg(other, "B1", "nao pedido", time.Hour)}, // followed (in the list), but its history was not asked
	}), originOnDemand)
	got := spool(t, r)
	if len(got) != 1 || got[0].ID != "A1" {
		t.Fatalf("spool = %+v", got)
	}
	if _, ok := r.sum.SobDemanda[other.String()]; ok || r.sum.Descartadas != 1 {
		t.Fatalf("sob_demanda %v descartadas %d", r.sum.SobDemanda, r.sum.Descartadas)
	}
	// nothing awaited at all (no request open): the whole block is dropped
	none, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, OnDemandDays: 30}))
	none.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{friend: {webMsg(friend, "A2", "x", time.Hour)}}), originOnDemand)
	if got := spool(t, none); len(got) != 0 || len(none.sum.SobDemanda) != 0 {
		t.Fatalf("unawaited block written: %+v %v", got, none.sum.SobDemanda)
	}
}

func TestARequestSentInThisRunAwaitsItsAnswer(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, OnDemandDays: 30}))
	capture(r)
	r.requestHistory([]HistoryRequest{{Chat: friend.String(), ID: "A", TS: time.Now().Unix()}})
	r.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{friend: {webMsg(friend, "A0", "x", time.Hour)}}), originOnDemand)
	if got := spool(t, r); len(got) != 1 || r.sum.SobDemanda[friend.String()] != 1 {
		t.Fatalf("spool %+v sob_demanda %v", got, r.sum.SobDemanda)
	}
}

func TestOnDemandBlockByTheOtherIdOfTheChatIsReportedUnderTheAwaitedOne(t *testing.T) {
	// the verdict was given by the number and the phone answers by the @lid (or the other way): with the mapping in the
	// session the answer is reported under the awaited id, even when nothing of it is written (all past the limit)
	day := 24 * time.Hour
	lid := types.JID{User: "100000000000055", Server: types.HiddenUserServer}
	for _, c := range []struct {
		name            string
		awaited, answer types.JID
	}{{"number awaited, @lid answers", friend, lid}, {"@lid awaited, number answers", lid, friend}} {
		r, dev := testReader(t, allOpts(Options{Chats: []string{c.awaited.String()}, OnDemandDays: 30,
			AwaitingHistory: []string{c.awaited.String()}}))
		if err := dev.LIDs.PutLIDMapping(context.Background(), lid, friend); err != nil {
			t.Fatal(err)
		}
		r.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{
			c.answer: {webMsg(c.answer, "OLD1", "x", 50*day)}}), originOnDemand)
		if n, ok := r.sum.SobDemanda[c.awaited.String()]; !ok || n != 0 || len(r.sum.SobDemanda) != 1 {
			t.Fatalf("%s: sob_demanda = %v", c.name, r.sum.SobDemanda)
		}
		if got := spool(t, r); len(got) != 0 {
			t.Fatalf("%s: past the limit written: %+v", c.name, got)
		}
	}
	// without the mapping the other id is not known: nothing reported (the source warns after a day, as before)
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, OnDemandDays: 30,
		AwaitingHistory: []string{friend.String()}}))
	r.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{lid: {webMsg(lid, "OLD2", "x", 50*day)}}), originOnDemand)
	if len(r.sum.SobDemanda) != 0 {
		t.Fatalf("unmapped @lid reported: %v", r.sum.SobDemanda)
	}
}

func TestOnDemandBlockStillHonoursTheIgnoreList(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{Chats: []string{friend.String()}, Ignore: []string{friend.String()}, OnDemandDays: 30,
		AwaitingHistory: []string{friend.String()}}))
	r.historyBlock(onDemandBlob(map[types.JID][]*waHistorySync.HistorySyncMsg{friend: {webMsg(friend, "OD1", "x", time.Hour)}}), originOnDemand)
	if got := spool(t, r); len(got) != 0 || r.sum.Ignoradas != 1 {
		t.Fatalf("ignored chat written from an on-demand block: %+v", got)
	}
}

func TestAwaitOnDemandStopsWhenEveryRequestWasAnswered(t *testing.T) {
	r, _ := testReader(t, allOpts(Options{}))
	r.sum.Pedidos = []string{friend.String()}
	r.onDemandBlocks = 1
	start := time.Now()
	if _, bad := r.awaitOnDemand(start.Add(5 * time.Second)); bad || time.Since(start) > time.Second {
		t.Fatal("answered: no wait")
	}
	r.onDemandBlocks = 0
	if _, bad := r.awaitOnDemand(time.Now().Add(300 * time.Millisecond)); bad {
		t.Fatal("no answer is not fatal")
	}
	if !strings.Contains(strings.Join(r.sum.Avisos, "\n"), "proximo tick") {
		t.Fatalf("avisos %v", r.sum.Avisos)
	}
}
