"""Is someone talking to the user? The gate of the copilot (v2): cues scored once per end of a turn of someone else.

A turn of someone else is a run of their lines that nobody else interrupts (a third person's "yeah" does not; any line
of the user does). The decision is taken DECIDE_S after the last line of the turn ends, once nobody else is still
mid-sentence:
  - a third person who takes the floor first cancels it (the question was for them, or they took it);
  - the same speaker going on moves it to the new end of the turn;
  - the user starting to answer does not stop it (the cards still help him answer).
Every end of turn is scored, not only the ones shaped like a question. At most one alert per turn.

Cues (all computed from what was said up to the decision, in English and Portuguese, without punctuation when the
live ASR drops it):
  named        the user's name in the turn, tolerant of how ASR spells it
  named_before someone named the user in the last NAMED_BEFORE_S and the user has not spoken since
  reply        the turn answers the user: the last substantive line before it is the user's, at most REPLY_S before
  you          a singular "you" (you / your / voce / vc / tu / teu / seu...) in the end of the turn
  group        the turn talks to everyone ("you guys", "anyone", "pessoal", "alguem", "voces")
  question     question form ("?", wh-word, inverted auxiliary, tag question; "como", "qual", "voce pode"...)
  request      an imperative or a request ("please", "can someone", "take a look", "da uma olhada", "me manda")
  open_floor   the floor is handed over ("and you?", "your side", "e voce?", "e ai", "alguma novidade")
  handoff      the end of an update in a round ("that's it from me", "who's next", "e isso", "quem mais")
  round        a daily where the user has not given his update yet (the next turn in the round may be his)
  topic        the turn mentions one of the user's topics: the topic map from the context (config, PRs, issues,
               lines of the minutes about the user) or a named thing the user brought up earlier in this meeting
               (the app passes the map only with gate_topics: measured, it added alerts without recall, so by default
               it goes to the model stage and here only the user's own named things count)
The score adds the weights of the cues (WEIGHTS) and fires at THRESHOLD (2) or more: the name (3), a reply to the
user (2), the floor handed over (2), his turn in a daily round (2) and one of his topics with a question or a request
(2) fire alone; "you" with a question or a request (1) and the name said shortly before (1) fire only together with
another cue; talking to the group (-1) lowers it. The weights are generic (no meeting data): tuned with
`meeting-copilot eval` on half of the recorded meetings and checked on the other half.
"""
import re

from . import brain

DECIDE_S = 1.2          # decide this long after the turn's last line ends (added alert delay)
REPLY_S = 10.0          # the user's last line ended at most this long before the turn: the turn answers the user
NAMED_BEFORE_S = 30.0
TAIL_WORDS = 40         # the question cues look at the end of the turn, not at a whole monologue
ROUND_WORDS = 40        # in a daily, the user "has not given his update" while he said fewer words than this
THRESHOLD = 2.0
COMMON_TOPIC = 4       # a topic the others already said this many times is the meeting's subject, not a cue for the user
FLOOR_DROP_S = 5.0     # live: someone else talking WITHOUT A BREAK after the turn has the floor (20 busy polls of
                       # 0.25 s: about 4.75 s from the first one)
WAIT_MAX_S = 15.0      # live: the longest wait for speech still on its way before deciding anyway

WEIGHTS = {'named': 3.0, 'named_before': 1.0, 'reply': 2.0, 'open_floor': 2.0, 'round_handoff': 2.0,
           'you_ask': 1.0, 'topic_ask': 2.0, 'group': -1.0}

ACCENTS = str.maketrans('áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÉÊÍÓÔÕÚÇ', 'aaaaaeeeeiiiiooooouuuucnAAAAEEIOOOUC')


def fold(text):
    """Lowercase, no accents, only letters, digits, apostrophes and single spaces."""
    s = (text or '').translate(ACCENTS).lower()
    return ' '.join(re.sub(r"[^a-z0-9' ]", ' ', s).split())


def _rx(*parts):
    return re.compile('|'.join(parts))


YOU = _rx(r"\b(you|your|yours|you're|youre|yourself|voce|vc|tu|te|teu|tua|teus|tuas|contigo|ce)\b",
          r"\b(o|a|do|da|no|na|pro|pra)\s+(seu|sua)\b")
GROUP = _rx(r"\b(you guys|you all|y'all|everyone|everybody|anyone|anybody|someone|somebody|all of you|guys|team)\b",
            r"\b(voces|pessoal|galera|alguem|todo mundo)\b")
QUESTION = _rx(
    # English
    r"\b(can|could|would|will|did|do|does|have|has|are|were|is|should|shall|may|might)\s+(you|we|i|it|they|he|she|this|that|there)\b",
    r"^(so |and |but |sorry |yeah |ok |okay |then |also )*(what|where|when|why|how|which|who|whose|is it|is there|any)\b",
    r"\b(what|where|when|why|how|which|who)\s+(is|are|was|were|do|does|did|about|if|would|should|can|could|will)\b",
    r"\b(right|correct|isn't it|aren't you|don't you|didn't you|no|yes or no|or not)$",
    r"\b(any (update|updates|blocker|blockers|news|questions|thoughts|idea|ideas)|do you know|what do you think)\b",
    # Portuguese (folded)
    r"^(e |mas |entao |ai |tipo |so )*(o que|que|qual|quais|quando|onde|como|quem|por que|porque|quanto|quantos|quantas|sera que)\b",
    r"\b(voce|vc|ce|tu)\s+(pode|poderia|consegue|conseguiu|conseguiria|acha|sabe|viu|tem|teve|ja|lembra|chegou|terminou|fez|vai|quer|precisa|testou|olhou|subiu|mandou|colocou)\b",
    r"\b(tem como|da pra|da para|sabe se|alguma (novidade|duvida|coisa|atualizacao|ideia)|algum (update|bloqueio|impedimento|ponto|problema))\b",
    r"\b(ne|certo|beleza|ok|confere|correto|sim ou nao|ou nao|nao tem|nao e|nao foi|nao ta|tudo bem|pode ser)$")
REQUEST = _rx(
    r"\b(please|can someone|could someone|can somebody|someone (needs|should|has) to|need (you|someone|somebody) to|make sure|"
    r"take a look|have a look|look into|let me know|let us know|send (me|us|it|the)|share (it|your|the|with)|check (it|this|that|with|if|the)|"
    r"follow up|walk (me|us) through|tell (me|us)|show (me|us)|explain)\b",
    r"\b(por favor|da uma olhada|de uma olhada|olha (isso|pra|para|ai)|verifica|confere|me (passa|manda|fala|diz|avisa|mostra|explica)|"
    r"manda (pra|para|ai)|compartilha|precisa(mos)? que (voce|vc|alguem)|alguem (pode|consegue|precisa|tem que)|"
    r"pode (ver|olhar|verificar|mandar|compartilhar|falar|mostrar|explicar|abrir|subir|testar))\b")
OPEN_FLOOR = _rx(
    r"\b(and you|what about you|how about you|your side|your end|your turn|over to you|anything from you|"
    r"anything on your|how is it going on your|what's new with you)\b",
    r"\b(e voce|e vc|e tu|teu lado|seu lado|sua vez|tua vez|fala ai|tua parte|sua parte|"
    r"alguma novidade|algum update|como (esta|ta) (o|a) (seu|sua|teu|tua))\b")
HANDOFF = _rx(
    r"\b(who's next|whos next|who is next|who wants to go next|next (one|person|up)|anyone else|i'm done|im done|"
    r"that's (it|all) (from|for|on) (me|my side)|thats (it|all) (from|for|on) (me|my side)|that's my update|nothing else from me|over to)\b",
    r"\b(quem (mais|e o proximo|e a proxima|vai agora|falta)|e isso (da|de) minha parte|era isso|so isso|por hoje e isso|"
    r"passo (a palavra|pra|para)|nada mais (da|de) minha parte|e o que eu tinha)\b")

# Capitalized words that are not things: function words that start a sentence when the ASR drops the punctuation,
# days and months. Language lists only.
COMMON = set("""i i'm i've i'll im ok okay yeah yes no so and but the a an we you he she they it this that there then
what when where why how who which can could would will did do does have has is are was were just also well right
now maybe because if or of to in on at for with from by about like actually basically mean think know see sorry
thanks thank hello hi hey good morning afternoon everyone guys let's lets one two three yesterday today tomorrow
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september
october november december english portuguese
eu voce vc ele ela nos eles elas a o os as um uma uns umas e mas ou se que de do da dos das em no na nos nas para pra
por com sem sim nao entao tipo ai aqui la isso esse essa este esta isto ja so tambem mais muito bom boa dia tarde
noite ontem hoje amanha gente pessoal obrigado obrigada beleza ta tá certo show legal agora depois antes acho sei
segunda terca quarta quinta sexta sabado domingo janeiro fevereiro marco abril maio junho julho agosto setembro
outubro novembro dezembro""".split())


def meeting_type(title, n_speakers=99):
    """daily | planning | interview | 1:1 | other, from the title (the agenda block or --title) and the number of people."""
    t = (title or '').lower().translate(ACCENTS)
    if n_speakers <= 2: return '1:1'
    if re.search(r'daily|stand ?-?up|status', t): return 'daily'
    if re.search(r'refin|planning|grooming|sprint|backlog', t): return 'planning'
    if re.search(r'entrevista|interview|vaga', t): return 'interview'
    return 'other'


def named_things(text):
    """Things named in a line: capitalized words, acronyms and words with digits ('Quokka', 'PR', 'V3', 'TKT-12'),
    folded, minus common words. Live ASR capitalizes names and products even without punctuation."""
    out = set()
    for tok in re.findall(r"[A-Za-zÀ-ÿ0-9][\wÀ-ÿ'\-]*", text or ''):
        if not (tok[0].isupper() or any(c.isdigit() for c in tok) or (len(tok) >= 2 and tok.isupper())): continue
        f = fold(tok).strip("'-")
        if (len(f) < 3 and not any(c.isdigit() for c in f)) or f in COMMON or f.isdigit(): continue
        out.add(f)
    return out


def topic_terms(text, aliases=()):
    """The topic map from a context text (the pack, minutes, a config list): the named things of the lines that are
    about the user (lines with an alias, and the sections of the user's PRs and issues), without the user's names."""
    keep, in_user_section = [], False
    for line in (text or '').splitlines():
        if line.startswith('#'):
            h = line.lower()
            in_user_section = ('by the user' in h or 'assigned to the user' in h or 'recent work' in h)
            continue
        if in_user_section or (aliases and brain.named(line, aliases)): keep.append(line)
    names = {fold(w) for a in aliases for w in a.split()}
    return {t for line in keep for t in named_things(line)} - names


def is_backchannel(text):
    w = fold(text).split()
    return 0 < len(w) <= 3 and all(x in BACKCHANNEL for x in w)


BACKCHANNEL = set("""yeah yes yep yup ya ok okay right sure uh huh um mhm mm hmm hm got it cool nice alright all good
great perfect exactly thanks thank you no nope oh ah i see true fine wow sim e isso beleza ta certo show perfeito
entendi aham uhum claro exato boa legal valeu obrigado obrigada pois nao verdade hum tranquilo otimo""".split())


def cues(turn_text, last_text, ctx):
    """The cues of one turn. turn_text: the whole turn; last_text: its last line. ctx: named, named_before, reply,
    round, topic (bools computed by the Detector). -> {cue: bool}."""
    tail = ' '.join(fold(last_text).split()[-TAIL_WORDS:])
    raw_tail = (last_text or '')[-300:]
    c = dict(ctx)
    c['you'] = bool(YOU.search(tail))
    c['group'] = bool(GROUP.search(tail))
    c['question'] = '?' in raw_tail.rstrip()[-120:] or bool(QUESTION.search(tail))
    c['request'] = bool(REQUEST.search(tail))
    c['open_floor'] = bool(OPEN_FLOOR.search(' '.join(tail.split()[-8:])))
    c['handoff'] = bool(HANDOFF.search(' '.join(tail.split()[-12:])))
    return c


def score(c, weights=None, threshold=None):
    """-> (fire, score, names of the cues that counted). The deterministic stage answers "is this turn for the user?";
    whether it really asks him something is left to the addressee model (addressee.py), so a reply to the user with no
    question shape still passes (Portuguese asks by intonation, which the transcript does not carry)."""
    w = weights or WEIGHTS
    th = THRESHOLD if threshold is None else threshold
    s, why = 0.0, []

    def add(name, k):
        nonlocal s
        s += w[k]; why.append(name)
    asks = c.get('question') or c.get('request') or c.get('open_floor')
    if c.get('named') or (c.get('named_in_turn') and asks): add('named', 'named')
    elif c.get('named_before') or c.get('named_in_turn'): add('named before', 'named_before')
    if c.get('reply'): add('reply to the user', 'reply')
    if c.get('open_floor'): add('floor handed over', 'open_floor')
    if c.get('round') and c.get('handoff'): add('his turn in the round', 'round_handoff')
    if c.get('you') and asks: add('asks "you"', 'you_ask')
    if c.get('topic') and asks: add('asks about his topic', 'topic_ask')
    if c.get('group') and not (c.get('named') or c.get('reply')): add('to the group', 'group')
    return s >= th, round(s, 2), why


def wait_step(floor_busy, asr_busy, floor_s, waited_s, step):
    """The live wait before a decision, one poll at a time. floor_s counts only CONTINUOUS speech of someone else
    (reset when the channel goes quiet: a blip or an "uhum" never adds up); a slow transcript only waits longer.
    -> ('wait' | 'drop' | 'decide', the new floor_s)."""
    floor_s = floor_s + step if floor_busy else 0.0
    if floor_s >= FLOOR_DROP_S: return 'drop', floor_s
    if (floor_busy or asr_busy) and waited_s < WAIT_MAX_S: return 'wait', floor_s
    return 'decide', floor_s


class Detector:
    """State of one meeting for the gate. Feed it every line as the app merges them (`seen`), then `decide` the
    pending turn when its time comes (live: a timer; eval: before the next line that starts after it is due).

    lines: the app's list of merged lines (dicts with who, me, text, start, end); the detector only reads it."""

    def __init__(self, aliases, topics=(), meeting_type='', decide_s=DECIDE_S, suppress=True, weights=None, threshold=None):
        self.aliases, self.topics = list(aliases), set(topics)
        self.type = meeting_type
        self.decide_s, self.suppress = decide_s, suppress
        self.weights, self.threshold = weights, threshold
        self.pending = None          # {'turn': first line index, 'last': last line index, 'due': media time, 'v': version}
        self.turn = None             # [first line index, speaker] of the current turn of someone else
        self.fired_turns = set()
        self.user_words = 0
        self.user_terms, self.other_terms = {}, {}
        self.version = 0
        self._counted = {}           # line index -> how much of its text was counted already (lines grow)

    def _count(self, lines, i):
        l = lines[i]
        done = self._counted.get(i, '')
        new = l['text'][len(done):] if l['text'].startswith(done) else l['text']
        self._counted[i] = l['text']
        terms = self.user_terms if l['me'] else self.other_terms
        for t in named_things(new): terms[t] = terms.get(t, 0) + 1
        if l['me']: self.user_words += len(new.split())

    def seen(self, lines):
        """The last line of `lines` is new or grew."""
        i = len(lines) - 1
        l = lines[i]
        self._count(lines, i)
        if l['me']:
            self.turn = None          # the user's line ends the turn of the other person; a pending decision stays
            return None
        if is_backchannel(l['text']) and not (self.turn and self.turn[1] == l['who']):
            return None               # a "yeah" neither ends a turn nor starts one, and is never decided alone
        if self.turn and self.turn[1] == l['who']:
            first = self.turn[0]
        else:
            if self.pending and self.suppress and self.pending['who'] != l['who']:
                self.pending = None   # a third person took the floor before the decision
            first = i; self.turn = [i, l['who']]
        if first in self.fired_turns: return None
        self.version += 1
        self.pending = {'turn': first, 'last': i, 'who': l['who'], 'due': l['end'] + self.decide_s, 'v': self.version}
        return self.pending

    def his_topics(self, terms):
        """The terms that are the user's: in the topic map and not yet everybody's subject in this meeting (others said
        it fewer than COMMON_TOPIC times, or no more than the user did), or brought up by the user himself here."""
        out = set()
        for t in terms:
            u, o = self.user_terms.get(t, 0), self.other_terms.get(t, 0)
            if (t in self.topics and (o < COMMON_TOPIC or u >= o)) or (u and u >= o): out.add(t)
        return out

    def decide(self, lines, version=None):
        """Score the pending turn. -> None when there is nothing (or a newer version) to decide, else
        {'fire', 'score', 'why', 'cues', 'turn', 'line'}."""
        p = self.pending
        if not p or (version is not None and p['v'] != version): return None
        self.pending = None
        idx = [k for k in range(p['turn'], p['last'] + 1) if lines[k]['who'] == p['who']]
        turn_text = ' '.join(lines[k]['text'] for k in idx)
        last = lines[idx[-1]]
        start = lines[idx[0]]['start']
        lw = last['text'].split()
        ctx = {'named': brain.named(' '.join(lw[:6] + lw[-10:]), self.aliases), 'named_in_turn': brain.named(turn_text, self.aliases),
               'named_before': False, 'reply': False,
               'round': self.type == 'daily' and self.user_words < ROUND_WORDS, 'topic': False}
        for h in reversed(lines[:idx[0]]):
            if start - h['end'] > max(NAMED_BEFORE_S, REPLY_S): break
            if h['me']: break                               # named before only while the user has not spoken since
            if brain.named(h['text'], self.aliases): ctx['named_before'] = True; break
        prev = next((h for h in reversed(lines[:idx[0]]) if h['me'] or not is_backchannel(h['text'])), None)
        if prev and prev['me'] and not is_backchannel(prev['text']) and start - prev['end'] <= REPLY_S: ctx['reply'] = True
        ctx['topic'] = bool(self.his_topics(named_things(turn_text)))
        c = cues(turn_text, last['text'], ctx)
        fire, s, why = score(c, self.weights, self.threshold)
        if fire: self.fired_turns.add(p['turn'])
        return {'fire': fire, 'score': s, 'why': why, 'cues': c, 'turn': p['turn'], 'line': idx[-1]}
