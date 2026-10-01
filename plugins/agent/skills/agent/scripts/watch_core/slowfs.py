#!/usr/bin/env python3
"""File reads on a Windows drive (/mnt/<letter>, 9P/DrvFs under WSL) with a deadline (PLN0245).

drive_probe.py (PLN0236) checks each source's folders before the tick runs it, but a folder that answers can still
hold a file that does not: 30/09 the ticks hung in state D reading a video that had just been moved to the archive
folder, while `ls` on the same drive answered. Any stat, open or read of a file on a drive can hang that way, and a
thread in D takes not even SIGKILL, so the process never finishes exiting and whoever waits for it hangs too (this is
why subprocess.run(timeout=...) does not help: after the timeout it kills the child and waits for it with no limit).

So every call below on a path that lives on a drive runs in a child process that nobody waits for: its stdio on
/dev/null, its own session, poll() until the deadline, and the answer written by the child to a local temporary file.
On a timeout the child is left behind (a SIGKILL is sent, taken only when the drive answers again) and the call raises
Stuck, which the tick turns into "FONTE QUEBRADA (<source>): drive X sem resposta (...)" for that source alone.
A path that is not on a drive runs the same code in this process, with no child: nothing changes for local folders.

  scan(folder, into=None)   {name: {'d': is_dir, 's': size, 'm': mtime}} from lstat (never opens a file); None when the
                            folder does not exist. Folders whose name starts with `into` also get 'f': their entries
  lstat(path)               (size, mtime, is_dir); OSError as os.lstat
  read_text(path, limit)    the first `limit` bytes as text (utf-8); OSError as open()
  load_json(path)           json.loads(read_text(path))
  run(argv, path, secs)     (returncode, stdout) of a command that reads `path` (ffprobe), never waited past `secs`
                            (default: the deadline);
                            (None, '') when a command on a local path runs out of time
  drive(path)               the drive label of `path` ('C', or the folder name of an AGENT_SLOW_FS_ROOTS entry), or None

Every child runs its code from memory (-c) in /, so a sick drive that holds the plugin folder cannot hang a read of
another drive. One timeout per drive per process: after it, every call on that drive raises Stuck at once. A child left stuck is
remembered in $XDG_CACHE_HOME/agent/drive-probe/fs-<hash>.pid (keyed by the path, not the drive: the drive may answer
for everything else); while that child is alive the same path raises Stuck without a new child, so a file that stays
stuck does not collect one more process in D per tick.
Overrides for tests: AGENT_FS_READ_S (deadline, default 20 s), AGENT_SLOW_FS_ROOTS (extra roots treated as a drive,
separated by ':'), XDG_CACHE_HOME. Standard library only: this file is also the child's script.
"""
import errno, hashlib, json, os, re, signal, subprocess, sys, tempfile, time

DRIVE_RE = re.compile(r'^/mnt/([A-Za-z])(?:/|$)')
_stuck = set()          # drives that ran out of time in this process


class Stuck(RuntimeError):
    """A read on a Windows drive ran out of time. The message never says "timeout": the tick would take it as a
    passing error and try again, leaving one more process in D."""
    def __init__(self, label, path, what='lendo'):
        self.label, self.path = label, path
        name = os.path.basename(str(path).rstrip('/')) or str(path)
        super().__init__(f'drive {label} sem resposta ({what} {name})')


def deadline():
    try:
        return max(0.1, float(os.environ.get('AGENT_FS_READ_S') or 20))
    except ValueError:
        return 20.0


def roots():
    return [r.rstrip('/') for r in (os.environ.get('AGENT_SLOW_FS_ROOTS') or '').split(':') if r.strip('/')]


def label(p):
    m = DRIVE_RE.match(p)
    if m: return m.group(1).upper()
    for r in roots():
        if p == r or p.startswith(r + '/'): return os.path.basename(r)
    return None


def drive(path):
    """Label of the drive `path` lives on. Local symlinks are followed with lstat/readlink only, and the walk stops at
    the first component on a drive: nothing on the drive itself is touched."""
    if not isinstance(path, str) or not path.strip(): return None
    p = os.path.abspath(os.path.expanduser(path))
    for _ in range(40):
        lab = label(p)
        if lab: return lab
        parts, cur, moved = p.strip('/').split('/'), '', False
        for i, part in enumerate(parts):
            cur = cur + '/' + part
            lab = label(cur)
            if lab: return lab
            try:
                if not os.path.islink(cur): continue
                target = os.readlink(cur)
            except OSError:
                return None
            p = os.path.normpath(os.path.join(os.path.dirname(cur), target, *parts[i + 1:])); moved = True
            break
        if not moved: return None
    return None


# ---------------- children left behind ----------------
def cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'agent', 'drive-probe')


def marker(name):
    return os.path.join(cache_dir(), re.sub(r'[^A-Za-z0-9_.-]', '_', name) + '.pid')


def path_marker(path):
    """Marker of a child left stuck on this path (by path, not by drive)."""
    return marker('fs-' + hashlib.sha1(os.path.abspath(os.path.expanduser(path)).encode()).hexdigest()[:16])


def proc_start(pid):
    """Start time of a live process (to tell it from a reused pid), None when it is gone or a zombie."""
    try:
        with open(f'/proc/{pid}/stat') as f: s = f.read()
    except OSError:
        return None
    f = s[s.rindex(')') + 2:].split()
    return None if f[0] in ('Z', 'X') else f[19]


def left_stuck(path):
    """The child noted in the marker file `path` is still alive (same pid and start time). A dead one is forgotten."""
    try:
        with open(path) as f: pid, start = f.read().split()[:2]
    except (OSError, ValueError):
        return False
    if proc_start(pid) == start: return True
    try: os.unlink(path)
    except OSError: pass
    return False


def remember(path, pid):
    start = proc_start(pid)
    if not start: return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f: f.write(f'{pid} {start}\n')
    except OSError:
        pass


def spawn(argv, secs, stdout=None):
    """Start `argv` and poll it until `secs`. (True, child) when it ended in time; (False, child) when it did not: the
    child gets a SIGKILL and is never waited for. OSError from Popen (command not found) goes up. The child runs in /,
    so its own imports never look at a folder on a drive (the runner's folder may live on one)."""
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout if stdout is not None else subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, cwd='/')
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if child.poll() is not None: return True, child
        time.sleep(0.02)
    if child.poll() is not None: return True, child
    try: os.kill(child.pid, signal.SIGKILL)       # taken only when the drive answers again; never waited for
    except OSError: pass
    return False, child


# ---------------- the operations (run here for a local path, in the child for a path on a drive) ----------------
def _scan(path, into=None):
    try:
        it = os.scandir(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    out = {}
    with it:
        for e in it:
            try: st = e.stat(follow_symlinks=False)
            except OSError: continue
            d = e.is_dir(follow_symlinks=False)
            x = {'d': d, 's': st.st_size, 'm': st.st_mtime}
            if d and into and e.name.startswith(into):
                try: x['f'] = sorted(os.listdir(e.path))
                except OSError: x['f'] = []
            out[e.name] = x
    return out


def _lstat(path, _=None):
    st = os.lstat(path)
    import stat as _st
    return [st.st_size, st.st_mtime, _st.S_ISDIR(st.st_mode)]


def _read(path, limit=None):
    with open(path, 'rb') as f: return f.read(int(limit or 1 << 20)).decode('utf-8', 'replace')


OPS = {'scan': _scan, 'lstat': _lstat, 'read': _read}
try:                    # the child runs this source with -c: never reads the script (nor imports) from the plugin folder
    with open(os.path.abspath(__file__), encoding='utf-8') as _f: _SRC = _f.read()
except (NameError, OSError):
    _SRC = None


def _tmp():
    d = os.path.join(cache_dir(), 'out')
    os.makedirs(d, exist_ok=True)
    fd, p = tempfile.mkstemp(prefix='fs-', suffix='.json', dir=d)
    os.close(fd)
    return p


def _call(op, path, arg=None):
    path = os.path.expanduser(path)
    lab = drive(path)
    if not lab: return OPS[op](path, arg)
    if lab in _stuck: raise Stuck(lab, path)
    mk = path_marker(path)
    if left_stuck(mk):
        _stuck.add(lab); raise Stuck(lab, path)
    out = _tmp()
    try:
        done, child = spawn([sys.executable, '-c', _SRC, op, path, out, json.dumps(arg)], deadline())
        if not done:
            remember(mk, child.pid); _stuck.add(lab)
            raise Stuck(lab, path)
        try:
            with open(out) as f: res = json.load(f)
        except (OSError, ValueError):
            raise OSError(errno.EIO, f'leitura sem resposta do processo filho ({op})', path)
    finally:
        try: os.unlink(out)
        except OSError: pass
    if 'err' in res: raise OSError(res['errno'], res['err'], path)
    return res['ok']


def scan(path, into=None): return _call('scan', path, into)


def lstat(path): return tuple(_call('lstat', path))


def read_text(path, limit=1 << 20): return _call('read', path, limit)


def load_json(path): return json.loads(read_text(path, 64 << 20))


def run(argv, path, secs=None):
    """A command that reads `path` (ffprobe). Its stdout goes to a local temporary file, never a pipe, and the command
    is never waited for past `secs`: a command left in D on a drive raises Stuck (and later calls on that drive too)."""
    lab = drive(path)
    if lab and lab in _stuck: raise Stuck(lab, path)
    mk = path_marker(path) if lab else None
    if mk and left_stuck(mk):
        _stuck.add(lab); raise Stuck(lab, path)
    out = _tmp()
    try:
        with open(out, 'wb') as f:
            done, child = spawn(argv, secs or deadline(), stdout=f)
        if not done:
            if not lab: return None, ''
            remember(mk, child.pid); _stuck.add(lab); raise Stuck(lab, path)
        with open(out, encoding='utf-8', errors='replace') as f: return child.returncode, f.read()
    finally:
        try: os.unlink(out)
        except OSError: pass


def _child(argv):
    op, path, out, arg = argv[0], argv[1], argv[2], json.loads(argv[3])
    try:
        res = {'ok': OPS[op](path, arg)}
    except OSError as e:
        res = {'err': e.strerror or str(e), 'errno': e.errno or errno.EIO}
    fd, tmp = tempfile.mkstemp(prefix='fs-', suffix='.part', dir=os.path.dirname(out))
    with os.fdopen(fd, 'w') as f: json.dump(res, f)
    os.replace(tmp, out)


if __name__ == '__main__':
    _child(sys.argv[1:])
