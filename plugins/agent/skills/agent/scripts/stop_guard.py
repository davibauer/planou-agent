#!/usr/bin/env python3
"""Claude Code Stop hook of the agent plugin (PLN0383): an agent session never ends a turn with its runner down.

The runner wakes the session by ENDING (a Bash run_in_background task); the session must relaunch it after every wake
(`FIRST_NOW=0 bash .../runner.sh <instance>`). When a wake lands in the middle of a long request the relaunch was
forgotten and the agent went silent for over an hour (no source tick, no heartbeat). The runner cannot relaunch itself
(nor can a hook start it): a process started outside the session's Bash tool is no task of the session, and its end
wakes nobody. So this hook only makes the model do it: at the end of a turn, when the instance of this session has no
runner alive, it answers {"decision": "block", "reason": ...} and the model relaunches before stopping.

Which session is an agent session (every check cheap first; any doubt = stay silent):
  1. the instance: TEAM_AGENT (the `team` launcher and the employee window export it), else the instance whose
     cache/planou/transcript.path (written by the runner's transcript pump) names this session's transcript;
  2. agent.py <instance> --runner-env: live and without ERR (the runner refuses the rest anyway);
  3. no runner of the instance alive: runner.pid naming a live runner.sh for it, or any runner.sh <instance> in /proc
     with the same HOME (never a probe on runner.lock: it would push a starting runner into its 60 s wait);
  4. no cache/runner/runner.stopped: `runner.sh <instance> stop` leaves it (from this session, from another terminal or
     from the provisioner's Desligar in Planou) and the next start of the runner clears it, so a stop made outside the
     session stays a stop;
  5. this session's transcript has a Bash run_in_background of `runner.sh <instance>`, and the last runner.sh command
     for it there is not a `stop` (a stop asked by the person stays a stop).
Against a loop: never with stop_hook_active (the model already got the block in this turn), and after a block that
did not bring the runner up (no Stop saw it alive since), not again for BACKOFF_S. Any error: exit 0 and no output (a
broken hook must never stop a session).
How it loads: the `team` launcher (team.sh, also under the employee window) passes it to claude with --settings, since the
skills are linked, not installed as a plugin; the plugin's hooks/hooks.json (`--plugin`) covers a plugin install and
stays out when the launcher's copy runs (TEAM_STOP_GUARD=1).
"""
import json, os, re, shlex, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,60}')
BACKOFF_S = int(os.environ.get('AGENT_STOP_GUARD_BACKOFF_S') or 1800)
MARK = 'stop_guard.json'   # in cache/runner: when the last block went out
STOPPED = 'runner.stopped'  # in cache/runner: written by `runner.sh <inst> stop`, removed when a runner starts


def instance_of(hook):
    v = os.environ.get('TEAM_AGENT') or ''
    if v: return v if NAME_RE.fullmatch(v) else None
    tp = hook.get('transcript_path') or ''
    if not tp: return None
    base = os.path.expanduser('~/.config/agent')   # watch_core.config.agent_base()
    try: names = sorted(os.listdir(base))
    except OSError: return None
    for n in names:
        try:
            with open(os.path.join(base, n, 'cache', 'planou', 'transcript.path')) as f:
                if f.read().strip() == tp and NAME_RE.fullmatch(n): return n
        except OSError: pass
    return None


def runner_env(inst):
    out = subprocess.run([sys.executable, os.path.join(HERE, 'agent.py'), inst, '--runner-env'],
                         capture_output=True, text=True, timeout=20).stdout
    env = {}
    for line in out.splitlines():
        k, _, v = line.partition('=')
        if k in ('ROOT', 'LIVE', 'ERR'):
            try: env[k] = ' '.join(shlex.split(v))
            except ValueError: env[k] = v
    return env


def runs_runner(pid, inst, leader=False):
    """pid is alive (not a zombie) and runs <dir>/runner.sh <inst> with agent.py beside it (any version: a rollout pin
    execs another version's runner.sh), not a stop or a status. leader: and leads its process group, as runner.sh's
    runners() (a subshell of the runner has the same cmdline and could outlive it)."""
    try:
        with open(f'/proc/{pid}/stat') as f: st = f.read()
        comm, fields = st[st.index('(') + 1:st.rindex(')')], st.rsplit(')', 1)[1].split()
        state, pgrp = fields[0], fields[2]
        # only bash (runner.sh runs under bash, as runner.sh's own `pgrep -x bash`), never one in D or Z: cmdline and
        # environ of a process stuck on a dead drive block the reader (PLN0241), and every Stop would wait on it
        if comm != 'bash' or state in ('D', 'Z') or (leader and pgrp != str(pid)): return False
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            a = [x.decode(errors='replace') for x in f.read().split(b'\0')]
    except (OSError, IndexError, ValueError): return False
    for i, x in enumerate(a):
        if os.path.basename(x) != 'runner.sh' or i + 1 >= len(a) or a[i + 1] != inst: continue
        if any(y in ('stop', '--stop', '--parar', '--status') for y in a[i + 2:]): return False
        d = os.path.dirname(x) or '.'
        if not d.startswith('/'): d = f'/proc/{pid}/cwd/{d}'
        if os.path.isfile(os.path.join(d, 'agent.py')): return True
    return False


def same_home(pid):
    try:
        with open(f'/proc/{pid}/environ', 'rb') as f:
            return ('HOME=' + os.environ.get('HOME', '')).encode() in f.read().split(b'\0')
    except OSError: return False


def runner_alive(inst, rdir):
    try:
        with open(os.path.join(rdir, 'runner.pid')) as f: p = f.read().strip()
        if p.isdigit() and runs_runner(p, inst): return True
    except OSError: pass
    for p in os.listdir('/proc'):
        if p.isdigit() and p != str(os.getpid()) and runs_runner(p, inst, leader=True) and same_home(p): return True
    return False


def launched_here(transcript, inst):
    """True when this session launched the runner in the background and did not stop it after."""
    if not isinstance(transcript, str) or not os.path.isfile(transcript): return False
    cmd_re = re.compile(r'runner\.sh[\'"]?\s+[\'"]?' + re.escape(inst) + r'[\'"]?(?=\s|$|[;&|)])([^;&|\n]*)')
    started, last = False, None
    with open(transcript, 'rb') as f:
        for raw in f:
            if b'runner.sh' not in raw or b'tool_use' not in raw: continue
            try: e = json.loads(raw)
            except ValueError: continue
            content = (e.get('message') or {}).get('content') if isinstance(e, dict) else None
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict) or c.get('type') != 'tool_use' or c.get('name') != 'Bash': continue
                inp = c.get('input') or {}
                for m in cmd_re.finditer(str(inp.get('command') or '')):
                    rest = m.group(1).split()
                    if rest and rest[0] in ('stop', '--stop', '--parar'): last = 'stop'
                    elif rest and rest[0] == '--status': continue
                    else:
                        last = 'start'
                        if inp.get('run_in_background') is True: started = True
    return started and last == 'start'


def main():
    if '--plugin' in sys.argv[1:] and os.environ.get('TEAM_STOP_GUARD') == '1': return   # the launcher's copy runs
    try: hook = json.loads(sys.stdin.read() or '{}')
    except ValueError: return
    if not isinstance(hook, dict): return
    inst = instance_of(hook)
    if not inst: return
    env = runner_env(inst)
    root = env.get('ROOT')
    if not root or env.get('LIVE') != '1' or env.get('ERR'): return
    rdir = os.path.join(root, 'cache', 'runner')
    mark = os.path.join(rdir, MARK)
    if runner_alive(inst, rdir):
        try: os.remove(mark)
        except OSError: pass
        return
    if hook.get('stop_hook_active'): return
    try:
        if time.time() - os.path.getmtime(mark) < BACKOFF_S: return
    except OSError: pass
    if os.path.exists(os.path.join(rdir, STOPPED)): return   # stopped on purpose, here or outside the session
    if not launched_here(hook.get('transcript_path'), inst): return
    os.makedirs(rdir, exist_ok=True)
    with open(mark, 'w') as f:
        json.dump({'blocked_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'session_id': hook.get('session_id')}, f)
    runner = os.path.join(HERE, 'runner.sh')
    print(json.dumps({'decision': 'block', 'reason': (
        f'runner da instancia {inst} parado: relance agora com `FIRST_NOW=0 bash {runner} {inst}` '
        f'(Bash com run_in_background), em uma linha e mais nada. Se uma acordada do runner ainda estiver para '
        f'tratar, trate depois de relancar, como sempre (a saida fica em {rdir}/tick.out).')}))


if __name__ == '__main__':
    try: main()
    except Exception: pass
    sys.exit(0)
