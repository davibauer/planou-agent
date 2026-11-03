"""The ready context pack of an agent instance, so a meeting starts with the context already built and cached.

The agent's heavy tick (hook meeting_copilot of the agent plugin) runs `meeting-copilot prepare` detached every ~5 min:
it rebuilds only the parts whose source changed (files by mtime and size; PRs, issues by their answer; the schema text
of a PR is fetched again only when the PR changed) and writes, under <instance>/data/meeting-copilot/:

  context.md   the pack text (what `start` puts in the system prompt)
  system.md    the exact system prompt of the card calls (pack + rules), the one the pre-warm sends
  meta.json    built_at, the parts with their fingerprints, the PR schema cache, the agenda blocks already warmed

With --blocks (the agenda's busy blocks of the next minutes, given by the hook), about 2 min before a block starts it
finalizes the pack and sends one minimal call with that system prompt, so the prompt cache is warm when the first
question comes. Once per block. It never starts audio capture.

It prints nothing; one line per run that changed something or failed goes to --log (the runner's avisos.log).
"""
import asyncio, fcntl, json, os, tempfile, time
from datetime import datetime, timezone

from . import brain, config, context

LEAD_S = 120          # pre-warm this long before a block starts
FRESH_S = 15 * 60     # `start` uses the ready pack as is when it is younger than this


def ready_dir(instance):
    return config.session_root(instance)


def _paths(instance):
    d = ready_dir(instance)
    return d, os.path.join(d, 'context.md'), os.path.join(d, 'system.md'), os.path.join(d, 'meta.json')


def load_meta(instance):
    try:
        with open(_paths(instance)[3], encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.')
    with os.fdopen(fd, 'w', encoding='utf-8') as f: f.write(text)
    os.replace(tmp, path)


def refresh(instance, online=True):
    """Incremental rebuild. -> (pack, changed part names, errors, ms)."""
    t = time.time()
    d, ctx_p, sys_p, meta_p = _paths(instance)
    os.makedirs(d, exist_ok=True)
    meta = load_meta(instance)
    pack = context.Pack(instance, online=online)
    cache = meta.get('pr_cache') or {}
    parts, changed = pack.parts(meta.get('parts') or {}, pr_cache=cache)
    text = pack.use(parts)
    user, aliases = config.user()
    system = brain.system_prompt(user, aliases, text)
    if changed or not os.path.exists(sys_p):
        _write(ctx_p, text); _write(sys_p, system)
    meta.update({'built_at': datetime.now(timezone.utc).isoformat(), 'parts': parts, 'pr_cache': cache,
                 'sources': pack.sources, 'chars': len(system)})
    if changed: meta['changed_at'] = meta['built_at']
    _write(meta_p, json.dumps(meta, ensure_ascii=False, indent=1))
    errs = [f"{n}: {p['err']}" for n, p in parts.items() if p.get('err')]
    return pack, changed, errs, int((time.time() - t) * 1000), pack.prs_fetched


def load(instance, max_age_s=FRESH_S):
    """(text, sources, age_s) of the ready pack when it is younger than max_age_s, else None."""
    meta = load_meta(instance)
    _, ctx_p, _, _ = _paths(instance)
    if not meta.get('built_at') or not os.path.exists(ctx_p): return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(meta['built_at'])).total_seconds()
    if age > max_age_s: return None
    with open(ctx_p, encoding='utf-8') as f: return f.read(), meta.get('sources') or [], age


async def _prewarm(system_file, model):
    from . import llm
    os.environ.setdefault('CLAUDE_CODE_MAX_OUTPUT_TOKENS', '16')
    pool = llm.Pool(model, system_file, size=1)
    try:
        return await pool.ask('Reply with OK.', timeout=90)
    finally:
        await pool.close()


def prewarm(instance):
    """One minimal call with the exact system prompt of the card calls. -> (ms, tokens)."""
    cfg = config.load()
    r = asyncio.run(_prewarm(_paths(instance)[2], cfg['cards_model']))
    from .llm import tokens
    return r['ms'], tokens(r['usage'])


def due(blocks, now, warmed, lead_s=LEAD_S):
    """Blocks to pre-warm: not warmed yet, starting within the next 15 min and not started more than 1 min ago.
    -> [(block, seconds to wait)]."""
    out = []
    for b in blocks or []:
        try: ini = datetime.fromisoformat(b['ini'])
        except (KeyError, ValueError, TypeError): continue
        if ini.tzinfo is None: ini = ini.replace(tzinfo=timezone.utc)
        bid = b.get('id') or b['ini']
        if bid in warmed: continue
        left = (ini - now).total_seconds()
        if -60 <= left <= 15 * 60: out.append((dict(b, id=bid), max(0.0, left - lead_s)))
    return sorted(out, key=lambda x: x[1])[:1]


def _log(path, msg):
    if not path: return
    try:
        path = os.path.expanduser(path); os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}\tmeeting-copilot: {msg}\n')
    except OSError:
        pass


def _lock(instance, wait):
    d = ready_dir(instance); os.makedirs(d, exist_ok=True)
    fd = open(os.path.join(d, 'prepare.lock'), 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)); return fd
    except BlockingIOError:
        fd.close(); return None


def _gpu_busy():
    """True while the copilot (or a meeting-minutes transcription) holds the GPU lock."""
    try:
        fd = open(config.GPU_LOCK, 'a')
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); return False
    except BlockingIOError:
        return True
    finally:
        fd.close()


def _mark(instance, bid, **kw):
    meta = load_meta(instance)
    w = meta.setdefault('warmed', {})
    w[bid] = dict(w.get(bid) or {}, **kw)
    for k in sorted(w, key=lambda k: w[k].get('at', ''))[:-20]: w.pop(k)     # keep the last 20
    _write(_paths(instance)[3], json.dumps(meta, ensure_ascii=False, indent=1))


def prepare(instance, blocks=None, log=None, online=True, prewarm_now=False):
    """The tick's job: refresh, and pre-warm before the next busy block. Never raises."""
    try:
        if not prewarm_now and _gpu_busy(): return                # a meeting is being transcribed: leave the machine alone
        fd = _lock(instance, wait=False)
        if not fd: return                                         # the previous run is still going
        try:
            pack, changed, errs, ms, fetched = refresh(instance, online)
            if changed or errs:
                _log(log, f'pack refreshed in {ms} ms, changed: {", ".join(changed) or "nothing"}'
                          + (f', {fetched} PR(s) fetched again' if fetched else '') + (f'; kept the old text of {"; ".join(errs)}' if errs else ''))
            now = datetime.now(timezone.utc)
            todo = [({'id': 'now'}, 0.0)] if prewarm_now else due(blocks, now, load_meta(instance).get('warmed') or {})
            for b, _ in todo:
                if b['id'] != 'now': _mark(instance, b['id'], at=now.isoformat(), state='scheduled', ini=b.get('ini'))
        finally:
            fd.close()
        for b, wait in todo:
            time.sleep(wait)
            fd = _lock(instance, wait=True)
            try:
                pack, changed, errs, ms, fetched = refresh(instance, online)       # finalize: what changed while waiting
                wms, tok = prewarm(instance)
            finally:
                fd.close()
            if b['id'] != 'now': _mark(instance, b['id'], state='warmed', warmed_at=datetime.now(timezone.utc).isoformat(), tokens=tok)
            _log(log, f'pre-warmed for the block at {b.get("ini") or "now"}: pack {ms} ms (changed: {", ".join(changed) or "nothing"}), '
                      f'call {wms} ms, cache write {tok.get("cache_write")} read {tok.get("cache_read")}')
    except Exception as e:                                        # the tick never breaks because of the copilot
        _log(log, f'prepare failed: {type(e).__name__}: {str(e)[:160]}')
