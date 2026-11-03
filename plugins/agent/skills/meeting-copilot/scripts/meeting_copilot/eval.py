"""Offline eval of the copilot's gate: "is this line addressed to the user?", measured on the recorded meetings.

  meeting-copilot eval [--meetings GLOB] [--n 3] [--sweep 1.5,3,5,8] [--json]
  meeting-copilot eval --sample 40 [--seed 7]         writes a blind review file (in the cache, never in the repo)
  meeting-copilot eval --review [VERDICTS]            scores the hand review against the automatic label and the gate

Data: the meeting-minutes folders (<videos_dir>/ata_*: transcript.json, speakers.json, voiceprints.npz, oratoria.md).
Nothing is transcribed again and no model is called: only the deterministic gate (brain.candidate) is measured.

Who is the user: the speakers whose voiceprint matches the user's voice in the meeting-minutes catalog (cosine >= 0.5;
two matches = the diarizer split the user), else the speaker oratoria.md names (SPEAKER_NN, or its word count).
Meetings where neither works are skipped and counted.

Automatic label, per turn of someone else (a run of lines by one speaker; another person's short backchannel such as
"yeah" or "ok" in the middle does not end it):
  pos   the user takes the floor with a real reply that starts at most N s after the turn ends (overlap counts;
        a backchannel first, "ok, so...", still counts when the real reply starts within the same N s);
  neg   a third person takes the floor within N s;
  weak  the user only answers with a backchannel within N s, or the real reply comes later (a short "yes" can be a
        real answer): reported, kept out of P/R/F1;
  excl  nobody speaks within N s, or the meeting ends.
Turn ids in the review files are <hash of the folder>:<index of the turn's first line>: they stay valid while the
transcript, MERGE_S and the backchannel list do not change (bump FORMAT when they do); review() counts as skipped any
verdict whose id no longer points at a pos or neg turn.
The label never looks at the form of the line (question mark, wh-word), so the gate is not measured against itself.

The gate is fed the way the live app feeds it: one call per transcript segment, with the history of lines merged as
app.add_line merges them (same speaker, gap under 2 s). A turn fires when any of its pieces fires. Offline whisperx
punctuation is much better than live ASR's, so every run also reports the gate with punctuation stripped.

Cache: $XDG_CACHE_HOME/meeting-copilot/eval/ (parsed meetings, keyed by the transcript's mtime and size; the review
files). The gate itself is never cached: a change to brain.py shows on the next run.
"""
import glob, hashlib, json, os, random, re, sys
from collections import Counter, defaultdict
from datetime import datetime

from . import brain, config, detect

FORMAT = 1               # bump when what is cached per meeting changes (user, language or type detection)
MERGE_S = 2.0           # app.add_line: same speaker within 2 s = the same line
VOICE_COS = 0.5         # meeting-minutes voiceprint.THRESH

BC_WORDS = set("""yeah yes yep yup ya ok okay right sure uh huh um mhm mm hmm hm got it cool nice alright all good
great perfect exactly thanks thank you no nope oh ah i see true fine wow sim e isso beleza ta certo show perfeito
entendi aham uhum claro exato boa legal valeu obrigado obrigada pois nao verdade hum tranquilo otimo ok""".split())
PT = set('que nao de uma pra para com mas isso entao eu voce ta tambem porque muito aqui vai tem ja esse essa gente'.split())
EN = set('the and is it that you we to of this so but have what just there with can be are was'.split())
PUNCT = re.compile(r'[?!.,;:¿¡]')
ACCENTS = str.maketrans('áàâãéêíóôõúüç', 'aaaaeeiooouuc')


def cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'meeting-copilot', 'eval')


def words(text):
    return brain.norm((text or '').lower().translate(ACCENTS)).split()


def is_backchannel(text):
    w = words(text)
    return 0 < len(w) <= 3 and all(x in BC_WORDS for x in w)


def language(texts):
    c = Counter(w for t in texts for w in words(t))
    pt, en = sum(c[w] for w in PT), sum(c[w] for w in EN)
    return 'pt' if pt > en else 'en'


meeting_type = detect.meeting_type


# ---------- reading a meeting folder ----------

def _json(path):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return None


def voice_user(folder, user_name):
    """Speakers whose voiceprint matches the user's voice in the catalog (numpy only here, imported lazily)."""
    try:
        import numpy as np
        if config.MM_SCRIPTS not in sys.path: sys.path.insert(0, config.MM_SCRIPTS)
        import mm_config
        cen = np.load(os.path.join(mm_config.VOICES, f'{user_name}.npy'))
        z = np.load(os.path.join(folder, 'voiceprints.npz'))
    except Exception:
        return None
    out = set()
    for k in z.files:
        v = z[k]; nv = float(np.linalg.norm(v)) or 1.0
        if float(v @ cen) / nv >= VOICE_COS: out.add(k)
    return out


def oratoria_user(folder, speakers):
    """The speaker oratoria.md is about: a SPEAKER_NN near the top, else the one whose word count it quotes."""
    try:
        with open(os.path.join(folder, 'oratoria.md'), encoding='utf-8') as f: head = f.read(4000)
    except OSError:
        return None
    m = re.search(r'(SPEAKER_\d+)', head)
    if m and m.group(1) in speakers: return {m.group(1)}
    for n in re.findall(r'(\d{1,3}(?:\.\d{3})*|\d+)\s+palavras', head):
        hit = [k for k, v in speakers.items() if v.get('palavras') == int(n.replace('.', ''))]
        if len(hit) == 1: return set(hit)
    return None


def read_meeting(folder, user_name):
    t = _json(os.path.join(folder, 'transcript.json'))
    sp = _json(os.path.join(folder, 'speakers.json')) or {}
    if not t: return None
    segs = [[s['speaker'], round(float(s['start']), 2), round(float(s['end']), 2), (s.get('text') or '').strip()]
            for s in t.get('segments') or [] if s.get('speaker') and (s.get('text') or '').strip()]
    locs = sp.get('locutores') or {}
    vu, ou = voice_user(folder, user_name), oratoria_user(folder, locs)
    if vu: user, src = vu, 'voice'
    elif ou: user, src = ou, 'oratoria'
    else: user, src = set(), 'none'
    title = ''
    try:
        with open(os.path.join(folder, 'ata.md'), encoding='utf-8') as f: title = f.readline()
    except OSError:
        pass
    real = [k for k, v in locs.items() if not v.get('provavel_ruido') and v.get('pct', 0) >= 3] or list({s[0] for s in segs})
    return {'format': FORMAT, 'segments': segs, 'user': sorted(user), 'user_src': src,
            'agree': (bool(ou & vu) if (vu and ou) else None),
            'lang': language(s[3] for s in segs), 'type': meeting_type(title, len(real))}


def excluded(name, mid, exclude):
    """A folder left out of the eval: its name contains one of the strings, or its id is one of them."""
    return any(x and (x in name or x == mid) for x in exclude or ())


def load_meetings(pattern, user_name, refresh=False, log=None, exclude=(), skipped=None):
    """[(meeting id, data)] from the folders matching pattern, through the cache. The id is a hash of the folder name.
    exclude: strings from the local config (eval_exclude): a folder whose name contains one, or whose id is one, is
    left out (personal recordings); `skipped` (a list) gets their ids."""
    cdir = os.path.join(cache_dir(), 'meetings'); os.makedirs(cdir, exist_ok=True)
    out = []
    for folder in sorted(glob.glob(pattern)):
        name = os.path.basename(folder.rstrip('/'))
        if excluded(name, hashlib.sha1(name.encode()).hexdigest()[:10], exclude):
            if skipped is not None: skipped.append(hashlib.sha1(name.encode()).hexdigest()[:10])
            continue
        tp = os.path.join(folder, 'transcript.json')
        try: st = os.stat(tp)
        except OSError: continue
        mid = hashlib.sha1(os.path.basename(folder.rstrip('/')).encode()).hexdigest()[:10]
        key = f'{FORMAT}:{st.st_mtime_ns}:{st.st_size}:{user_name}'
        cp = os.path.join(cdir, mid + '.json')
        d = None if refresh else _json(cp)
        if not d or d.get('key') != key:
            d = read_meeting(folder, user_name)
            if d is None: continue
            d['key'] = key
            tmp = f'{cp}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f: json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, cp)
            if log: log(f'read {mid}')
        d['name'] = name
        out.append((mid, d))
    return out


def meeting_start(name):
    m = re.search(r'(\d{4}-\d{2}-\d{2})[ _T](\d{2})-(\d{2})', name or '')
    return datetime.strptime(f'{m.group(1)} {m.group(2)}{m.group(3)}', '%Y-%m-%d %H%M') if m else None


def instance_of(start, instances):
    """The agent instance whose workspace has the minutes of this meeting (meetings/<YYYY-MM-DD-HHMM>-...)."""
    if not start: return None
    key = start.strftime('%Y-%m-%d-%H%M')
    for inst, ws in instances:
        if glob.glob(os.path.join(ws, 'meetings', key + '*')): return inst
    return None


def attach_topics(meetings, aliases, mode='minutes', log=None):
    """d['topics'] for each meeting, built only from what existed before it (no leak from its own minutes):
      minutes  the topic map of the minutes of the same instance before the meeting (lines about the user), plus the
               instance's `topics` from the local config;
      context  also CONTEXT.md and handoff.md of the workspace AS THEY ARE TODAY (written after most meetings: a leak,
               reported apart);
      none     no topic map (only what the user brings up in the meeting itself).
    -> {instance: number of meetings} (None = no instance found)."""
    from . import context
    insts = []
    for cp in sorted(glob.glob(os.path.join(config.AGENT_HOME, '*', 'config', 'config.json'))):
        inst = os.path.basename(os.path.dirname(os.path.dirname(cp)))
        ws = config.workspace(inst)
        if ws and os.path.isdir(os.path.join(ws, 'meetings')): insts.append((inst, ws))
    ws_of = dict(insts)
    cfg_topics = {k: v.get('topics') or [] for k, v in (config.load().get('instances') or {}).items()}
    seen = Counter()
    for _, d in meetings:
        d['topics'] = []
        if mode == 'none': continue
        start = meeting_start(d.get('name'))
        inst = instance_of(start, insts)
        seen[inst] += 1
        if not inst: continue
        ws = ws_of[inst]
        text = '\n'.join(context._read(p, 6000) for _, p in context.recent_minutes(ws, start, n=5))
        if mode == 'context':
            text += '\n' + '\n'.join(context._read(os.path.join(ws, f), 6000) for f in ('CONTEXT.md', 'handoff.md'))
        d['topics'] = sorted(detect.topic_terms(text, aliases) | {detect.fold(t) for t in cfg_topics.get(inst, [])})
    return dict(seen)


# ---------- gate and labels on one meeting ----------

def run_gate(segs, user, aliases, strip=False, gate='v2', topics=(), mtype='', suppress=True, threshold=None):
    """Lines merged as the live app merges them, each with 'fired' and the gate's reasons.
    v1: brain.candidate on every piece (the old gate). v2: detect.Detector, decided once per end of turn at
    end + DECIDE_S, before any line that starts after that (as the live timer does); suppress=False keeps the decision
    when a third person takes the floor first (to see how much of v2 comes from waiting rather than detecting)."""
    lines = []
    det = detect.Detector(aliases, topics, mtype, suppress=suppress, threshold=threshold) if gate == 'v2' else None

    def resolve(now):
        if det.pending and det.pending['due'] <= now:
            r = det.decide(lines)
            if r:
                l = lines[r['line']]
                l['reasons'].append(('fire:' + r['why'][0]) if r['fire'] else
                                    ('miss:asks nothing' if 'asks nothing' in r['why'] else 'miss:not for the user'))
                l['score'] = r['score']; l['cues'] = r['cues']
                if r['fire']: l['fired'] = True
    for who, a, b, text in segs:
        raw = text                  # lines merge by the length of the original text, so turn ids do not move with strip
        if strip: text = PUNCT.sub('', text)
        me = who in user
        if det: resolve(a)
        if det and not suppress and det.pending and not me and det.pending['who'] != who and not detect.is_backchannel(text):
            resolve(float('inf'))           # no suppression: the pending turn is decided as if nobody had taken the floor
        piece = {'who': who, 'me': me, 'text': text, 'start': a, 'end': b}
        last = lines[-1] if lines else None
        if last and last['who'] == who and a - last['end'] < MERGE_S and last['raw'] < 500:
            last['text'] = (last['text'] + ' ' + text).strip(); last['end'] = b; last['raw'] += 1 + len(raw)
        else:
            lines.append({**piece, 'fired': False, 'reasons': [], 'raw': len(raw)})
        if det:
            det.seen(lines); continue
        ok, why = brain.candidate(piece, lines[:-1], aliases)
        cur = lines[-1]
        cur['reasons'].append(why)
        if ok: cur['fired'] = True
    if det: resolve(float('inf'))
    return lines


def turns(lines):
    """Turns of the other people: (indexes of their lines, index of the line that ended the turn or None)."""
    out, cur = [], None
    for i, l in enumerate(lines):
        if l['me']:
            if cur: out.append((cur, i)); cur = None
            continue
        if cur and l['who'] == lines[cur[0]]['who']: cur.append(i); continue
        if is_backchannel(l['text']): continue      # a "yeah" from someone else neither ends a turn nor starts one
        if cur: out.append((cur, i))
        cur = [i]
    if cur: out.append((cur, None))
    return out


def label(lines, idx, nxt, n):
    if nxt is None: return 'excl'
    end, l = lines[idx[-1]]['end'], lines[nxt]
    if l['start'] - end > n: return 'excl'
    if not l['me']: return 'neg'
    if not is_backchannel(l['text']): return 'pos'
    for k in range(nxt + 1, len(lines)):         # "ok" first: pos only if the real reply also starts within N s
        m = lines[k]
        if m['start'] - end > n: return 'weak'
        if m['me']: return 'weak' if is_backchannel(m['text']) else 'pos'
        if not is_backchannel(m['text']): return 'weak'
    return 'weak'


def miss_reason(lines, idx):
    """Why the gate let a turn pass: the furthest its pieces got ('not for the user' > 'no question' > 'short';
    v2: 'not for the user' > 'asks nothing' > 'cancelled', a third person took the floor before the decision)."""
    rs = {r.split(':', 1)[-1] for i in idx for r in lines[i]['reasons']}
    for r in ('not for the user', 'no question', 'asks nothing', 'short'):
        if r in rs: return r
    return sorted(rs)[0] if rs else 'cancelled'


def fire_reason(lines, idx):
    for i in idx:
        for r in lines[i]['reasons']:
            if r in ('named', 'user spoke recently', 'named before'): return r
            if r.startswith('fire:'): return r[5:]
    return '?'


def score_meeting(d, aliases, ns, strip=False, opts=None):
    """{n: [(turn id, label, fired, reason)]}, other-speaker lines, fired lines, hours.
    opts: gate ('v1' | 'v2', default v2), suppress, threshold, gate_topics (the topic map d['topics'] also in the
    deterministic stage; off by default, as live: it added alerts without recall; the model stage always gets it)."""
    o = opts or {}
    lines = run_gate(d['segments'], set(d['user']), aliases, strip, gate=o.get('gate', 'v2'),
                     topics=(d.get('topics') or ()) if o.get('gate_topics') else (),
                     mtype=d.get('type', ''), suppress=o.get('suppress', True), threshold=o.get('threshold'))
    ts = turns(lines)
    res = {n: [] for n in ns}
    for idx, nxt in ts:
        fired = any(lines[i]['fired'] for i in idx)
        why = fire_reason(lines, idx) if fired else miss_reason(lines, idx)
        for n in ns:
            res[n].append((idx[0], label(lines, idx, nxt, n), fired, why))
    segs = d['segments']
    hours = max(1e-6, (segs[-1][2] - segs[0][1]) / 3600) if segs else 1e-6
    others = [l for l in lines if not l['me']]
    return res, len(others), sum(l['fired'] for l in others), hours, lines


# ---------- metrics ----------

def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def new_cell():
    return {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0, 'pos': 0, 'neg': 0, 'weak': 0, 'excl': 0, 'weak_fired': 0,
            'meetings': 0, 'hours': 0.0, 'fired_lines': 0, 'other_lines': 0, 'miss': Counter(), 'fa': Counter()}


def add(cell, rows):
    for _, lab, fired, why in rows:
        cell[lab] += 1
        if lab == 'pos':
            if fired: cell['tp'] += 1
            else: cell['fn'] += 1; cell['miss'][why] += 1
        elif lab == 'neg':
            if fired: cell['fp'] += 1; cell['fa'][why] += 1
            else: cell['tn'] += 1
        elif lab == 'weak' and fired: cell['weak_fired'] += 1


def finish(cell):
    p, r, f = prf(cell['tp'], cell['fp'], cell['fn'])
    out = {k: v for k, v in cell.items() if k not in ('miss', 'fa')}
    out.update(precision=round(p, 3), recall=round(r, 3), f1=round(f, 3),
               alerts_per_hour=round(cell['fired_lines'] / cell['hours'], 1) if cell['hours'] else 0.0,
               hours=round(cell['hours'], 2), miss=dict(cell['miss']), false_alarm=dict(cell['fa']))
    return out


def _score_job(a):
    d, aliases, ns, strip, opts = a
    return score_meeting(d, aliases, ns, strip, opts)[:4]


def evaluate(meetings, aliases, ns, strip=False, jobs=None, opts=None):
    """{n: {group: metrics}} with groups 'all:<lang>', 'grp:<lang>' (group meetings: all but 1:1, where "addressed"
    is trivial; the row to optimize) and '<lang>:<type>'. jobs: worker processes (None = all cores)."""
    todo = [d for _, d in meetings if d['user']]
    args = [(d, aliases, ns, strip, opts) for d in todo]
    if jobs == 1 or len(args) < 8:
        outs = [_score_job(a) for a in args]
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs or os.cpu_count()) as ex:
            outs = list(ex.map(_score_job, args, chunksize=2))
    cells = {n: defaultdict(new_cell) for n in ns}
    for d, (res, n_other, n_fired, hours) in zip(todo, outs):
        for n in ns:
            groups = [f"all:{d['lang']}", f"{d['lang']}:{d['type']}"] + ([f"grp:{d['lang']}"] if d['type'] != '1:1' else [])
            for g in groups:
                c = cells[n][g]; add(c, res[n])
                c['meetings'] += 1; c['hours'] += hours; c['fired_lines'] += n_fired; c['other_lines'] += n_other
    return {n: {g: finish(c) for g, c in sorted(cells[n].items())} for n in ns}


def coverage(meetings):
    c = Counter(d['user_src'] for _, d in meetings)
    agree = [d['agree'] for _, d in meetings if d['agree'] is not None]
    return {'meetings': len(meetings), 'user_by_voice': c['voice'], 'user_by_oratoria': c['oratoria'],
            'skipped_no_user': c['none'], 'voice_vs_oratoria_checked': len(agree), 'voice_vs_oratoria_agree': sum(agree),
            'by_lang': dict(Counter(d['lang'] for _, d in meetings if d['user'])),
            'by_type': dict(Counter(d['type'] for _, d in meetings if d['user']))}


# ---------- hand review ----------

def _context(lines, idx, user_tag='YOU'):
    names = {}
    def tag(l):
        if l['me']: return user_tag
        return names.setdefault(l['who'], 'P' + str(len(names) + 1))
    a, b = max(0, idx[0] - 6), min(len(lines), idx[-1] + 4)
    return [f"{'>> ' if a + j in idx else '   '}[{brain.mmss(l['start'])}] {tag(l)}: {l['text']}"
            for j, l in enumerate(lines[a:b])]


def sample(meetings, aliases, n, k, seed, opts=None):
    """k random pos and k random neg turns at reply window n, shuffled, without the label or the gate's verdict."""
    pool = {'pos': [], 'neg': []}
    for mid, d in meetings:
        if not d['user']: continue
        res, _, _, _, lines = score_meeting(d, aliases, [n], opts=opts)
        for first, lab, _, _ in res[n]:
            if lab in pool: pool[lab].append((mid, first))
    rnd = random.Random(seed)
    pick = rnd.sample(pool['pos'], min(k, len(pool['pos']))) + rnd.sample(pool['neg'], min(k, len(pool['neg'])))
    rnd.shuffle(pick)
    by = dict(meetings)
    items = []
    for mid, first in pick:
        d = by[mid]
        lines = run_gate(d['segments'], set(d['user']), aliases, gate='v1')     # the context only; the verdict is not shown
        idx = next(i for i, _ in turns(lines) if i[0] == first)
        items.append({'id': f'{mid}:{first}', 'lang': d['lang'], 'type': d['type'], 'context': _context(lines, idx)})
    return items


def review(meetings, aliases, n, verdicts, jobs=None, opts=None, strip=False):
    """verdicts: {id: {'addressed': bool|None, 'asks': bool}} -> label quality and the gate against the hand labels.
    The sample is stratified (k pos + k neg): label_error_rate_balanced_sample is the error rate of that 50/50 sample,
    not of the whole data (use auto_pos_wrong / auto_neg_wrong per stratum), and P/R/F1 against the hand labels are
    also given re-weighted to the real share of pos and neg turns (gate_vs_hand_weighted)."""
    by = dict(meetings)
    rows, skipped = [], 0
    for vid, v in verdicts.items():
        mid, _, first = vid.partition(':')
        d = by.get(mid)
        row = None
        if d and first.isdigit():
            row = next((r for r in score_meeting(d, aliases, [n], strip, opts)[0][n] if r[0] == int(first)), None)
        if not row or row[1] not in ('pos', 'neg') or v.get('addressed') is None:
            skipped += 1; continue
        rows.append((row[1], row[2], bool(v['addressed']), bool(v.get('asks')), d['lang']))
    pos = [r for r in rows if r[0] == 'pos']; neg = [r for r in rows if r[0] == 'neg']
    tot = Counter()
    for g, m in (evaluate(meetings, aliases, [n], strip, jobs=jobs, opts=opts)[n].items() if rows else ()):
        if g.startswith('all:'): tot['pos'] += m['pos']; tot['neg'] += m['neg']
    w = {'pos': tot['pos'] / len(pos) if pos else 0.0, 'neg': tot['neg'] / len(neg) if neg else 0.0}

    def gate(weighted):
        c = Counter()
        for lab, fired, addressed, _, _ in rows:
            k = w[lab] if weighted else 1.0
            if fired and addressed: c['tp'] += k
            elif fired: c['fp'] += k
            elif addressed: c['fn'] += k
        p, r_, f = prf(c['tp'], c['fp'], c['fn'])
        return {'precision': round(p, 3), 'recall': round(r_, 3), 'f1': round(f, 3)}
    asks = [r for r in rows if r[2] and r[3]]
    hit = sum(1 for r in asks if r[1])
    by_lang = {lg: f"{sum(1 for r in asks if r[1] and r[4] == lg)}/{sum(1 for r in asks if r[4] == lg)}"
               for lg in sorted({r[4] for r in asks})}
    not_addr = [r for r in rows if not r[2]]
    return {'reviewed': len(rows), 'skipped': skipped, 'auto_pos': len(pos), 'auto_pos_wrong': sum(1 for r in pos if not r[2]),
            'auto_neg': len(neg), 'auto_neg_wrong': sum(1 for r in neg if r[2]),
            'label_error_rate_balanced_sample': round(sum(1 for r in rows if (r[0] == 'pos') != r[2]) / len(rows), 3) if rows else None,
            'pos_that_ask': sum(1 for r in pos if r[2] and r[3]),
            'gate_vs_hand_sample': gate(False), 'gate_vs_hand_weighted': gate(True),
            'asks_for_user': len(asks), 'gate_recall_on_asks': f'{hit}/{len(asks)}' if asks else None,
            'gate_recall_on_asks_by_lang': by_lang,
            'gate_fired_on_not_addressed': f'{sum(1 for r in not_addr if r[1])}/{len(not_addr)}'}


# ---------- command ----------

def default_pattern():
    sys.path.insert(0, config.MM_SCRIPTS)
    try:
        import mm_config
        return os.path.join(mm_config.videos_dir(), 'ata_*')
    except Exception:
        return os.path.expanduser('~/Videos/ata_*')


def table(result):
    lines = []
    for n, groups in result.items():
        lines.append(f'reply window N = {n:g} s')
        lines.append(f"  {'group':<16}{'mtg':>4}{'pos':>6}{'neg':>6}{'weak':>6}{'excl':>6}{'P':>7}{'R':>7}{'F1':>7}{'alerts/h':>10}")
        for g, m in groups.items():
            lines.append(f"  {g:<16}{m['meetings']:>4}{m['pos']:>6}{m['neg']:>6}{m['weak']:>6}{m['excl']:>6}"
                         f"{m['precision']:>7.2f}{m['recall']:>7.2f}{m['f1']:>7.2f}{m['alerts_per_hour']:>10.1f}")
    return '\n'.join(lines)


def reasons(result, n):
    out = []
    for g, m in result[n].items():
        if g.split(':')[0] not in ('all', 'grp'): continue
        fn = sum(m['miss'].values()) or 1; fa = sum(m['false_alarm'].values()) or 1
        out.append(f"  {g}: misses " + ', '.join(f'{k} {v / fn:.0%}' for k, v in sorted(m['miss'].items(), key=lambda x: -x[1])) +
                   ' | false alarms ' + ', '.join(f'{k} {v / fa:.0%}' for k, v in sorted(m['false_alarm'].items(), key=lambda x: -x[1])))
    return '\n'.join(out)


def split(meetings, which):
    """Half of the meetings by the parity of their id: 'tune' (even) to set weights, 'check' (odd) to report."""
    if which in (None, '', 'all'): return meetings
    want = 0 if which == 'tune' else 1
    return [(m, d) for m, d in meetings if int(m, 16) % 2 == want]


def main(args):
    pattern = args.meetings or default_pattern()
    user_name, aliases = config.user()
    cfg = config.load()
    log = (lambda s: print(s, file=sys.stderr, flush=True)) if args.verbose else None
    skipped = []
    meetings = load_meetings(pattern, user_name, refresh=args.refresh, log=log, exclude=cfg.get('eval_exclude') or [], skipped=skipped)
    bad_types = set(cfg.get('eval_exclude_types') or [])
    meetings = [(m, d) for m, d in meetings if d['type'] not in bad_types or skipped.append(m)]
    meetings = split(meetings, args.split)
    if not meetings: sys.exit(f'no meetings match {pattern}')
    opts = {'gate': args.gate, 'suppress': not args.no_suppress, 'threshold': args.threshold,
            'gate_topics': args.gate_topics}
    insts = attach_topics(meetings, aliases, args.topics) if args.gate == 'v2' else {}
    os.makedirs(cache_dir(), exist_ok=True)
    if args.sample:
        items = sample(meetings, aliases, args.n, args.sample, args.seed, opts)
        path = os.path.join(cache_dir(), f'review-n{args.n:g}-seed{args.seed}.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for it in items: f.write(json.dumps(it, ensure_ascii=False) + '\n')
        print(f'{len(items)} items (blind: no label, no gate verdict) in {path}')
        print('Write one line per item to review-verdicts.jsonl in the same folder: '
              '{"id": "...", "addressed": true|false|null, "asks": true|false}')
        return
    if args.review is not None:
        path = args.review or os.path.join(cache_dir(), 'review-verdicts.jsonl')
        verdicts = {}
        with open(path, encoding='utf-8') as f:
            for row in f:
                if row.strip():
                    v = json.loads(row); verdicts[v['id']] = v
        out = {'as_is': review(meetings, aliases, args.n, verdicts, jobs=args.jobs, opts=opts),
               'no_punctuation': review(meetings, aliases, args.n, verdicts, jobs=args.jobs, opts=opts, strip=True)}
        if args.model:
            from . import addressee
            out['model'] = addressee.review(meetings, aliases, args.n, verdicts, opts, model=args.model, strip=True,
                                            limit=args.model_limit, jobs=args.jobs)
        if args.json: print(json.dumps(out, indent=1)); return
        for k, v in out.items():
            print(f'[{k}]'); print('\n'.join(f'  {a}: {b}' for a, b in v.items()))
        return
    ns = sorted({float(x) for x in args.sweep.split(',')} | {args.n}) if args.sweep else [args.n]
    cov = coverage(meetings)
    cov['excluded'] = len(skipped)
    asis = evaluate(meetings, aliases, ns, jobs=args.jobs, opts=opts)
    bare = evaluate(meetings, aliases, ns, strip=True, jobs=args.jobs, opts=opts)
    if args.json:
        print(json.dumps({'coverage': cov, 'gate': {str(n): v for n, v in asis.items()}, 'topics_by_instance': len(insts),
                          'gate_no_punctuation': {str(n): v for n, v in bare.items()}, 'options': opts}, indent=1))
        return
    print('meetings: ' + ', '.join(f'{k}={v}' for k, v in cov.items()))
    print(f"gate {args.gate}" + (f", topics {args.topics} ({sum(v for k, v in insts.items() if k)} meetings with an instance)"
                                  if args.gate == 'v2' else '') + ('' if opts['suppress'] else ', no third-person suppression'))
    print('\nGATE WITH PUNCTUATION STRIPPED (closer to live ASR: the row to optimize)')
    print(table({args.n: bare[args.n]})); print(reasons(bare, args.n))
    print('\nGATE AS IS (offline punctuation: an optimistic bound)')
    print(table(asis)); print(f'\nwhy, at N = {args.n:g} s:'); print(reasons(asis, args.n))
