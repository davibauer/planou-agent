#!/usr/bin/env python3
"""Who holds the locks of the `team` launcher, and whether that is an agent running or a leftover idle shell (PLN0215).

The launcher (team.sh) takes two locks per agent: ~/.cache/team/locks/<agent>.lock (XDG_CACHE_HOME) and
/tmp/team-<agent>.lock (TEAM_TMP_DIR). Up to the agent plugin 0.48.0 the shell the launcher left behind (`exec bash -i`
after claude exited, or after "já está rodando" when only one of the two locks was taken) inherited the lock descriptors
and kept the lock for as long as the terminal lived: the agent never opened again. The launcher now closes them, but
shells left by older launchers keep holding the lock until their terminal is closed.

A holder is a process with a descriptor on the lock file (the link in /proc/<pid>/fd names the current path; the link
is read, never followed: PLN0236) whose fdinfo has a
`lock:` line: an open descriptor alone is not a lock (a launcher that failed to lock keeps the file open). A holder is
an idle shell when it is a shell (bash, zsh, sh, dash, fish, ksh) with no script argument (`bash -i`) and no child
process other than the caller's own ancestors (the launcher typed inside that very shell). Anything else (claude, the
launcher `bash .../team.sh <agent>`) means the agent is running.

  team_lock.py status <agent>    prints running | idle | free and exits 0 (for scripts)
  team_lock.py explain <agent>   the launcher's message when the lock is taken: who holds it and how to release it

Nothing here kills or signals a process: releasing is the user's call (close the terminal, or kill -KILL <pid>).
Overrides for tests: XDG_CACHE_HOME, TEAM_TMP_DIR, TEAM_PROC_DIR (/proc).
"""
import os, sys

SHELLS = {'bash', 'zsh', 'sh', 'dash', 'fish', 'ksh'}


def proc():
    return os.environ.get('TEAM_PROC_DIR') or '/proc'


def lock_paths(name):
    cache = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    tmp = os.environ.get('TEAM_TMP_DIR') or '/tmp'
    return [os.path.join(cache, 'team', 'locks', f'{name}.lock'), os.path.join(tmp, f'team-{name}.lock')]


def _read(path, mode='r'):
    try:
        with open(path, mode) as f: return f.read()
    except OSError:
        return None


def cmdline(pid):
    raw = _read(os.path.join(proc(), str(pid), 'cmdline'), 'rb') or b''
    return [a.decode(errors='replace') for a in raw.split(b'\0') if a]


def children(pid):
    out = set()
    try:
        tasks = os.listdir(os.path.join(proc(), str(pid), 'task'))
    except OSError:
        return out
    for t in tasks:
        for c in (_read(os.path.join(proc(), str(pid), 'task', t, 'children')) or '').split():
            if c.isdigit(): out.add(int(c))
    return out


def ancestors(pid=None):
    """The caller and its parents up to init: a launcher typed into a leftover shell is that shell's child."""
    pid = os.getpid() if pid is None else pid
    out = set()
    while pid and pid not in out:
        out.add(pid)
        st = _read(os.path.join(proc(), str(pid), 'stat')) or ''
        try:
            pid = int(st[st.rindex(')') + 2:].split()[1])
        except (ValueError, IndexError):
            break
    return out


def is_idle_shell(pid, mine=frozenset()):
    argv = cmdline(pid)
    if not argv: return False
    name = os.path.basename(argv[0]).lstrip('-')
    if name not in SHELLS or any(not a.startswith('-') for a in argv[1:]): return False
    return not (children(pid) - set(mine))


def holders(path, mine=frozenset()):
    """[{pid, cmd, tty, idle, self}] of the processes holding the flock of `path` (the current file, by inode)."""
    # Never stat() /proc/<pid>/fd/N: it follows the link to whatever the descriptor points at, and a descriptor on a
    # Windows drive that stopped answering (/mnt/<letter>) hangs the caller in state D for good (PLN0236). readlink
    # reads the name only; the lock files are local, so the match is by name (a replaced lock reads "... (deleted)").
    if not os.path.exists(path): return []
    names = {os.path.abspath(path), os.path.realpath(path)}
    out = []
    for p in os.listdir(proc()):
        if not p.isdigit(): continue
        fd_dir = os.path.join(proc(), p, 'fd')
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) not in names: continue
            except OSError:
                continue
            info = _read(os.path.join(proc(), p, 'fdinfo', fd)) or ''
            if not any(l.startswith('lock:') for l in info.splitlines()): continue
            pid = int(p)
            try:
                tty = os.readlink(os.path.join(fd_dir, '0'))
            except OSError:
                tty = None
            out.append({'pid': pid, 'cmd': ' '.join(cmdline(pid)), 'tty': tty if tty and tty.startswith('/dev/') else None,
                        'idle': is_idle_shell(pid, mine), 'self': pid in mine})
            break
    return out


def status(name, mine=None):
    """('running' | 'idle' | 'free', [holders of both locks, one entry per pid])."""
    mine = ancestors() if mine is None else mine
    seen = {}
    for path in lock_paths(name):
        for h in holders(path, mine): seen.setdefault(h['pid'], h)
    hs = sorted(seen.values(), key=lambda h: h['pid'])
    if not hs: return 'free', []
    return ('idle' if all(h['idle'] for h in hs) else 'running'), hs


def _who(h):
    cmd = h['cmd'] if len(h['cmd']) <= 80 else h['cmd'][:77] + '...'
    return f"pid {h['pid']}: {cmd}" + (f", terminal {h['tty']}" if h['tty'] else '')


def explain(name):
    state, hs = status(name)
    if state == 'running':
        busy = [h for h in hs if not h['idle']]
        return (f'>> {name} já está rodando em outro terminal ({"; ".join(_who(h) for h in busy)}). '
                'Feche aquele antes de abrir de novo.')
    if state == 'idle':
        lines = [f'>> {name} não está rodando: a trava está presa por um shell ocioso que sobrou de uma abertura anterior '
                 '(o shell herdou a trava do launcher antigo).']
        for h in hs:
            if h['self']:
                lines.append(f"   {_who(h)}: é este terminal. Digite exit e rode team {name} de novo.")
            else:
                lines.append(f"   {_who(h)}. Para liberar: feche aquele terminal (exit nele) ou kill -KILL {h['pid']}; "
                             f"depois rode team {name} de novo.")
        return '\n'.join(lines)
    return (f'>> {name} já está rodando em outro terminal (não achei o processo que segura a trava: '
            f'{" e ".join(lock_paths(name))}). Feche aquele antes de abrir de novo.')


def main(argv):
    if len(argv) != 2 or argv[0] not in ('status', 'explain'):
        print(__doc__, file=sys.stderr); return 2
    if argv[0] == 'status':
        print(status(argv[1])[0]); return 0
    print(explain(argv[1])); return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
