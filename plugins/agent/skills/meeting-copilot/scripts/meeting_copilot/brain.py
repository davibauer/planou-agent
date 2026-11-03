"""What the copilot does with the transcript: spot questions to the user, suggest what to say, keep the notes.

Cards: the gate (detect.py: cues scored once per end of a turn of someone else, then the addressee model in
addressee.py) picks the turns for the user, and ONE model call writes the cards (Topics / Answer / Careful / Ask, each with
the ids of the context it is based on); without the addressee model that call also confirms the turn is for the user.
`candidate` below is the old gate (v1), kept for `meeting-copilot eval --gate v1`. Notes: every ~60 s a separate call updates
Decisions / Actions / Open questions with the new lines, plus FYI items for the "Everything" mode.
"""
import difflib, re

DIRECT_S = 90.0     # the user spoke this recently: an unnamed "you" is probably for them
NAMED_S = 30.0      # the user was named this recently

Q_RE = re.compile(r"""\b(can|could|would|will|did|do|does|have|has|are|were|should|shall)\s+(you|we)\b
    |\byou\s+(can|could|should|need|have|did|think|mean|said|changed|added|use)\b
    |^\W*(so\s+|and\s+|but\s+|sorry,?\s+|yeah,?\s+)?(what|where|when|why|how|which|who|is\s+it|is\s+there|any)\b
    |\b(please|show\s+me|tell\s+me|let\s+me\s+know|walk\s+me|explain|any\s+(update|blocker|news)s?)\b""", re.I | re.X)
DASHES = re.compile(r'\s*[—–]\s*')


def norm(s):
    return re.sub(r'[^a-z0-9 ]', ' ', s.lower())


def named(text, aliases):
    """The user's name in the text, tolerant of how ASR spells it ('Danaa' for an alias 'Dana')."""
    toks = norm(text).split()
    for a in aliases:
        al = norm(a).split()
        if not al: continue
        if len(al) > 1 and ' '.join(al) in ' '.join(toks): return True
        for t in toks:
            if t == al[0] or (len(t) >= 4 and len(al[0]) >= 4 and difflib.SequenceMatcher(None, t, al[0]).ratio() >= 0.8):
                return True
    return False


def looks_like_question(text):
    return '?' in text or bool(Q_RE.search(text))


def candidate(line, history, aliases):
    """Is this line worth a model call? line/history: dicts with who, me, text, start, end. -> (bool, reason)."""
    if line['me']: return False, 'user'
    text = line['text']
    if len(text.split()) < 3 and '?' not in text: return False, 'short'
    if not looks_like_question(text): return False, 'no question'
    if named(text, aliases): return True, 'named'
    for h in reversed(history):
        if line['start'] - h['end'] > max(DIRECT_S, NAMED_S): break
        if h['me'] and line['start'] - h['end'] <= DIRECT_S: return True, 'user spoke recently'
        if not h['me'] and named(h['text'], aliases) and line['start'] - h['end'] <= NAMED_S: return True, 'named before'
    return False, 'not for the user'


def clean(s):
    return DASHES.sub(', ', (s or '').strip()).replace(' ,', ',')


def mmss(t):
    t = max(0, int(t)); h, r = divmod(t, 3600)
    return (f'{h}:' if h else '') + f'{r // 60:02d}:{r % 60:02d}'


def transcript_block(lines, since=None, limit_chars=6000):
    out = [f"[{mmss(l['start'])}] {l['who']}: {l['text']}" for l in lines if since is None or l['end'] >= since]
    s = '\n'.join(out)
    return s[-limit_chars:]


def system_prompt(user, aliases, pack_text):
    return f"""You are a live meeting copilot for {user} (also called {', '.join(aliases)}). In the transcript the user is "You".
Other people appear by name or as "Speaker N". The transcript comes from live speech recognition: expect wrong words,
cut sentences and names spelled in different ways.

When someone asks the user something, you suggest what the user can say. Write as the user speaks: plain, direct,
short, first person, in the language of the meeting. "I think", "as far as I can see" are fine. Never use em dashes,
filler, praise or "great question". Use only facts from the context below or from the transcript, never invented details.
When the context has a concrete fact that answers the question (a table, a column, a migration file, a PR, a service),
the answer states it plainly ("I added two columns to the orders table in the shop service, migration V3_2_0, in
shop-api#12") and cites that PR; this beats "I'll check". Read the user's recent PRs and their schema changes before
saying the user did not change something. For any count or list of tables, columns, fields or indexes, use the "schema
facts" lines of the PRs as written; never count from the raw SQL or take a number from older notes. Only when the context really does not have it, the answer says honestly
what the user can say (for example that they will check X and come back).

Every card cites what it is based on, with the ids used in the context: memory:<slug>, ata:<folder>, <repo>#<n>,
<JIRA-KEY>, context, handoff, or transcript.

# Context about the user's work
{pack_text}
"""


def card_prompt(line, lines, memories, recent_groups, mode, language='English', hint='', decided=False):
    mem = '\n\n'.join(f'### memory:{s}\n{t}' for s, t in memories) or '(none)'
    prev = '\n'.join(f'- [{mmss(g["t"])}] {g["q"]}' for g in recent_groups) or '(none)'
    decide = ('A separate check already decides whether the candidate line is for the user: take it as directed to the '
              'user and write DIRECTED: yes.' if decided else DECIDE)
    return f"""Meeting transcript (most recent last):
{transcript_block(lines, since=line['start'] - 180)}

Candidate line: [{mmss(line['start'])}] {line['who']}: "{line['text']}"{(' (' + hint + ')') if hint else ''}

Possibly relevant memories:
{mem}

Questions to the user already handled in the last minutes:
{prev}

{decide}
The meeting is in {language}: write QUESTION and the cards in {language}, even when the context is in another language.

Reply in exactly this line format, nothing else, one field per line, the answer first:
DIRECTED: yes or no
QUESTION: the question in at most 15 words, in the meeting language
TOPICS: 2 or 3 talking points for the answer, separated by " ; ", each at most 7 words, notes not sentences (the user
speaks in his own words; never a script to read)
TOPICS_BASED: comma-separated ids
ANSWER: the same answer as one ready sentence, only for when the user asks for it, at most 30 words
ANSWER_WHY: at most 12 words
ANSWER_BASED: comma-separated ids
CAREFUL: only if there is a real risk (something not done or not merged, a promise to avoid, a conflict with a decision), at most 25 words
CAREFUL_WHY: at most 12 words
CAREFUL_BASED: ids
ASK: only if useful, a short question the user can ask back, at most 20 words
ASK_WHY: at most 12 words
ASK_BASED: ids
When DIRECTED is no, stop after that line. Leave out the CAREFUL and ASK lines when they add nothing.
Reply immediately with the lines. Do not think or deliberate first: the user is waiting in the meeting.{' Mode: only questions to the user.' if mode == 'mine' else ''}"""


DECIDE = """Decide if the candidate line asks the USER something (a question, a request for information, an update, an action
or an opinion), by name or by "you" while talking with the user. Not directed: questions to someone else, to the
whole group about their own work, rhetorical ones, or a repeat of a question already handled with nothing new.
Speaker labels come from voice matching and can be wrong: one person may show up as several "Speaker N". When the user
was just talking or presenting, a question that follows is most likely for the user."""

FIELD = re.compile(r'^\s*\**\s*(DIRECTED|QUESTION|TOPICS|TOPICS_WHY|TOPICS_BASED|ANSWER|ANSWER_WHY|ANSWER_BASED|CAREFUL|CAREFUL_WHY|CAREFUL_BASED|ASK|ASK_WHY|ASK_BASED)\s*\**\s*:\s*(.*)$', re.I)


def parse_cards(text, final=False):
    """Line format -> {'directed': bool|None, 'question': str, 'cards': [complete cards]}. While streaming, only lines
    already ended by a newline count, and a card is complete when its _BASED line is (or, at the end, its text)."""
    rows = text.split('\n')
    if not final: rows = rows[:-1]
    f = {}
    for r in rows:
        m = FIELD.match(r)
        if m: f[m.group(1).upper()] = m.group(2).strip()
    d = f.get('DIRECTED', '').lower()
    out = {'directed': True if d.startswith('y') else (False if d.startswith('n') else None),
           'question': f.get('QUESTION', ''), 'cards': []}
    for k in ('TOPICS', 'ANSWER', 'CAREFUL', 'ASK'):
        if k in f and (k + '_BASED' in f or final) and f[k] and f[k].lower() not in ('none', 'n/a', '-'):
            out['cards'].append({'kind': k.lower(), 'text': f[k], 'why': f.get(k + '_WHY', ''),
                                 'based_on': [b.strip() for b in re.split(r'[,;]', f.get(k + '_BASED', '')) if b.strip()]})
    return out


def notes_prompt(items, lines, since, language='English'):
    cur = '\n'.join(f'- id={i["id"]} {i["kind"]}: {i["text"]}' + (f' (owner {i["owner"]})' if i.get('owner') else '') +
                    (f' (due {i["due"]})' if i.get('due') else '') for i in items) or '(empty)'
    return f"""You keep the running notes of this meeting. Current notes:
{cur}

New transcript since the last update (with a little overlap):
{transcript_block(lines, since=since, limit_chars=9000)}

Return only what the new lines add or change: a new item with id "new", or an existing item with its id when its text,
owner or due changed. Items you do not return are kept as they are. Never merge several items into one. Put in "drop" the
ids that the new lines make wrong, and open questions that got answered. Only things that were actually said:
- decision: something agreed or decided.
- action: someone will do something; owner as said ("You" for the user), due only if said ("today", "Friday", a date).
- question: an open question nobody answered yet.
Also list FYI items: things said by others that matter to the user's work in the context above (at most 3, only new).
Also the focus now: "topic" = what the meeting is discussing right now, at most 6 words; "ask" = the single best
question the user could ask now (a gap, a risk, an unstated assumption, an action with no owner or no due), at most 15
words, or "" when nothing is worth asking; "ask_why" = at most 6 words on why.
Write in {language}, the language of the meeting.

Reply with JSON only:
{{"items": [{{"id": "existing id or new", "kind": "decision|action|question", "text": "short, plain", "owner": "", "due": "", "at": "mm:ss when it was said"}}],
  "drop": ["ids"],
  "fyi": [{{"text": "one short line", "who": "speaker", "at": "mm:ss"}}],
  "focus": {{"topic": "", "ask": "", "ask_why": ""}}}}"""


def parse_at(s):
    m = re.match(r'^(?:(\d+):)?(\d{1,2}):(\d{2})$', (s or '').strip())
    if not m: return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def merge_notes(old, new_items, next_id, drop=()):
    """Upsert the model's items into the old list: an existing id is replaced in place, 'new' gets the next free id,
    old items the model did not return are kept, ids in drop are removed. -> (items, next_id, ids_added)."""
    known = {i['id']: i for i in old}
    out, added = [], []
    for it in new_items or []:
        kind = (it.get('kind') or '').lower()
        if kind not in ('decision', 'action', 'question') or not (it.get('text') or '').strip(): continue
        iid = it.get('id') if it.get('id') in known else None
        if iid is None:
            # the model sometimes rewrites an old item without its id: same kind and very similar text = the same item
            for k, o in known.items():
                if o['kind'] == kind and difflib.SequenceMatcher(None, norm(o['text']), norm(it['text'])).ratio() > 0.8:
                    iid = k; break
        if iid is None or iid in [o['id'] for o in out]:
            iid = f'n{next_id}'; next_id += 1; added.append(iid)
        out.append({'id': iid, 'kind': kind, 'text': clean(it['text']), 'owner': clean(it.get('owner') or ''),
                    'due': clean(it.get('due') or ''), 'at': parse_at(it.get('at')) if parse_at(it.get('at')) is not None
                    else (known.get(iid) or {}).get('at')})
    got = {o['id']: o for o in out}
    drop = set(drop or ()) - set(added)
    merged = [got.pop(o['id'], o) for o in old if o['id'] not in drop]
    merged += [o for o in out if o['id'] in got]
    return merged, next_id, added


def notes_markdown(title, items):
    def sec(kind):
        return [i for i in items if i['kind'] == kind]
    act = '\n'.join(f"- [ ] {i['owner'] + ': ' if i['owner'] else ''}{i['text']}{' (due ' + i['due'] + ')' if i['due'] else ''}" for i in sec('action'))
    dec = '\n'.join('- ' + i['text'] for i in sec('decision'))
    q = '\n'.join('- ' + i['text'] for i in sec('question'))
    return f"# {title}\n\n## Decisions\n{dec or '- (none)'}\n\n## Actions\n{act or '- (none)'}\n\n## Open questions\n{q or '- (none)'}\n"
