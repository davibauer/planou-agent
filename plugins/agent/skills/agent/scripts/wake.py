#!/usr/bin/env python3
"""Blocks that wait instead of waking the session (PLN0247): the grouping buffer of the runner.

The job-scout woke its session ~40 times a day: almost every 30-minute tick brought a few jobs that passed the filter,
a reputation nag or the Gmail check reminder, and each wake cost a turn of the model. Those blocks do not need an answer
in the minute they appear: they can wait and go together. A behavior declares them in `behaviors/<name>/wake.json`
(the instance may add to it with a "wake" object in its config.json, same keys):

  hold           [regex]  a block whose first line matches is kept in the buffer and never wakes alone; every one is
                          kept (a job listed once is never listed again: the buffer is its only copy)
  latest         [regex]  the same, but only the newest copy is kept (a reminder printed again on every tick while it
                          holds, like `== GMAIL ATRASADO`); one that a tick no longer prints leaves the buffer
  urgent         [regex]  a line matching inside a block held now wakes at once, with the whole buffer
  hold_max_min   int      the oldest block in the buffer wakes the session after this many minutes (default 180)
  hold_max_kb    int      a buffer larger than this wakes the session (default 64): it never drops anything
  broken_repeat_min int   a FONTE QUEBRADA of a source already broken wakes again only after this many minutes;
                          0 = never again until it recovers (the runner's default stays 60)

A block starts at a line that matches (a `== ` header or a loose line such as `(notion: 2 linhas sincronizadas)`) and
goes on over the lines below it up to a blank line, the next `== ` header or a protocol line (FONTE QUEBRADA, AVISO,
Traceback...); a loose line takes only its indented lines. Nothing else in the tick changes.

The runner (runner.sh) calls:
  wake.py hold --root <instance> --dir <cache/runner> [--first]
      after a heavy tick: moves the blocks out of tick.tmp into <instance>/data/retido.json (data/, not cache/: the
      scout marked those jobs as seen, the buffer is their only copy, and data/ is in the backup) and prints `DUE=<reason>` when the buffer
      must go now (urgent line, oldest block past hold_max_min, buffer past hold_max_kb, or --first: the first heavy
      tick of a session always wakes, so a fresh session sees everything, the Gmail check included). One line per
      block held goes to avisos.log. Any error leaves tick.tmp untouched (the tick wakes as it did before).
  wake.py release --root <instance>
      prints the buffer for the tick.out of a wake (any wake: heavy tick, Planou, stuck tick), headed by
      `== AGRUPADAS (...)` when it holds blocks of earlier ticks. The runner deletes retido.json only after tick.out
      was written.
  wake.py rules --root <instance>
      the merged rules as JSON (exit 1 when the instance has none)
"""
import argparse, json, os, re, sys, tempfile, time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
BEHAVIORS_DIR = os.path.join(os.path.dirname(SCRIPTS), 'behaviors')
BUFFER = 'retido.json'

if SCRIPTS not in sys.path: sys.path.insert(0, SCRIPTS)
from watch_core import behavior_names   # noqa: E402  (old behavior names in config.json still count)


def buffer_path(root):
    return os.path.join(root, 'data', BUFFER)
LIST_KEYS = ('hold', 'latest', 'urgent')
INT_KEYS = ('hold_max_min', 'hold_max_kb', 'broken_repeat_min')
DEFAULT_MAX_MIN = 180
DEFAULT_MAX_KB = 64
STOP = re.compile(r'^(== |FONTE QUEBRADA|VIGIA QUEBRADO|FONTE RECUPERADA|AVISO |Traceback|EXIT=)')


# ---------------------------------------------------------------- rules

def problems(w, at='"wake"'):
    """Errors of a wake object (config.json "wake" or a behavior's wake.json)."""
    if not isinstance(w, dict): return [f'{at} precisa ser um objeto {{hold, latest, urgent, hold_max_min, hold_max_kb, broken_repeat_min}}']
    err = []
    for k, v in w.items():
        if k in LIST_KEYS:
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                err.append(f'{at}.{k} precisa ser uma lista de expressoes regulares'); continue
            for x in v:
                try: re.compile(x)
                except re.error as e: err.append(f'{at}.{k}: expressao invalida {x!r} ({e})')
        elif k in INT_KEYS:
            if not isinstance(v, int) or isinstance(v, bool) or v < 0 or (k != 'broken_repeat_min' and v == 0):
                err.append(f'{at}.{k} precisa ser um inteiro positivo' + (' (ou 0)' if k == 'broken_repeat_min' else ''))
        elif not k.startswith('_'):
            err.append(f'{at}.{k}: chave desconhecida ({", ".join(LIST_KEYS + INT_KEYS)})')
    return err


def _behavior_rules(name, root=None):
    name = behavior_names.behavior_name(name)
    local = [(os.path.join(root, 'behaviors'), n) for n in [name, *behavior_names.old_names(name)]] if root else []
    for base, n in local + [(BEHAVIORS_DIR, name)]:
        f = os.path.join(base, n, 'wake.json')
        if os.path.isfile(f):
            try:
                with open(f, encoding='utf-8') as fh: return json.load(fh)
            except (OSError, ValueError): return None
    return None


def rules(cfg, root=None):
    """The merged rules of an instance: its behaviors' wake.json, then its config "wake". {} = nothing waits.
    Lists add up; a number of the config wins, among behaviors the smallest."""
    parts = [(_behavior_rules(b, root), False) for b in behavior_names.current(cfg.get('behaviors'))]
    parts.append((cfg.get('wake'), True))
    out = {}
    for w, own in parts:
        if not w or problems(w): continue
        for k in LIST_KEYS:
            for x in w.get(k) or []:
                if x not in out.setdefault(k, []): out[k].append(x)
        for k in INT_KEYS:
            if k in w: out[k] = w[k] if own or k not in out else min(out[k], w[k])
    return out if any(out.get(k) for k in ('hold', 'latest')) or 'broken_repeat_min' in out else {}


def load_cfg(root):
    for f in (os.path.join(root, 'config', 'config.json'), os.path.join(root, 'config.json')):
        if os.path.isfile(f):
            with open(f, encoding='utf-8') as fh: return json.load(fh)
    return {}


# ---------------------------------------------------------------- the tick

def split(text, rl):
    """(lines left, [(kind, rule, block text)]) of a tick: the blocks the rules take out."""
    pats = [('hold', r, re.compile(r)) for r in rl.get('hold') or []] + [('latest', r, re.compile(r)) for r in rl.get('latest') or []]
    lines, rest, blocks, i = text.split('\n'), [], [], 0
    while i < len(lines):
        ln = lines[i]
        hit = next(((k, r) for k, r, p in pats if not ln[:1].isspace() and p.search(ln)), None)
        if not hit:
            rest.append(ln); i += 1; continue
        j = i + 1
        header = ln.startswith('== ')
        while j < len(lines):
            nx = lines[j]
            if not nx.strip() or STOP.match(nx): break
            if not header and not nx[:1].isspace(): break
            j += 1
        blocks.append((hit[0], hit[1], '\n'.join(lines[i:j])))
        i = j
    # a block taken out leaves no run of blank lines behind
    out = []
    for ln in rest:
        if not ln.strip() and (not out or not out[-1].strip()): continue
        out.append(ln)
    while out and not out[-1].strip(): out.pop()
    return out, blocks


def read_buffer(path):
    try:
        with open(path, encoding='utf-8') as fh: b = json.load(fh)
        return b if isinstance(b.get('blocks'), list) else {'blocks': []}
    except (OSError, ValueError, AttributeError):
        return {'blocks': []}


def write_atomic(path, text):
    """A unique temporary file beside `path`, then os.replace (the watch_core.fileio way, without importing watch_core)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh: fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise


def _log(d, lines):
    if not lines: return
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    with open(os.path.join(d, 'avisos.log'), 'a', encoding='utf-8') as fh:
        for ln in lines: fh.write(f'{stamp}\t{ln}\n')


def hold(root, d, first=False, now=None):
    """Moves the blocks of tick.tmp into the buffer; returns the reason the buffer must go now, or ''."""
    now = time.time() if now is None else now
    rl = rules(load_cfg(root), root)
    if not rl: return ''
    tick = os.path.join(d, 'tick.tmp')
    try:
        with open(tick, encoding='utf-8', errors='replace') as fh: text = fh.read()
    except OSError: return ''
    rest, blocks = split(text, rl)
    bp = buffer_path(root)
    os.makedirs(os.path.dirname(bp), exist_ok=True)
    buf = read_buffer(bp)
    seen_latest = {r for k, r, _ in blocks if k == 'latest'}
    # a reminder the tick no longer prints (the Gmail was checked) leaves the buffer
    buf['blocks'] = [b for b in buf['blocks'] if b.get('kind') != 'latest' or b.get('rule') in seen_latest]
    log = []
    for kind, rule, txt in blocks:
        head = txt.split('\n', 1)[0][:120]
        old = next((b for b in buf['blocks'] if kind == 'latest' and b.get('kind') == 'latest' and b.get('rule') == rule), None)
        if old: old['text'] = txt
        else: buf['blocks'].append({'kind': kind, 'rule': rule, 'since': now, 'text': txt})
        log.append(f'agrupado sem acordar: {head}')
    if blocks:
        # buffer first, then the tick without the blocks: a failure in between repeats a block, never loses one
        write_atomic(bp, json.dumps(buf, ensure_ascii=False))
        write_atomic(tick, '\n'.join(rest) + ('\n' if rest else ''))
    elif buf['blocks'] != read_buffer(bp)['blocks']:
        write_atomic(bp, json.dumps(buf, ensure_ascii=False))
    if not buf['blocks']:
        try: os.remove(bp)
        except OSError: pass
        _log(d, log); return ''
    urgent = [re.compile(r) for r in rl.get('urgent') or []]
    reason = ''
    if first: reason = 'primeiro tick da sessao'
    elif any(p.search(ln) for _, _, txt in blocks for ln in txt.split('\n') for p in urgent): reason = 'linha urgente'
    elif now - min(b.get('since') or now for b in buf['blocks']) >= 60 * rl.get('hold_max_min', DEFAULT_MAX_MIN): reason = 'tempo maximo'
    elif sum(len(b.get('text') or '') for b in buf['blocks']) >= 1024 * rl.get('hold_max_kb', DEFAULT_MAX_KB): reason = 'buffer cheio'
    n = len(buf['blocks'])
    if not reason and log:
        log.append(f'{n} bloco(s) agrupados desde {time.strftime("%H:%M", time.localtime(min(b["since"] for b in buf["blocks"])))}')
    _log(d, log)
    return reason


def release(root, now=None):
    """The buffer as tick.out text ('' when empty)."""
    now = time.time() if now is None else now
    bs = [b for b in read_buffer(buffer_path(root))['blocks'] if (b.get('text') or '').strip()]
    if not bs: return ''
    oldest = min(b.get('since') or now for b in bs)
    out = []
    if now - oldest >= 60:
        out.append(f'== AGRUPADAS ({len(bs)} blocos desde {time.strftime("%H:%M", time.localtime(oldest))}, que esperaram '
                   'a proxima acordada: tratar como deste tick)')
        out.append('')
    for b in bs:
        out.append(b['text'].rstrip('\n')); out.append('')
    return '\n'.join(out) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description='blocos que esperam a proxima acordada (runner.sh)')
    ap.add_argument('cmd', choices=('hold', 'release', 'rules'))
    ap.add_argument('--root')
    ap.add_argument('--dir')
    ap.add_argument('--first', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'rules':
        rl = rules(load_cfg(a.root), a.root)
        print(json.dumps(rl, ensure_ascii=False, indent=1)); return 0 if rl else 1
    if a.cmd == 'release':
        sys.stdout.write(release(a.root)); return 0
    try:
        r = hold(a.root, a.dir, a.first)
    except Exception as e:                  # never breaks the runner: the tick wakes as it did before
        try: _log(a.dir, [f'wake.py hold falhou: {type(e).__name__}: {str(e)[:160]}'])
        except OSError: pass
        return 0
    if r: print(f'DUE={r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
