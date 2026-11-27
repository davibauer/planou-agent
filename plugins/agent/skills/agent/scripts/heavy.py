#!/usr/bin/env python3
"""heavy.py: run a heavy job (build, test suite, render) without starving the machine (PLN1027).

With memory at its limit (swap 7/8 GB, PSI full ~7%) Claude Code killed the light runner of an idle agent while another
agent rendered a video (6 GB + ffmpeg) and a third ran several dotnet test/MSBuild/VBCSCompiler at once. Every heavy
job of every agent now goes through this wrapper:

  python3 heavy.py [--mem 50%] [--swap 512M] [--wait S] [--dotnet-shutdown] -- <command> [args...]
  python3 heavy.py --status

1. One heavy job at a time for the user: an fcntl lock on AGENT_HEAVY_LOCK (default ~/.cache/agent/heavy.lock), held
   by this wrapper on a close-on-exec fd, never by the job (a VBCSCompiler or an MSBuild node left alive must not keep
   the lock, the 43 min `flock` without `-o` of 29/09). The holder (pid, command, since) is written in the lock file.
2. Waits while MemAvailable < AGENT_HEAVY_MIN_AVAIL_MB (3072) or PSI "some avg10" > AGENT_HEAVY_MAX_PSI (5.0), from
   /proc/meminfo and /proc/pressure/memory. Lock and memory share one ceiling, AGENT_HEAVY_WAIT_S (1800 s): past it,
   nothing runs and the exit is 75 (EX_TEMPFAIL) with who holds the lock or what the memory looks like.
3. Runs the job in a user scope with a memory ceiling, `systemd-run --user --scope -p MemoryMax=<mem>
   -p MemorySwapMax=<swap>` (AGENT_HEAVY_MEMORY_MAX, default 50% of the RAM; AGENT_HEAVY_SWAP_MAX, default 512M, so a
   capped job does not just spill into swap), only when systemd-run exists, the user manager delegates the memory
   controller (cgroup.controllers of user@<uid>.service) and a probe scope (`true`) works; otherwise it runs without a
   ceiling and says so. Exit 137 with a ceiling: one line saying the job probably hit it.
4. The job (never this wrapper, which holds the lock) gets oom_score_adj AGENT_HEAVY_OOM_ADJ (500): raising it needs
   no privilege, so the kernel OOM killer takes the heavy job before the runner or the session. That only steers the
   kernel; what Claude Code itself kills under pressure it decides alone. The runner cannot lower its own score
   (below the inherited 0 needs CAP_SYS_RESOURCE), so this is the only safe lever. The job also gets PDEATHSIG TERM:
   a wrapper killed with -9 never leaves a render running without the lock.
5. A dotnet job (an argument named dotnet, or "dotnet " inside a `bash -c` text) or --dotnet-shutdown: `dotnet
   build-server shutdown` right after, still under the lock (AGENT_HEAVY_DOTNET_SHUTDOWN=0 turns it off).
6. Inside a heavy job a nested heavy.py runs the command directly (no lock wait on itself, no second scope). Nested
   means the pid written in the held lock file is one of its ancestors (/proc/<pid>/stat, the parent chain), never the
   environment alone (PLN1048): a job under `env -i` loses every variable and still nests, and an AGENT_HEAVY_ACTIVE=1
   inherited by a process outside the job (or set by hand) no longer skips the lock. AGENT_HEAVY_ACTIVE=1 is still
   exported to the job, as information only. Known limit: a daemon the job leaves behind is reparented away from the
   wrapper, so a heavy.py it starts waits for the lock like any other job (the right serialization, at worst a wait
   up to the ceiling while the outer job lasts).

The exit code is the job's (a signal N as 128+N). AGENT_MEM_PROC (default /proc) and AGENT_HEAVY_CGROUP_ROOT (default
/sys/fs/cgroup) are for the tests; AGENT_HEAVY_CGROUP=0 skips the ceiling; AGENT_HEAVY_SYSTEMD_RUN names systemd-run.

mem_state() and mem_high() are also what the runner's Stop hook (stop_guard.py) reads to tell a runner killed under
memory pressure: high = PSI some avg10 >= AGENT_MEM_HIGH_PSI (10) or MemAvailable < AGENT_MEM_HIGH_AVAIL_MB (1024),
the same rule runner.sh records in cache/runner/mem.alta while it lives (runner_mem_death below). High pressure is
only data: the notice says "por falta de memoria" only with evidence of the cause (PLN1048: the harness notice
"stopped because the system is running low on memory" for the runner's launch, read by stop_guard.py from the
transcript, or a kernel OOM kill of the runner's pid in dmesg); without it, "fim sem sinal" with the pressure beside it.
"""
import json, os, re, shlex, shutil, signal, subprocess, sys, time

EX_TEMPFAIL = 75


def _env_num(name, default, kind=float):
    try: return kind(os.environ.get(name) or default)
    except ValueError: return default


def proc_root():
    return os.environ.get('AGENT_MEM_PROC') or '/proc'


def mem_state(proc=None):
    """{'avail_mb', 'total_mb', 'swap_used_mb', 'swap_total_mb', 'some10', 'some60', 'full10', 'full60'}; a value it
    cannot read is None (a kernel without PSI has no /proc/pressure)."""
    proc = proc or proc_root()
    st = dict.fromkeys(('avail_mb', 'total_mb', 'swap_used_mb', 'swap_total_mb', 'some10', 'some60', 'full10', 'full60'))
    try:
        kv = {}
        with open(os.path.join(proc, 'meminfo')) as f:
            for line in f:
                k, _, v = line.partition(':')
                if v.split(): kv[k.strip()] = int(v.split()[0]) // 1024
        st['avail_mb'], st['total_mb'] = kv.get('MemAvailable'), kv.get('MemTotal')
        if 'SwapTotal' in kv and 'SwapFree' in kv:
            st['swap_total_mb'], st['swap_used_mb'] = kv['SwapTotal'], kv['SwapTotal'] - kv['SwapFree']
    except (OSError, ValueError): pass
    try:
        with open(os.path.join(proc, 'pressure', 'memory')) as f:
            for line in f:
                kind, *fields = line.split()
                if kind not in ('some', 'full'): continue
                vals = dict(x.split('=', 1) for x in fields if '=' in x)
                for avg in ('10', '60'):
                    try: st[f'{kind}{avg}'] = float(vals[f'avg{avg}'])
                    except (KeyError, ValueError): pass
    except OSError: pass
    return st


def describe(st):
    parts = []
    if st.get('avail_mb') is not None:
        parts.append(f'disponivel {st["avail_mb"]} MB' + (f' de {st["total_mb"]} MB' if st.get('total_mb') else ''))
    if st.get('swap_total_mb'): parts.append(f'swap {st["swap_used_mb"]}/{st["swap_total_mb"]} MB')
    if st.get('some10') is not None: parts.append(f'PSI some {st["some10"]:.1f} full {st.get("full10") or 0:.1f}')
    return ', '.join(parts) or 'memoria ilegivel'


def mem_high(st):
    """Pressure high enough to explain a process killed by Claude Code or the kernel (the runner's rule)."""
    psi, mb = _env_num('AGENT_MEM_HIGH_PSI', 10.0), _env_num('AGENT_MEM_HIGH_AVAIL_MB', 1024, int)
    return bool((st.get('some10') is not None and st['some10'] >= psi)
                or (st.get('avail_mb') is not None and st['avail_mb'] < mb))


def mem_busy(st):
    """Too tight to start a heavy job: the gate of the wrapper (stricter than mem_high)."""
    mb, psi = _env_num('AGENT_HEAVY_MIN_AVAIL_MB', 3072, int), _env_num('AGENT_HEAVY_MAX_PSI', 5.0)
    if st.get('avail_mb') is not None and st['avail_mb'] < mb: return True
    return st.get('some10') is not None and st['some10'] > psi


# ---- the lock ----

def lock_path():
    p = os.environ.get('AGENT_HEAVY_LOCK')
    if p: return p
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'agent', 'heavy.lock')


def holder(path=None):
    """Who holds the lock, from the lock file ({} when free or unreadable)."""
    import fcntl
    path = path or lock_path()
    try: fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError: return {}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return {}                       # nobody holds it
        except OSError: pass
        try: return json.loads(os.read(fd, 4096).decode() or '{}')
        except ValueError: return {'pid': '?'}
    finally: os.close(fd)


def holder_text(h):
    if not h: return 'livre'
    since = time.strftime('%H:%M', time.localtime(h['since'])) if isinstance(h.get('since'), (int, float)) else '?'
    return f'com o pid {h.get("pid", "?")} ({h.get("cmd", "?")}) desde {since}'


def _ppid(pid):
    try:
        with open(f'/proc/{pid}/stat') as f: return int(f.read().rsplit(')', 1)[1].split()[1])
    except (OSError, IndexError, ValueError): return 0


def nested(path=None):
    """True when the lock is held by one of this process's ancestors (we run inside its job)."""
    pid = holder(path).get('pid')
    if not isinstance(pid, int) or pid <= 1: return False
    p = os.getppid()
    for _ in range(64):
        if p == pid: return True
        if p <= 1: return False
        p = _ppid(p)
    return False


def try_lock(fd):
    import fcntl
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
    except OSError: return False


def acquire(cmd, deadline, poll, log):
    """The lock fd (close-on-exec), or None when the deadline passed."""
    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    said = 0
    while not try_lock(fd):
        now = time.monotonic()
        if now >= deadline: os.close(fd); return None
        if now - said >= 300:
            log(f'esperando a trava de jobs pesados ({holder_text(holder(path))})'); said = now
        time.sleep(min(poll, max(0.1, deadline - now)))
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({'pid': os.getpid(), 'cmd': ' '.join(cmd)[:160], 'since': int(time.time())}).encode(), 0)
    return fd


def wait_memory(deadline, poll, log):
    """True when there is room for a heavy job before the deadline."""
    said = 0
    while True:
        st = mem_state()
        if not mem_busy(st): return True, st
        now = time.monotonic()
        if now >= deadline: return False, st
        if now - said >= 60: log(f'esperando memoria ({describe(st)})'); said = now
        time.sleep(min(poll, max(0.1, deadline - now)))


# ---- the ceiling ----

def ceiling(mem, swap):
    """(argv prefix, None) for a capped run, or ([], reason) when the ceiling is not available."""
    if os.environ.get('AGENT_HEAVY_CGROUP') == '0': return [], 'AGENT_HEAVY_CGROUP=0'
    sd = os.environ.get('AGENT_HEAVY_SYSTEMD_RUN') or shutil.which('systemd-run')
    if not sd or not os.access(sd, os.X_OK): return [], 'sem systemd-run'
    uid = os.getuid()
    cg = os.path.join(os.environ.get('AGENT_HEAVY_CGROUP_ROOT') or '/sys/fs/cgroup', 'user.slice', f'user-{uid}.slice',
                      f'user@{uid}.service', 'cgroup.controllers')
    try:
        with open(cg) as f:
            if 'memory' not in f.read().split(): return [], 'o systemd do usuario nao delega o controlador de memoria'
    except OSError: return [], 'sem cgroup v2 do systemd do usuario'
    prefix = [sd, '--user', '--scope', '--quiet', '-p', f'MemoryMax={mem}', '-p', f'MemorySwapMax={swap}']
    try:
        r = subprocess.run(prefix + ['true'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e: return [], f'systemd-run falhou ({e.__class__.__name__})'
    if r.returncode != 0:
        return [], 'systemd-run --user --scope recusou (' + ((r.stderr or '').strip().splitlines() or ['?'])[-1][:120] + ')'
    return prefix + ['--'], None


DOTNET_RE = re.compile(r'(^|[\s/;&|(])dotnet\s')


def is_dotnet(cmd):
    return any(os.path.basename(a) == 'dotnet' or DOTNET_RE.search(a) for a in cmd)


def _child_setup(adj):
    def setup():
        try:   # never lower than inherited (lowering needs privilege): max(adj, current)
            with open('/proc/self/oom_score_adj', 'r+') as f:
                cur = int(f.read().strip() or 0)
                if adj > cur: f.seek(0); f.write(str(adj))
        except (OSError, ValueError): pass
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)   # PR_SET_PDEATHSIG
        except Exception: pass
    return setup


def run(argv, prefix=(), adj=500):
    """The job's exit code (128+N for a signal); TERM and HUP to the wrapper go to the job, INT reaches it by the group."""
    env = {**os.environ, 'AGENT_HEAVY_ACTIVE': '1'}
    p = subprocess.Popen(list(prefix) + list(argv), env=env, preexec_fn=_child_setup(adj))
    forward = lambda sig, _f: p.poll() is None and p.send_signal(sig)
    old = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP)}
    old[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)   # a handler, not SIG_IGN: exec resets it
    try:
        rc = p.wait()
    finally:
        for s, h in old.items(): signal.signal(s, h)
    return 128 - rc if rc < 0 else rc


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    log = lambda m: print(f'heavy: {m}', file=sys.stderr, flush=True)
    if args[:1] == ['--status']:
        h = holder()
        pre, why = ([], 'nao conferido') if os.environ.get('AGENT_HEAVY_CGROUP') == '0' else ceiling(
            os.environ.get('AGENT_HEAVY_MEMORY_MAX') or '50%', os.environ.get('AGENT_HEAVY_SWAP_MAX') or '512M')
        st = mem_state()
        print(f'memoria: {describe(st)}' + (' (ocupada: um job pesado esperaria)' if mem_busy(st) else ''))
        print(f'trava: {holder_text(h)} ({lock_path()})')
        print('teto: ' + ('systemd-run --user --scope' if pre else f'sem teto ({why})'))
        return 0
    mem = os.environ.get('AGENT_HEAVY_MEMORY_MAX') or '50%'
    swap = os.environ.get('AGENT_HEAVY_SWAP_MAX') or '512M'
    wait_s = _env_num('AGENT_HEAVY_WAIT_S', 1800.0)
    poll = _env_num('AGENT_HEAVY_POLL_S', 10.0)
    adj = _env_num('AGENT_HEAVY_OOM_ADJ', 500, int)
    shutdown = None
    while args and args[0] != '--':
        a = args.pop(0)
        if a in ('--mem', '--swap', '--wait') and args:
            v = args.pop(0)
            if a == '--mem': mem = v
            elif a == '--swap': swap = v
            else:
                try: wait_s = float(v)
                except ValueError: log(f'--wait precisa de segundos: {v}'); return 2
        elif a == '--dotnet-shutdown': shutdown = True
        else:
            log(f'argumento desconhecido {a!r}. uso: heavy.py [--mem 50%] [--swap 512M] [--wait S] '
                f'[--dotnet-shutdown] -- <comando> | heavy.py --status'); return 2
    cmd = args[1:]
    if not cmd: log('faltou o comando depois de --'); return 2
    if nested(): return run(cmd, (), adj)   # inside the job of the lock's holder: the outer one holds it all
    if shutdown is None: shutdown = is_dotnet(cmd) and os.environ.get('AGENT_HEAVY_DOTNET_SHUTDOWN') != '0'
    deadline = time.monotonic() + max(0.0, wait_s)
    fd = acquire(cmd, deadline, min(poll, 2.0), log)
    if fd is None:
        log(f'esperei {int(wait_s)} s pela trava ({holder_text(holder())}); nao rodei. Tente de novo mais tarde.')
        return EX_TEMPFAIL
    try:
        ok, st = wait_memory(deadline, poll, log)
        if not ok:
            log(f'esperei {int(wait_s)} s por memoria ({describe(st)}); nao rodei. Tente de novo mais tarde.')
            return EX_TEMPFAIL
        prefix, why = ceiling(mem, swap)
        if why: log(f'sem teto de memoria ({why}); rodando assim mesmo')
        rc = run(cmd, prefix, adj)
        if rc == 137 and prefix:
            log(f'o job foi morto (KILL): provavelmente bateu no teto de memoria (MemoryMax={mem}); '
                f'suba com --mem ou AGENT_HEAVY_MEMORY_MAX')
        if shutdown:
            try: subprocess.run(['dotnet', 'build-server', 'shutdown'], capture_output=True, timeout=120)
            except (OSError, subprocess.SubprocessError): log('dotnet build-server shutdown nao rodou')
        return rc
    finally:
        os.close(fd)


# ---- the runner killed under memory pressure (stop_guard.py and runner.sh) ----

MEM_MARK = 'mem.alta'          # cache/runner: written by runner.sh while the pressure is high, removed when it drops
DEATH_MARK = 'runner.memoria'  # cache/runner: written by runner.sh when a signal took it down under high pressure
LOGGED = 'mem_kill.logged'     # cache/runner: the key of the last death already logged


def _pid_runs(pid):
    try:
        with open(f'/proc/{pid}/stat') as f: return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except (OSError, IndexError): return False


def runner_mem_death(rdir):
    """(key, text, pid) when the runner of this cache/runner folder ended with the memory pressure high, else None.
    The text is the pressure (data, not the cause: log_mem_death adds the cause only with evidence); pid is the dead
    runner's (None for runner.memoria).
    - runner.memoria: the runner itself saw a signal with the pressure high (TERM, HUP; it removed runner.pid);
    - runner.pid naming a dead process (a KILL leaves it) and mem.alta (the last state it recorded was high), or no
      mark at all but the pressure high right now (a pinned older runner.sh that records nothing)."""
    def read(name):
        try:
            with open(os.path.join(rdir, name)) as f: return f.read().strip(), os.path.getmtime(os.path.join(rdir, name))
        except OSError: return None, None
    death, dt = read(DEATH_MARK)
    if death is not None:
        return f'{DEATH_MARK}:{int(dt)}', _mark_text(death, 'sinal'), None
    pid, _ = read('runner.pid')
    if not pid or not pid.isdigit() or _pid_runs(pid): return None
    mark, mt = read(MEM_MARK)
    if mark is not None: return f'{pid}:{int(mt)}', _mark_text(mark, 'morto'), int(pid)
    st = mem_state()
    if mem_high(st): return f'{pid}:agora', f'memoria alta agora ({describe(st)})', int(pid)
    return None


def _mark_text(line, how):
    """'<since epoch> <some10> <full10> <avail MB>' (mem.alta) or '<epoch> <some10> <full10> <avail MB>' (runner.memoria)."""
    f = line.split()
    try: when = time.strftime('%H:%M', time.localtime(int(f[0])))
    except (IndexError, ValueError): when = '?'
    st = {}
    try: st = {'some10': float(f[1]), 'full10': float(f[2]), 'avail_mb': int(f[3])}
    except (IndexError, ValueError): pass
    return (f'TERM ou HUP as {when} com a memoria alta' if how == 'sinal' else f'memoria alta desde {when}') + (
        f' ({describe(st)})' if st else '')


def kernel_oom(pid, since=None):
    """Evidence text when the kernel log has an OOM kill of this pid ("Killed process <pid>", "oom-kill:...pid=<pid>"),
    else None. AGENT_MEM_KLOG names a file to read instead of `dmesg` (the tests; set but unreadable = no evidence).
    since (epoch): a line stamped before it is an older process that had the same pid, not this runner. The "[secs]"
    stamp is the kernel clock, which does not count a suspend, as CLOCK_MONOTONIC does not: its epoch is taken from
    the age (now on that clock minus the stamp), not from the boot time; a line without a stamp counts."""
    if not pid: return None
    src = os.environ.get('AGENT_MEM_KLOG')
    try:
        if src is not None:
            with open(src, errors='replace') as f: text = f.read()
        else:
            text = subprocess.run(['dmesg'], capture_output=True, text=True, errors='replace', timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError): return None
    for line in (text or '').splitlines():
        if not re.search(rf'(Killed process {pid}\b|oom-kill:.*\bpid={pid}\b)', line): continue
        m = re.match(r'\s*\[\s*(\d+(?:\.\d+)?)\]', line)
        if since and m and time.time() - (time.monotonic() - float(m.group(1))) < since - 60: continue
        return f'o kernel matou o pid {pid} por falta de memoria (OOM)'
    return None


def cause(text, evidence):
    """What follows "parado": the memory as the cause only with evidence; otherwise the end has no known cause and the
    pressure goes as data."""
    return f'por falta de memoria ({evidence}; {text})' if evidence else f'(fim sem sinal; {text})'


def log_mem_death(root, rdir, inst, evidence=None):
    """Logs a runner that ended under memory pressure once in <root>/cache/planou/warnings.log (the format of
    watch_core.planou's _log: ISO time, tab, text; the last 200 lines). evidence: the cause seen by the caller (the
    harness notice in the transcript), or a callable that finds it; otherwise a kernel OOM kill of the dead pid since
    the runner's start (runner.pid). Both are looked for only when this death is not logged yet. Returns what follows "parado" in
    the notice (also when it was logged before: then the phrase the first logger wrote, so the starting runner never
    contradicts the Stop hook that saw the evidence) or None. The check of the logged key, the line and the key go under
    the lock of warnings.log, with an atomic rewrite (fileio.append_line): the Stop hook and a starting runner never
    log it twice, and a line of watch_core.planou written at the same time is never lost."""
    found = runner_mem_death(rdir)
    if not found: return None
    key, text, pid = found
    def logged():
        """The phrase of this death when it is already logged (a key alone, from an older version: no evidence)."""
        try:
            with open(os.path.join(rdir, LOGGED)) as fh: done = fh.read().split('\n', 1)
        except OSError: return None
        if done[0].strip() != key: return None
        return done[1].strip() if len(done) > 1 and done[1].strip() else cause(text, None)
    def found_now():                   # the transcript scan and dmesg: only for a death not logged yet
        ev = evidence() if callable(evidence) else evidence
        try: since = os.path.getmtime(os.path.join(rdir, 'runner.pid'))
        except OSError: since = None
        return cause(text, ev or kernel_oom(pid, since))
    phrase = logged()
    new = None if phrase else found_now()
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path: sys.path.insert(0, here)
        from watch_core import fileio
        from datetime import datetime, timezone
        f = os.path.join(root, 'cache', 'planou', 'warnings.log')
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with fileio.locked(f):
            phrase = logged()
            if not phrase:
                phrase = new or found_now()
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                fileio.append_line(f, f'{stamp}\trunner da instancia {inst} parado {phrase}')
                with open(os.path.join(rdir, LOGGED), 'w') as fh: fh.write(f'{key}\n{phrase}\n')
    except OSError: phrase = phrase or new
    return phrase


if __name__ == '__main__':
    if sys.argv[1:2] == ['--runner-death']:      # runner.sh at start, before it clears the marks: <root> <inst>
        try:
            t = log_mem_death(sys.argv[2], os.path.join(sys.argv[2], 'cache', 'runner'), sys.argv[3])
            if t: print(f'runner: o anterior parou {t}')
        except Exception: pass
        sys.exit(0)
    sys.exit(main())
