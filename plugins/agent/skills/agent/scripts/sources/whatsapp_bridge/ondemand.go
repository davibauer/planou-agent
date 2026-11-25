package main

// On-demand history (PLN0985, mode "all"): a chat outside the list only reaches the spool with a signal of the
// objective or while it is new, so when the session judges it relevant the conversation before that verdict was
// already dropped. Instead of pairing again, the bridge asks the account's OWN phone for the messages of that chat
// before an anchor (whatsmeow's on-demand history sync). The phone only pages backward from the anchor, so the source
// anchors the first request at the NEWEST message of the chat it has (what was dropped sits between and before the
// candidates) and a repeated one at the OLDEST (the page before). The phone answers with a history block of type
// ON_DEMAND, handled like any other block (historyNotification) and written under its own origin, only for the chats
// whose history the source is waiting for (awaiting_history): any other chat in such a block is dropped.
//
// This is the bridge's only send, and it never reaches a person: a peer protocol message (category "peer") to the
// account's own primary device, the same channel the phone and its linked devices use to exchange history. The
// destination is always the own JID (ownPhone): sendToOwnPhone refuses any other, and SendPeerMessage takes no
// destination at all (whatsmeow addresses it to the own JID with Peer: true). It is sent only for chats the instance
// follows (the list plus the chats judged relevant), never for an ignored one, once per verdict (the Python source
// keeps track) and never retried in a loop.

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
)

// originOnDemand marks the spool lines that came from an on-demand history block (the answer to a request).
const originOnDemand = "sob_demanda"

const (
	defaultOnDemandCount = 50  // whatsmeow's recommended page
	maxOnDemandCount     = 200 // more than this per request is cut here (the schema refuses it too)
)

// HistoryRequest is one chat whose earlier messages are asked for, anchored at a message of that chat the instance
// has (from the spool, recorded with the "relevante" verdict: the newest on the first request, the oldest on a
// repeated one).
type HistoryRequest struct {
	Chat   string `json:"chat"`    // the chat as the spool line has it
	ID     string `json:"id"`      // the anchor: the phone sends the messages before it
	FromMe bool   `json:"from_me"` // whether the anchor was sent by the account
	TS     int64  `json:"ts"`      // the anchor's time, unix seconds
	Count  int    `json:"count"`   // messages before the anchor (default 50, at most 200)
}

var errNotOwnPhone = errors.New("destino diferente do proprio aparelho da conta")

// ownPhone is the account's own JID (the primary device, without the device part); empty when not paired.
func (r *reader) ownPhone() types.JID {
	if r.cli == nil || r.cli.Store == nil || r.cli.Store.ID == nil {
		return types.EmptyJID
	}
	return r.cli.Store.ID.ToNonAD()
}

// sendToOwnPhone is the default peerSend: it refuses any destination but the own JID and then hands the request to
// SendPeerMessage, which has no destination parameter (whatsmeow sends it to the own JID with Peer: true).
func (r *reader) sendToOwnPhone(ctx context.Context, to types.JID, m *waE2E.Message) error {
	own := r.ownPhone()
	if own.IsEmpty() || to != own {
		return errNotOwnPhone
	}
	_, err := r.cli.SendPeerMessage(ctx, m)
	return err
}

// followed: a chat the instance reads always (the list plus the relevant chats, which the source adds to chats) and
// that the ignore list does not name by id.
func (r *reader) followed(chat types.JID) bool {
	id := chat.String()
	if !r.match.chatJIDs[id] && !r.match.contactJIDs[id] {
		return false
	}
	return r.all == nil || (!r.all.ignore.chatJIDs[id] && !r.all.ignore.contactJIDs[id])
}

// requestHistory asks the own phone for the history of each requested chat. Nothing is sent for a request that is
// malformed, for a status/channel/broadcast, for a chat the instance does not follow or when not in mode "all".
// The chats asked are reported (pedidos_historico), so the source does not ask again for the same verdict.
func (r *reader) requestHistory(reqs []HistoryRequest) {
	if len(reqs) == 0 {
		return
	}
	if r.all == nil {
		r.warn("pedido de historico ignorado: so vale no modo todos")
		return
	}
	own := r.ownPhone()
	if own.IsEmpty() {
		r.warn("pedido de historico ignorado: aparelho nao pareado")
		return
	}
	for _, q := range reqs {
		chat, err := types.ParseJID(q.Chat)
		if err != nil || q.ID == "" || q.TS <= 0 {
			r.warn("pedido de historico sem chat, mensagem ou hora validos: ignorado")
			continue
		}
		chat = chat.ToNonAD()
		switch chat.Server {
		case types.GroupServer, types.DefaultUserServer, types.HiddenUserServer:
		default:
			r.warn("pedido de historico de status, canal ou lista de transmissao: ignorado")
			continue
		}
		if !r.followed(chat) {
			r.warn("pedido de historico de chat nao acompanhado (fora da lista e sem veredito relevante, ou em ignorar): " +
				"ignorado: " + chat.String())
			continue
		}
		n := q.Count
		if n <= 0 {
			n = defaultOnDemandCount
		} else if n > maxOnDemandCount {
			n = maxOnDemandCount
		}
		anchor := &types.MessageInfo{
			MessageSource: types.MessageSource{Chat: chat, IsFromMe: q.FromMe, IsGroup: chat.Server == types.GroupServer},
			ID:            q.ID, Timestamp: time.Unix(q.TS, 0)}
		m := r.cli.BuildHistorySyncRequest(anchor, n)
		ctx, cancel, _ := r.opCtx(20 * time.Second)
		err = r.peerSend(ctx, own, m)
		cancel()
		if err != nil {
			r.warn(fmt.Sprintf("pedido de historico ao proprio celular falhou (%s): o proximo tick tenta de novo", short(err.Error())))
			continue
		}
		r.mu.Lock()
		r.sum.Pedidos = append(r.sum.Pedidos, q.Chat)
		r.awaiting[chat.String()] = true // its answer is awaited even if the source did not list it
		r.mu.Unlock()
	}
}

// awaitOnDemand waits, within the run's deadline, until one ON_DEMAND block arrived for each request sent. What the
// phone sends later is queued by the server for this device and handled on a later tick. Returns a fatal message when
// one arrives meanwhile.
func (r *reader) awaitOnDemand(deadline time.Time) (string, bool) {
	for {
		r.mu.Lock()
		done := r.onDemandBlocks >= len(r.sum.Pedidos)
		r.mu.Unlock()
		if done {
			return "", false
		}
		if !time.Now().Before(deadline) {
			r.warn("o celular ainda nao mandou o historico pedido: chega num proximo tick (o celular precisa estar com rede)")
			return "", false
		}
		select {
		case msg := <-r.fatal:
			return msg, true
		case <-time.After(250 * time.Millisecond):
		}
	}
}

// isAwaited: one of the chat's addresses is a chat whose on-demand history the source is waiting for (asked in this
// run or in an earlier one and not answered yet).
func (r *reader) isAwaited(ids []string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return anyIn(r.awaiting, ids)
}

// historyBlock writes the allowed messages of one downloaded history block. origin is "historico" (pairing) or
// originOnDemand (the answer to a request); an on-demand block reports each of its awaited chats, even when nothing
// of it was written (all older than the limit), so the source knows the phone answered. A chat nobody asked for is
// neither written nor reported.
func (r *reader) historyBlock(blob *waHistorySync.HistorySync, origin string) bool {
	failed := false
	for _, conv := range blob.GetConversations() {
		chat, err := types.ParseJID(conv.GetID())
		if err != nil {
			continue
		}
		if origin == originOnDemand && r.isAwaited([]string{chat.ToNonAD().String()}) {
			r.mu.Lock()
			if r.sum.SobDemanda == nil {
				r.sum.SobDemanda = map[string]int{}
			}
			r.sum.SobDemanda[chat.ToNonAD().String()] += 0
			r.mu.Unlock()
		}
		for _, hm := range conv.GetMessages() {
			m, err := r.cli.ParseWebMessage(chat, hm.GetMessage())
			if err != nil {
				continue
			}
			if !r.message(m, origin) {
				failed = true
			}
		}
	}
	return !failed
}
