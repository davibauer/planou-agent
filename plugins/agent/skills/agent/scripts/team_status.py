#!/usr/bin/env python3
"""team-status [--json [--rollout]]: the team in one table, without the model.

Per agent: session open, runner alive (runner.pid), last tick and how long ago, the token weight of yesterday and today
(team-cost) and the version the runner runs. Base of the chief-of-staff and of the `runners` check of the health hook.

The agents come from the team list (team_agents.py, ~/.config/team/agents.json) plus the instances of the agent plugin
that are not in it yet; nothing is fixed in this script. Runner folder of each agent, first match:
  "runner_dir" in the agents.json entry (optional, ~ expanded)
  ~/.config/agent/<name>/cache/runner   an instance of the agent plugin
  ~/.config/<name>/cache/runner         an agent with its own plugin (job-scout, travel-agent)
  ~/.config/<name>/runner               an agent whose runner lives at the top of its folder (chief-of-staff)
An agent in agents.json without any of these folders shows "sem instância" (JSON "instance": false) and never counts
as a runner down. An unreadable or invalid agents.json is reported on stderr and the listing goes on with the instances.

Session open: the Claude Code registry (~/.claude/sessions/<pid>.json of a live process, procStart checked against
/proc/<pid>/stat so a reused pid after a reboot does not count), matched to the agent by the session name, by the
TEAM_AGENT of the process or by the id in ~/.config/team/<agent>.session. The locks of the `team` launcher
(~/.cache/team/locks/<agent>.lock and the older /tmp/team-<agent>.lock) are only a fallback, read by team_lock.py:
a lock held only by a leftover idle shell (a `bash -i` with no child, left by a launcher up to the agent plugin
0.48.0, PLN0215) is not a session: "session_source" is "idle_lock", "session_open" false, "idle_lock_pids" lists the
shells and the table says "trava de shell ocioso" with the pid (close that terminal, or kill -KILL <pid>).

A runner folder on a Windows drive (/mnt/<letter>) is checked first by drive_probe.py, in a child process with a
deadline (PLN0236): when the drive does not answer, nothing in that folder is read, the table says "drive X sem
resposta" (JSON "drive_stuck": "X") and the listing goes on. The lock check reads /proc/<pid>/fd links without following
them (team_lock.py). As a last resort, a run longer than TEAM_STATUS_MAX_S (default 90 s) gives up with one line on
stderr and leaves with exit 3 instead of hanging whoever called it (the chief-of-staff tick, the health hook).
Overrides for tests: CLAUDE_CONFIG_DIR (registry), XDG_CACHE_HOME and TEAM_TMP_DIR (locks), TEAM_COST_CMD (team-cost),
AGENT_DRIVE_PROBE_S and AGENT_SLOW_FS_ROOTS (drive_probe.py), TEAM_STATUS_MAX_S.
"""
import fcntl, glob, json, os, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drive_probe, team_agents, team_lock   # noqa: E402

MODE = {'canary': 'canário', 'released': '', 'pinned': 'fixa', 'rollback': 'voltou', 'refused': 'recusada', 'live': 'disco'}
STATE = {'canary': 'canário rodando', 'released': 'liberada', 'refused': 'recusada'}


def home():
    return os.path.expanduser('~')


def _held(path):
    try:
        f = open(path, 'a')
    except OSError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN); return False
        except OSError:
            return True


def _proc_start(pid):
    """Field 22 of /proc/<pid>/stat (process start); None when dead or a zombie."""
    try:
        s = open(f'/proc/{pid}/stat').read()
    except OSError:
        return None
    f = s[s.rindex(')') + 2:].split()
    return None if f[0] in ('Z', 'X') else f[19]


def _team_agent(pid):
    """Only TEAM_AGENT from the process environment (the rest may hold a token: never print it)."""
    try:
        env = open(f'/proc/{pid}/environ', 'rb').read().split(b'\0')
    except OSError:
        return None
    for kv in env:
        if kv.startswith(b'TEAM_AGENT='): return kv[11:].decode(errors='replace')
    return None


def live_sessions():
    """Claude Code registry (the same source as ListAgents): live processes with the same procStart."""
    d = os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(home(), '.claude'), 'sessions')
    out = []
    for f in glob.glob(os.path.join(d, '*.json')):
        try:
            data = json.load(open(f)); pid = int(data.get('pid') or os.path.basename(f).split('.')[0])
        except Exception:
            continue
        ps = _proc_start(pid)
        if ps is None or (data.get('procStart') is not None and str(data.get('procStart')) != ps): continue
        out.append({'pid': pid, 'name': data.get('name'), 'session_id': data.get('sessionId'), 'team_agent': _team_agent(pid)})
    return out


def _pointer(name):
    try:
        return open(os.path.join(team_agents.team_dir(), f'{name}.session')).read().strip() or None
    except OSError:
        return None


def session_source(name, live):
    """Where "open" came from: ('registry' | 'lock' | 'idle_lock' | None, [pids of the idle shells])."""
    sid = _pointer(name)
    if any(s['name'] == name or s['team_agent'] == name or (sid and s['session_id'] == sid) for s in live): return 'registry', []
    state, hs = team_lock.status(name, mine=frozenset())
    if state == 'running': return 'lock', []
    if state == 'idle': return 'idle_lock', [h['pid'] for h in hs]
    # held, but no holder found in /proc (another pid namespace): count it as open, as before
    if any(_held(p) for p in team_lock.lock_paths(name)): return 'lock', []
    return None, []


def runner_dir(agent, entry=None):
    """The agent's runner folder, or None when the agent has no folder at all (no instance)."""
    n = agent['name']
    given = (entry or {}).get('runner_dir')
    if isinstance(given, str) and given.strip(): return os.path.expanduser(given.strip())
    if agent.get('agent_plugin'): return os.path.join(home(), '.config', 'agent', n, 'cache', 'runner')
    base = os.path.join(home(), '.config', n)
    if not os.path.isdir(base): return None
    for d in (os.path.join(base, 'cache', 'runner'), os.path.join(base, 'runner')):
        if os.path.isdir(d): return d
    return os.path.join(base, 'cache', 'runner')


def runner_alive(d):
    try:
        os.kill(int(open(os.path.join(d, 'runner.pid')).read().strip()), 0); return True
    except Exception:
        return False


def version(d):
    """runner.version (PLN0056): "<plugin> <version> <mode>", written by the runner when it starts."""
    try:
        return open(os.path.join(d, 'runner.version')).read().split()[:3]
    except Exception:
        return None


def last_tick(d):
    for name in ('metricas.tsv', 'metrics.tsv'):
        p = os.path.join(d, name)
        if os.path.exists(p): return os.path.getmtime(p)
    return None


def _vk(v):
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).replace('-', '.').split('.'))


def canary():
    """Per plugin: canary, last released and the newest version with its state (~/.config/team/rollout.json)."""
    try:
        ro = json.load(open(os.path.join(team_agents.team_dir(), 'rollout.json')))
    except Exception:
        return {}
    out = {}
    for pl in sorted(set(ro.get('canary') or {}) | set(ro.get('plugins') or {})):
        vs = ((ro.get('plugins') or {}).get(pl) or {}).get('versions') or {}
        rel = [v for v, r in vs.items() if (r or {}).get('state') == 'released']
        newest = max(vs, key=_vk) if vs else None
        out[pl] = {'canary': (ro.get('canary') or {}).get(pl), 'released': max(rel, key=_vk) if rel else None,
                   'newest': newest, 'newest_state': (vs.get(newest) or {}).get('state') if newest else None,
                   'reason': (vs.get(newest) or {}).get('reason') if newest else None}
    return out


def cost():
    cmd = os.environ.get('TEAM_COST_CMD') or 'team-cost'
    try:
        return json.loads(subprocess.run([cmd, '2', '--json'], capture_output=True, text=True, timeout=60).stdout)
    except Exception:
        return {}


def weight(t):
    return t['input'] + 2 * t['cache_write'] + 5 * t['output'] + t['cache_read'] / 10


def agents():
    """[(effective agent, raw agents.json entry or {})]: the file in order, then the instances missing from it."""
    try:
        entries, _ = team_agents.load()
    except team_agents.AgentsError as e:
        print(f'team-status: {e}. Seguindo só com as instâncias do plugin agent.', file=sys.stderr)
        entries = []
    raw = {e['name']: e for e in entries}
    return [(a, raw.get(a['name']) or {}) for a in team_agents.all_agents(entries)]


def collect(now=None):
    now = time.time() if now is None else now
    live = live_sessions()
    spent = cost()
    res = {}
    for a, entry in agents():
        n = a['name']
        d = runner_dir(a, entry)
        src, idle_pids = session_source(n, live)
        stuck = drive_probe.stuck(d) if d else None       # never read a folder on a drive that does not answer
        ok = bool(d) and not stuck
        ut = last_tick(d) if ok else None
        v = version(d) if ok else None
        res[n] = {'session_open': src in ('registry', 'lock'), 'session_source': src, 'idle_lock_pids': idle_pids, 'runner_alive': ok and runner_alive(d),
                  'last_tick_min': None if ut is None else int((now - ut) / 60),
                  'weight_2d': int(sum(weight(t) for t in (spent.get(n) or {}).values())),
                  'plugin': v[0] if v else None, 'version': v[1] if v and len(v) > 1 else None,
                  'version_mode': v[2] if v and len(v) > 2 else None,
                  'instance': d is not None, 'runner_dir': d, 'listed': a.get('listed', True), 'enabled': a['enabled'],
                  'drive_stuck': stuck}
    return res


def render(res, can):
    lines = [f"{'agent':21} {'sessão':8} {'runner':13} {'últ. tick':>10} {'tokens 2d':>10}  versão"]
    for n, r in res.items():
        ut = '-' if r['last_tick_min'] is None else (f"{r['last_tick_min']} min" if r['last_tick_min'] < 120 else f"{r['last_tick_min'] // 60} h")
        runner = 'sem instância' if not r['instance'] else ('?' if r.get('drive_stuck') else ('vivo' if r['runner_alive'] else 'parado'))
        alert = '  <- runner caído com a sessão aberta' if r['instance'] and r['session_open'] and not r['runner_alive'] and not r.get('drive_stuck') else ''
        if r.get('drive_stuck'): alert += f"  <- {drive_probe.message(r['drive_stuck'])} (pasta do runner não lida)"
        ver = f"{r['version']} {MODE.get(r['version_mode'], r['version_mode'] or '')}".strip() if r['version'] else '-'
        if r.get('idle_lock_pids'):
            pids = ', '.join(str(p) for p in r['idle_lock_pids'])
            alert += f'  <- trava de shell ocioso (pid {pids}): feche aquele terminal ou kill -KILL {pids}'
        lines.append(f"{n:21} {'aberta' if r['session_open'] else 'fechada':8} {runner:13} {ut:>10} {r['weight_2d'] / 1e6:>9.1f}M  {ver}{alert}")
    if can:
        lines.append('\ncanário (~/.config/team/rollout.json):')
        for pl, c in can.items():
            extra = ''
            if c['newest'] and c['newest'] != c['released']:
                extra = f"; {c['newest']}: {STATE.get(c['newest_state'], c['newest_state'] or 'aguardando o canário')}"
                if c['reason']: extra += f" ({c['reason'][:80]})"
            lines.append(f"  {pl:12} canário {c['canary'] or '-'}; liberada {c['released'] or '-'}{extra}")
    return '\n'.join(lines)


def _watchdog():
    """Last resort: a read that hangs anyway (a /proc file of a process stuck on a dead drive) must not hang the caller."""
    try:
        limit = float(os.environ.get('TEAM_STATUS_MAX_S') or 90)
    except ValueError:
        limit = 90.0
    def fire():
        try:
            sys.stderr.write(f'team-status: sem resposta em {int(limit)} s (um drive ou processo travado?); saindo sem a tabela\n')
            sys.stderr.flush()
        finally:
            os._exit(3)
    t = threading.Timer(limit, fire); t.daemon = True; t.start()


def main(argv):
    if argv and argv[0] in ('-h', '--help'):
        print(__doc__); return 0
    _watchdog()
    res = collect()
    can = canary()
    if '--json' in argv:
        print(json.dumps({**res, '_rollout': can} if '--rollout' in argv else res)); return 0
    print(render(res, can)); return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
