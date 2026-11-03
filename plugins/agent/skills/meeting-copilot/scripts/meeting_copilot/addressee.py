"""The second stage of the gate: a stronger model decides whether a turn that passed the deterministic stage
(detect.py) really asks the user something. Only that decision: the cards stay on the fast model.

Live: one call per turn that passed, with a small fixed system prompt (the user's names and topic map, no context
pack), run alongside the cards call (`gated_cards`); the cards show only when it says yes. Any failure (an error, an
empty or unreadable reply, or no answer within DEADLINE_S) shows the cards as if it said yes: a missed alert costs
more than a stray one. Model: `addressee_model` in the config (empty = off, and the cards call decides as before).

Eval (`meeting-copilot eval --review --model`): the same call on the hand-reviewed turns that passed the deterministic
stage, plus a random sample of turns that passed (to estimate alerts per hour after the model). Every reply is cached
under $XDG_CACHE_HOME/meeting-copilot/eval/llm/, keyed by model, system prompt and prompt, so a re-run costs nothing;
at most `limit` new calls per run. Through the `claude` CLI (the login of Claude Code), like the rest of the copilot.
"""
import asyncio, hashlib, json, os, random, re, time

from . import brain, config, detect, llm

CONTEXT_S = 120.0       # transcript before the turn that goes into the prompt
MAX_TOPICS = 60
DEADLINE_S = 5.5        # live: the whole call, from the start of the cards for the turn; past it the held cards show


def system_prompt(user, aliases, topics=()):
    t = ', '.join(sorted(topics)[:MAX_TOPICS]) or '(none known)'
    return f"""You watch a live meeting transcript for {user} (also called {', '.join(aliases)}), who appears as "You".
Other people appear as "Speaker N" or P1, P2...; labels come from voice matching and can be wrong. The text comes from
live speech recognition: punctuation is often missing (Portuguese questions may have no question mark at all), words
and names can be misspelled.

Your only job: decide whether the LAST turn expects the user, now, to answer or act: a question to him (even without
a question mark), a request or task for him (including changes or requirements he is told to make on his own work),
his update in a round, his opinion or confirmation, or a follow-up on what he just said that waits for his answer.
"No" for: a question to someone else; a question to the group when someone else is the one expected to answer; a
rhetorical question; someone talking about the user's work without expecting anything from him; a plain
acknowledgement ("ok", "got it", "thanks"); small talk and greetings.

Topics the user works on (for reference): {t}

Reply with one line: YES or NO, then " | " and at most 12 words saying what is asked of the user (or why not)."""


def turn_prompt(lines, idx, cues=()):
    """lines: merged transcript lines up to the end of the turn; idx: indexes of the turn's lines."""
    end = lines[idx[-1]]['end']
    first = lines[idx[0]]['start']
    names = {}

    def tag(l):
        if l['me']: return 'You'
        return names.setdefault(l['who'], 'P' + str(len(names) + 1))
    before = [l for l in lines[:idx[0]] if l['end'] >= first - CONTEXT_S]
    t = '\n'.join(f"[{brain.mmss(l['start'])}] {tag(l)}: {l['text']}" for l in before)
    last = '\n'.join(f"[{brain.mmss(lines[k]['start'])}] {tag(lines[k])}: {lines[k]['text']}" for k in idx if lines[k]['end'] <= end)
    hint = f"\n(Cues the cheap filter saw: {', '.join(cues)}.)" if cues else ''
    return f"""Transcript before the turn:
{t or '(start of the meeting)'}

LAST TURN:
{last}{hint}

Does the last turn ask the user (You) something? One line: YES or NO | what is asked."""


def parse(text):
    """-> True / False / None (unreadable)."""
    m = re.match(r'\W*(yes|no|sim|nao|não)\b', (text or '').strip(), re.I)
    if not m: return None
    return m.group(1).lower() in ('yes', 'sim')


async def judge(pool, prompt, deadline=DEADLINE_S):
    """One live addressee call with a total deadline. Never raises (only cancellation passes through).
    -> {'status': 'yes' | 'no' | 'fail', 'why': '' | 'empty' | 'unreadable' | 'deadline' | 'error: <type>',
        'text', 'ms', 'usage', 'cost'}; the caller treats 'fail' as yes."""
    t = time.time()
    rec = {'status': 'fail', 'why': '', 'text': '', 'ms': 0, 'usage': {}, 'cost': 0.0}
    try:
        # pool.ask's own timeout is per streamed line; wait_for bounds the whole call (and kills the process)
        r = await asyncio.wait_for(pool.ask(prompt, timeout=deadline), deadline)
        rec.update(text=r.get('text') or '', usage=r.get('usage') or {}, cost=r.get('cost') or 0.0)
        yes = parse(rec['text'])
        if not rec['text'].strip(): rec['why'] = 'empty'
        elif yes is None: rec['why'] = 'unreadable'
        else: rec['status'] = 'yes' if yes else 'no'
    except asyncio.TimeoutError:
        rec['why'] = 'deadline'
    except asyncio.CancelledError:
        raise
    except Exception as e:      # the CLI did not start, a broken pipe...: never lose the alert over it
        rec['why'] = 'error: ' + type(e).__name__
    rec['ms'] = int((time.time() - t) * 1000)
    return rec


async def gated_cards(cards_pool, prompt, n, show, verdict=None):
    """The cards call, raced against n - 1 twins (the first to stream cards wins, the others are killed), its cards
    held until the addressee verdict: yes or a failure shows them (once: show(d) gets the parsed reply as it grows, and
    the caller shows only the cards it has not shown yet), no cancels the calls.
    verdict: an awaitable of judge(...), or None (no addressee stage: the cards call decides alone).
    -> (state, results): state has winner, directed, gate ('yes' | 'no' | 'fail'), addr (the judge record or None)."""
    st = {'winner': None, 'directed': None, 'gate': None if verdict else 'yes', 'pending': None, 'addr': None}
    cancels = [asyncio.Event() for _ in range(n)]

    def out(d):
        if st['gate'] in ('yes', 'fail'): show(d)
        else: st['pending'] = d                  # held until the verdict

    def on_text(idx, acc):
        if st['winner'] not in (None, idx): return
        d = brain.parse_cards(acc)
        if st['winner'] is None and (d['directed'] is False or d['cards']):
            st['winner'] = idx
            for k, c in enumerate(cancels):
                if k != idx: c.set()             # the twin lost the race
        if st['winner'] != idx: return
        st['directed'] = d['directed']
        if d['directed'] is False: cancels[idx].set(); return
        out(d)

    async def gatekeeper():
        r = await verdict                        # judge never raises
        st['addr'], st['gate'] = r, r['status']
        if r['status'] == 'no':
            for c in cancels: c.set()
        elif st['pending']: show(st['pending'])
    gk = asyncio.ensure_future(gatekeeper()) if verdict else None
    try:
        results = await asyncio.gather(*[cards_pool.ask(prompt, on_text=(lambda a, k=k: on_text(k, a)), cancel=cancels[k])
                                         for k in range(n)])
    except BaseException:                        # cancelled or failed: do not wait for the verdict
        if gk: gk.cancel()
        if hasattr(verdict, 'cancel'): verdict.cancel()
        raise
    if gk: await gk
    if st['gate'] != 'no':
        if st['winner'] is None:                 # nothing streamed: use the first full reply
            ok = [k for k, x in enumerate(results) if x['text']]
            if ok:
                st['winner'] = ok[0]; d = brain.parse_cards(results[ok[0]]['text'], final=True); st['directed'] = d['directed']
                if d['directed']: show(d)
        elif st['directed']:                     # cards whose last line had no newline yet
            show(brain.parse_cards(results[st['winner']]['text'], final=True))
    return st, results


def model_name(cfg=None, override=None):
    """The addressee model: `override` (eval --model NAME), else addressee_model from the config; '' turns the stage
    off live (None), while the eval falls back to notes_model."""
    if override and override != 'default': return override
    c = cfg or config.load()
    return c.get('addressee_model') or (c.get('notes_model') if override else None)


# ---------- eval ----------

def _cache_path(model, system, prompt):
    from .eval import cache_dir
    h = hashlib.sha256('\0'.join((model, system, prompt)).encode()).hexdigest()[:24]
    d = os.path.join(cache_dir(), 'llm'); os.makedirs(d, exist_ok=True)
    return os.path.join(d, h + '.json')


async def _run(jobs, model, limit, size=4):
    """jobs: [(key, system, prompt)] -> {key: reply dict}, the cache first; at most `limit` new calls."""
    out, todo = {}, []
    for key, system, prompt in jobs:
        p = _cache_path(model, system, prompt)
        try:
            with open(p, encoding='utf-8') as f: out[key] = dict(json.load(f), cached=True)
        except (OSError, ValueError):
            todo.append((key, system, prompt, p))
    todo = todo[:max(0, limit)]
    by_sys = {}
    for j in todo: by_sys.setdefault(j[1], []).append(j)
    import tempfile
    for system, js in by_sys.items():
        fd, sysfile = tempfile.mkstemp(prefix='addressee-', suffix='.md')
        with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(system)
        pool = llm.Pool(model, sysfile, size=size)
        sem = asyncio.Semaphore(size)

        async def one(key, prompt, path):
            async with sem:
                r = await pool.ask(prompt, timeout=90)
            if r['text']:
                rec = {'text': r['text'], 'ms': r['ms'], 'tokens': llm.tokens(r['usage']), 'cost': r['cost']}
                tmp = f'{path}.{os.getpid()}.tmp'
                with open(tmp, 'w', encoding='utf-8') as f: json.dump(rec, f)
                os.replace(tmp, path)
                out[key] = dict(rec, cached=False)
        try:
            await asyncio.gather(*[one(k, pr, pa) for k, _, pr, pa in js])
        finally:
            await pool.close()
            os.unlink(sysfile)
    return out


def review(meetings, aliases, n, verdicts, opts, model=None, strip=True, limit=120, jobs=None, extra=40, seed=7):
    """The two stages against the hand verdicts: the deterministic stage, then the model on the reviewed turns it
    passed. Also the model on `extra` random turns that passed (seeded), to estimate alerts per hour after it."""
    from . import eval as ev
    user, _ = config.user()
    model = model_name(override=model)
    by = dict(meetings)
    rows, calls, passed = [], [], []
    for vid, v in verdicts.items():
        mid, _, first = vid.partition(':')
        d = by.get(mid)
        if not d or not first.isdigit() or v.get('addressed') is None: continue
        res, _, _, _, lines = ev.score_meeting(d, aliases, [n], strip, opts)
        row = next((r for r in res[n] if r[0] == int(first)), None)
        if not row or row[1] not in ('pos', 'neg'): continue
        rows.append((vid, row[2], bool(v['addressed']), bool(v.get('asks')), d['lang']))
        if row[2]: calls.append((vid, d, lines, int(first)))
    rnd = random.Random(seed)
    pool = []
    for mid, d in meetings:
        if not d['user'] or d['type'] == '1:1': continue
        res, _, _, hours, lines = ev.score_meeting(d, aliases, [n], strip, opts)
        pool += [(f'{mid}:{r[0]}', d, lines, r[0]) for r in res[n] if r[2]]
    sample = rnd.sample(pool, min(extra, len(pool)))
    sysp = {}
    jobs_ = []
    for key, d, lines, first in calls + sample:
        idx = next(i for i, _ in ev.turns(lines) if i[0] == first)
        cues = next((r.split(':', 1)[1] for k in idx for r in lines[k]['reasons'] if r.startswith('fire:')), '')
        s = sysp.setdefault(tuple(d.get('topics') or ()), system_prompt(user, aliases, d.get('topics') or ()))
        jobs_.append((key, s, turn_prompt(lines, idx, [cues] if cues else [])))
    got = asyncio.run(_run(jobs_, model, limit))
    yes = {k: parse(r['text']) for k, r in got.items()}
    # as live: only a readable NO stops the alert (an unreadable or missing reply shows the cards)
    final = [(vid, fired and yes.get(vid) is not False, addr, asks, lang) for vid, fired, addr, asks, lang in rows]
    asks = [r for r in final if r[2] and r[3]]
    det_asks = [r for r in rows if r[2] and r[3]]
    not_addr = [r for r in final if not r[2]]
    tp = sum(1 for r in final if r[1] and r[2]); fp = sum(1 for r in final if r[1] and not r[2])
    fn = sum(1 for r in final if not r[1] and r[2])
    p, r_, f = ev.prf(tp, fp, fn)
    smp = [yes.get(k) is not False for k, *_ in sample if k in yes]
    new = [r for r in got.values() if not r['cached']]
    tok = {k: sum(r['tokens'][k] for r in got.values()) for k in ('in', 'cache_read', 'cache_write', 'out')}
    ms = sorted(r['ms'] for r in got.values())
    return {'model': model, 'reviewed': len(rows), 'model_calls': len(got), 'model_calls_new': len(new),
            'missing': len({k for k, _, _ in jobs_} - set(got)),
            'stage1_recall_on_asks': f"{sum(1 for r in det_asks if r[1])}/{len(det_asks)}",
            'final_recall_on_asks': f"{sum(1 for r in asks if r[1])}/{len(asks)}",
            'final_recall_on_asks_by_lang': {lg: f"{sum(1 for r in asks if r[1] and r[4] == lg)}/{sum(1 for r in asks if r[4] == lg)}"
                                             for lg in sorted({r[4] for r in asks})},
            'final_fired_on_not_addressed': f'{sum(1 for r in not_addr if r[1])}/{len(not_addr)}',
            'final_vs_hand_sample': {'precision': round(p, 3), 'recall': round(r_, 3), 'f1': round(f, 3)},
            'model_yes_rate_on_passed_turns': round(sum(1 for x in smp if x) / len(smp), 3) if smp else None,
            'passed_turns_sampled': len(smp),
            'tokens_all_calls': tok, 'cost_usd_all_calls': round(sum(r['cost'] for r in got.values()), 4),
            'cost_usd_this_run': round(sum(r['cost'] for r in new), 4),
            'ms_median': ms[len(ms) // 2] if ms else None, 'ms_p90': ms[min(len(ms) - 1, int(len(ms) * 0.9))] if ms else None}
