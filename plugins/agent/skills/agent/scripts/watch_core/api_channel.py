"""Which channel a worker goes by (PLN1032): Agent(worker) on the subscription, or a new process on the API credits.

The user's rule, for every agent: only when the subscription's weekly quota is at 90% or more and the API credit has
room, a request that would be an Agent(worker) runs in the background through the `claude-api` wrapper
(~/.local/bin/claude-api, the user's, outside this repository). The wrapper refuses with exit 4 under 90% and with
exit 3 at the credit cap; this module reads the same things the same way, so the session no longer has to remember to
read the statusline before delegating: `agent.py <instance> --brief` prints its decision on top (the CANAL line).

- The quota: ~/.cache/claude-rate-limits/<account>.latest.json, written by the statusline (rate_limits.seven_day
  .used_percentage, resets_at, captured_at). <account> is the basename of ${CLAUDE_CONFIG_DIR:-~/.claude} without one
  leading dot, as the wrapper does. No file, a broken one or a window that already turned (resets_at in the past) is
  "no reading": agent. A reading only grows inside its window, so an old one still decides; past an hour the line says
  "leitura de HH:MM" (it may be higher now).
- The credit: `claude-api --saldo` (it only sums the ledger; it also turns the cycle, which a raw ledger read would
  miss). At the cap: agent. Not parsed: claude-api anyway (the wrapper still refuses with exit 3).
- The history: the statusline overwrites its file, so the plugin keeps the readings it saw in
  ~/.config/agent/api-quota-history.jsonl (one row per new captured_at, rows of more than 8 days dropped), from every
  --brief and every live heavy tick; "acima de 90% desde <dia HH:MM>" is the first row of the window at 90% or more.

Everything takes `home` and `now` so tests never read the real ~/.cache or ~/.config.
"""
import fcntl, json, os, re, subprocess, time

THRESHOLD = 90            # the wrapper's COTA_MIN: under it, exit 4
ALL_IN = 99               # at this, the session itself stops working with tools and sends everything to claude-api
STALE_S = 3600            # an older reading is said with its time
KEEP_S = 8 * 86400        # history rows kept
WEEK_S = 7 * 86400
WRAPPER = '~/.local/bin/claude-api'
HISTORY = 'api-quota-history.jsonl'
DAYS = ('seg', 'ter', 'qua', 'qui', 'sex', 'sab', 'dom')
SALDO_RE = re.compile(r'US\$ ([\d.]+) de US\$ ([\d.]+) \(teto \d+% = US\$ ([\d.]+)')


def _home(home):
    return home or os.path.expanduser('~')


def account_key(env=None):
    """basename of ${CLAUDE_CONFIG_DIR:-$HOME/.claude} without one leading dot (the wrapper's _rl_key)."""
    env = os.environ if env is None else env
    d = env.get('CLAUDE_CONFIG_DIR') or os.path.join(env.get('HOME') or os.path.expanduser('~'), '.claude')
    k = os.path.basename(os.path.normpath(os.path.expanduser(d)))
    return k[1:] if k.startswith('.') else k


def cache_file(home=None, key=None):
    return os.path.join(_home(home), '.cache', 'claude-rate-limits', f'{key or account_key()}.latest.json')


def history_file(home=None):
    return os.path.join(_home(home), '.config', 'agent', HISTORY)


def read(home=None, key=None, now=None):
    """{'pct', 'resets_at', 'captured_at', 'key'} of the statusline's last reading, with 'turned': True when its window
    already turned; None without a file or with one that does not parse."""
    key = key or account_key()
    now = time.time() if now is None else now
    try:
        d = json.load(open(cache_file(home, key)))
        w = d['rate_limits']['seven_day']
        pct = float(w['used_percentage'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    resets = w.get('resets_at') or 0
    cap = d.get('captured_at')
    return {'key': key, 'pct': pct, 'resets_at': resets, 'captured_at': cap if isinstance(cap, (int, float)) else None,
            'turned': not isinstance(resets, (int, float)) or resets < now}


def record(r, home=None, now=None):
    """Appends the reading `r` to the history when its captured_at is new for its account; drops rows older than 8
    days. Returns the rows of the history (after the write). Never raises for a full disk or a broken row."""
    now = time.time() if now is None else now
    path = history_file(home)
    if not r or r.get('captured_at') is None: return rows(home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            old = _parse(f.read())
            keep = [x for x in old if (x.get('captured_at') or 0) >= now - KEEP_S]
            last = next((x for x in reversed(keep) if x.get('key') == r['key']), None)
            new = {k: r[k] for k in ('key', 'captured_at', 'pct', 'resets_at')}
            fresh = last is None or last.get('captured_at') != r['captured_at']
            if len(keep) != len(old):            # rewrite in place under the lock (readers take the same lock)
                f.seek(0); f.truncate()
                for x in keep + ([new] if fresh else []): f.write(json.dumps(x) + '\n')
            elif fresh:
                f.seek(0, 2); f.write(json.dumps(new) + '\n')
            f.flush()
            return keep + ([new] if fresh else [])
    except OSError:
        return rows(home)


def _parse(text):
    out = []
    for line in text.splitlines():
        try: x = json.loads(line)
        except ValueError: continue
        if isinstance(x, dict): out.append(x)
    return out


def rows(home=None):
    try: return _parse(open(history_file(home)).read())
    except OSError: return []


def above_since(hist, r):
    """(captured_at of the first row of the reading's window at 90% or more, seen_below) or None. The window is the
    same account, captured after resets_at - 7 days; seen_below says a row under 90% came before it in the window
    (without one, the history started above 90% and the quota may have crossed earlier)."""
    start = (r.get('resets_at') or 0) - WEEK_S
    below = False
    for x in sorted(hist, key=lambda x: x.get('captured_at') or 0):
        if x.get('key') != r['key'] or (x.get('captured_at') or 0) <= start: continue
        if (x.get('pct') or 0) >= THRESHOLD: return x['captured_at'], below
        below = True
    return None


def credit(home=None, run=subprocess.run):
    """(spent, cap) from `claude-api --saldo`; None when the wrapper does not answer in that format."""
    try:
        out = run([os.path.join(_home(home), '.local', 'bin', 'claude-api'), '--saldo'], capture_output=True, text=True,
                  timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = SALDO_RE.search(out or '')
    return (float(m.group(1)), float(m.group(3))) if m else None


def when(ts, now=None, day=False):
    """'HH:MM' (or '<dia> DD/MM HH:MM' with day, or when ts is not today)."""
    t, n = time.localtime(ts), time.localtime(time.time() if now is None else now)
    if day: return f'{DAYS[t.tm_wday]} {t.tm_mday:02d}/{t.tm_mon:02d} {t.tm_hour:02d}:{t.tm_min:02d}'
    if (t.tm_year, t.tm_yday) != (n.tm_year, n.tm_yday): return f'{t.tm_mday:02d}/{t.tm_mon:02d} {t.tm_hour:02d}:{t.tm_min:02d}'
    return f'{t.tm_hour:02d}:{t.tm_min:02d}'


def _pct(p):
    return f'{p:g}%'


def decide(home=None, now=None, key=None, run=subprocess.run):
    """The one decision: {'channel': 'agent'|'claude-api', 'why': text, 'pct', 'all_in'}. Records the reading in the
    history on the way (the --brief is one of the two writers)."""
    now = time.time() if now is None else now
    r = read(home, key, now)
    if r is None:
        return {'channel': 'agent', 'pct': None, 'all_in': False,
                'why': f'sem leitura da cota ({cache_file(home, key).replace(_home(home), "~", 1)})'}
    hist = record(r, home, now)
    age = ''
    if r['captured_at'] is not None and now - r['captured_at'] > STALE_S:
        age = f'leitura de {when(r["captured_at"], now)}, pode estar maior'
    if r['turned']:
        return {'channel': 'agent', 'pct': None, 'all_in': False,
                'why': f'janela da cota ja virou (leitura de {_pct(r["pct"])} antes da virada; a nova vem com a statusline)'}
    if r['pct'] < THRESHOLD:
        return {'channel': 'agent', 'pct': r['pct'], 'all_in': False,
                'why': f'cota semanal em {_pct(r["pct"])}, abaixo de {THRESHOLD}%' + (f'; {age}' if age else '')}
    since = above_since(hist, r)
    parts = [f'cota semanal em {_pct(r["pct"])}']
    if since is not None:
        at, below = since
        parts.append(f'acima de {THRESHOLD}% desde {when(at, now, day=True)}' if below else
                     f'acima de {THRESHOLD}% pelo menos desde {when(at, now, day=True)} (primeira leitura guardada)')
    if age: parts.append(age)
    if not os.path.isfile(os.path.join(_home(home), '.local', 'bin', 'claude-api')):
        return {'channel': 'agent', 'pct': r['pct'], 'all_in': False,
                'why': '; '.join(parts + [f'sem {WRAPPER} neste computador'])}
    c = credit(home, run)
    if c and c[0] >= c[1]:
        return {'channel': 'agent', 'pct': r['pct'], 'all_in': False,
                'why': '; '.join(parts + [f'saldo da API no teto (US$ {c[0]:g} de US$ {c[1]:g}), exit 3 do claude-api'])}
    parts.append(f'saldo US$ {c[0]:g} de US$ {c[1]:g}' if c else 'saldo nao conferido (o claude-api recusa com exit 3 no teto)')
    return {'channel': 'claude-api', 'pct': r['pct'], 'all_in': r['pct'] >= ALL_IN, 'why': '; '.join(parts)}


def command(add_dirs=(), model=None):
    """The ready claude-api command: new process, request by stdin, JSON and label by environment, no prompts."""
    dirs = ''.join(f' --add-dir {d}' for d in add_dirs)
    m = f' --model {model}' if model else ''
    return (f"CLAUDE_API_JSON=<scratchpad>/<PID>-api.json CLAUDE_API_LABEL='<PID> <o que faz>' {WRAPPER} "
            f"--permission-mode bypassPermissions{m}{dirs} < <scratchpad>/<PID>-pedido.txt")


def lines(d, add_dirs=(), model=None):
    """The CANAL line (and, for claude-api, the command and, at 99%, the all-in line)."""
    if d['channel'] == 'agent':
        return [f'CANAL: agent ({d["why"]}): Agent(worker) pela assinatura']
    out = [f'CANAL: claude-api ({d["why"]}): processo novo pelos creditos de API, em background (Bash run_in_background); '
           'o pedido inteiro pelo stdin, sem as linhas CANAL e COMANDO; exit 4 ou 3 = Agent(worker) normal',
           'COMANDO: ' + command(add_dirs, model)]
    if d['all_in']:
        out.append(f'CANAL {ALL_IN}%: a cota passou de {ALL_IN}%: a sessao tambem para de trabalhar com ferramentas e '
                   'manda tudo para o claude-api')
    return out


def tick(home=None, now=None):
    """The heavy tick's share: record a new reading, nothing else (cheap: one small file read). Never raises."""
    try: record(read(home, None, now), home, now)
    except Exception: pass


if __name__ == '__main__':   # the CANAL line alone, for a request that does not go through --brief: [add-dir ...]
    import sys
    print('\n'.join(lines(decide(), [a for a in sys.argv[1:] if os.path.isdir(os.path.expanduser(a))])))
