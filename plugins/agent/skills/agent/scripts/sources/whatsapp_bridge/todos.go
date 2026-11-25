package main

// The "all contacts" mode (mode "all", PLN0972): every chat may be read, and a local filter, with no model, decides
// what is written. No connection here (todos_test.go covers it).
//
// Order for each message, before anything reaches the spool:
//  1. ignore list (chats/contacts never read: by id, number, address book name or group subject) -> dropped, counted
//  2. the allowlist (chats + contatos) plus the chats judged relevant -> written as usual, attachments included
//  3. muted chats (too many "irrelevant" verdicts) -> dropped
//  4. candidate: a signal of the instance's objective in the message (text, group subject, sender name, file name,
//     phone) or a new chat (not known when the mode started and with no verdict yet) -> written, marked, attachment
//     NOT downloaded
//  5. everything else -> dropped: no text, no attachment, only the counter
//
// The known chats are kept as keyed hashes (HMAC-SHA256 with a per-instance salt), never as the ids themselves.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"go.mau.fi/whatsmeow/types"
)

// Signals are the words of the instance's objective (the instance's signals file, read by the Python source). The keys
// follow the format the instances already use for e-mail; e-mail only keys are not sent here.
type Signals struct {
	Names           []string `json:"names"`            // people and companies: whole words, accents and case ignored
	Words           []string `json:"words"`            // keywords_anywhere + subject_words: whole words, accents and case ignored
	Acronyms        []string `json:"acronyms"`         // whole words, case kept
	Domains         []string `json:"domains"`          // a site or e-mail domain written in the message
	AttachmentWords []string `json:"attachment_words"` // a piece of the attachment's file name
	Phones          []string `json:"phones"`           // a sender or chat with this number (digits)
}

func (s Signals) empty() bool {
	return len(s.Names)+len(s.Words)+len(s.Acronyms)+len(s.Domains)+len(s.AttachmentWords)+len(s.Phones) == 0
}

// allMode is the compiled state of the mode for one run.
type allMode struct {
	ignore   *matcher
	judged   map[string]bool // chat ids with an "irrelevant" verdict: no longer candidates for being new
	muted    map[string]bool // chat ids muted by verdicts: never read
	known    map[string]bool // keyed hashes of the chats known when the mode started
	salt     string
	baseline bool // the mode's first run: nothing is new; every chat seen goes to the known set
	signals  Signals
}

func newAllMode(o Options) *allMode {
	if o.Mode != "all" {
		return nil
	}
	return &allMode{ignore: newMatcher(Options{Chats: o.Ignore, Contatos: o.Ignore}), judged: set(o.Judged),
		muted: set(o.Muted), known: set(o.Known), salt: o.Salt, baseline: o.Baseline, signals: o.Signals}
}

func set(xs []string) map[string]bool {
	m := map[string]bool{}
	for _, x := range xs {
		if x = strings.TrimSpace(x); x != "" {
			m[x] = true
		}
	}
	return m
}

func anyIn(m map[string]bool, ids []string) bool {
	for _, id := range ids {
		if id != "" && m[id] {
			return true
		}
	}
	return false
}

// hashID is the stored form of a known chat: HMAC-SHA256(salt, id), 16 hex digits.
func hashID(salt, id string) string {
	h := hmac.New(sha256.New, []byte(salt))
	h.Write([]byte(id))
	return hex.EncodeToString(h.Sum(nil))[:16]
}

func (a *allMode) isKnown(ids []string) bool {
	for _, id := range ids {
		if id != "" && a.known[hashID(a.salt, id)] {
			return true
		}
	}
	return false
}

// ignoredGroup: decided=false when the ignore list names groups by subject and this group's subject is not known in
// this run (fail closed: the message is left unacked, never written).
func (a *allMode) ignoredGroup(jid, subject string, subjectKnown bool) (ignored, decided bool) {
	if a.ignore.chatJIDs[jid] {
		return true, true
	}
	if len(a.ignore.chatNames) == 0 {
		return false, true
	}
	if !subjectKnown {
		return false, false
	}
	return subject != "" && a.ignore.chatNames[subject], true
}

func (a *allMode) ignoredDirect(ids, users, names []string) bool {
	return a.ignore.direct(ids, users, names)
}

// ignoreNeedsNumber: the ignore list has phone numbers, address book names or ...@s.whatsapp.net ids, which a 1:1 chat
// known only by its @lid cannot be checked against. Group ids, subjects and @lid ids need no number.
func (a *allMode) ignoreNeedsNumber() bool {
	if len(a.ignore.contactUsers)+len(a.ignore.contactNames) > 0 {
		return true
	}
	for j := range a.ignore.contactJIDs {
		if strings.HasSuffix(j, "@"+types.DefaultUserServer) {
			return true
		}
	}
	return false
}

// ignoreHasLID: the ignore list has @lid ids, which a 1:1 chat by number whose @lid the session does not know (no
// SenderAlt/RecipientAlt, no stored mapping) cannot be checked against.
func (a *allMode) ignoreHasLID() bool {
	for j := range a.ignore.contactJIDs {
		if strings.HasSuffix(j, "@"+types.HiddenUserServer) {
			return true
		}
	}
	return false
}

// isNew: a chat that was not known when the mode started and has no "irrelevant" verdict yet.
func (a *allMode) isNew(ids []string) bool {
	return !a.baseline && !a.isKnown(ids) && !anyIn(a.judged, ids)
}

// accents folded to plain letters (the Portuguese and Spanish ones), so "Joao" matches "João"
var foldAccents = strings.NewReplacer(
	"á", "a", "à", "a", "â", "a", "ã", "a", "ä", "a", "å", "a",
	"é", "e", "è", "e", "ê", "e", "ë", "e",
	"í", "i", "ì", "i", "î", "i", "ï", "i",
	"ó", "o", "ò", "o", "ô", "o", "õ", "o", "ö", "o",
	"ú", "u", "ù", "u", "û", "u", "ü", "u",
	"ç", "c", "ñ", "n", "ý", "y", "ÿ", "y")

func fold(s string) string { return foldAccents.Replace(strings.ToLower(s)) }

func alnum(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// hasWord: term appears in text as a whole word (no letter or digit right before or after it).
func hasWord(text, term string) bool {
	if term == "" {
		return false
	}
	for i := 0; ; {
		j := strings.Index(text[i:], term)
		if j < 0 {
			return false
		}
		start, end := i+j, i+j+len(term)
		if (start == 0 || !alnum(text[start-1])) && (end == len(text) || !alnum(text[end])) {
			return true
		}
		i = start + 1
	}
}

// hits lists which signals a message carries: "nome X", "palavra X", "sigla X", "dominio X", "anexo X", "telefone".
// text is the message, subject the group's subject, senders the names of who wrote (address book and push name: a
// signal only puts the chat in front of the session, it never opens it), file the attachment's name, users the phone
// numbers of the chat and of the sender.
func (s Signals) hits(text, subject string, senders []string, file string, users []string) []string {
	if s.empty() {
		return nil
	}
	raw := strings.Join(append([]string{text, subject}, senders...), "\n")
	low := fold(raw)
	out := []string{}
	for _, n := range s.Names {
		if n = strings.TrimSpace(n); n != "" && hasWord(low, fold(n)) {
			out = append(out, "nome "+n)
		}
	}
	for _, w := range s.Words {
		if w = strings.TrimSpace(w); w != "" && hasWord(low, fold(w)) {
			out = append(out, "palavra "+w)
		}
	}
	for _, k := range s.Acronyms {
		if k = strings.TrimSpace(k); k != "" && hasWord(raw, k) {
			out = append(out, "sigla "+k)
		}
	}
	for _, d := range s.Domains {
		if d = strings.TrimSpace(strings.ToLower(d)); d != "" && hasWord(low, d) {
			out = append(out, "dominio "+d)
		}
	}
	if f := fold(file); f != "" {
		for _, w := range s.AttachmentWords {
			if w = strings.TrimSpace(w); w != "" && strings.Contains(f, fold(w)) {
				out = append(out, "anexo "+w)
			}
		}
	}
	for _, p := range s.Phones {
		d := nonDigit.ReplaceAllString(p, "")
		for _, u := range users {
			if d != "" && u == d {
				out = append(out, "telefone")
				break
			}
		}
	}
	return out
}
