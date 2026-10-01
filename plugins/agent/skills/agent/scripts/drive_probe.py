#!/usr/bin/env python3
"""Is a Windows drive answering? A path on /mnt/<letter> (9P/DrvFs under WSL) is checked in a child process with a
deadline before the caller touches it (PLN0236).

When the Windows side of a drive stops answering, any stat, open or listdir on it puts the calling thread in state D
(uninterruptible) for good: not even SIGKILL takes it out, and a process with one such thread never finishes exiting,
so whoever waits for it (the runner, `timeout`, a `$(...)`) hangs too. A thread with join(timeout) does not help for
that reason. So the check runs in a child process that nobody waits for: Popen with every stdio on /dev/null and its
own session, poll() until the deadline, and on timeout the child is left behind (a SIGKILL is sent, which a child in D
takes only when the drive answers again). The caller never touches the drive and carries on.

  stuck(path)          the drive label ('F') when the drive of `path` did not answer in time, else None
  stuck_any(paths)     the first stuck label among `paths`, else None
  drive(path)          the drive label of `path` (following local symlinks with lstat only), or None
The drive label, the child with a deadline and the marker of a child left stuck come from watch_core/slowfs.py, which
also does the reads the sources make on a drive (PLN0245).
  python3 drive_probe.py <path>...   prints "<path>: ok" or "<path>: drive X sem resposta", exit 1 when one is stuck

One probe per drive per process (the answer is cached), and a probe left stuck is remembered in
$XDG_CACHE_HOME/agent/drive-probe/<label>.pid: while that child is still alive the drive counts as stuck without a
new probe, so a dead drive does not collect one more process in D per tick.
A path that does not exist, or any other error, counts as answering: the caller deals with it as before.
Overrides for tests: AGENT_DRIVE_PROBE_S (deadline, default 5 s), AGENT_SLOW_FS_ROOTS (extra roots treated as a
drive, separated by ':'; the label is the folder name), XDG_CACHE_HOME.
"""
import os, sys

from watch_core import slowfs
from watch_core.slowfs import drive   # noqa: F401  (drive(path): the label, following local symlinks with lstat only)

CHILD = ('import os, sys\np = sys.argv[1]\ntry:\n    if os.path.isdir(p): os.listdir(p)\n'
         '    else: open(p, "rb").close()\nexcept OSError:\n    pass\n')
_cache = {}


def deadline():
    try:
        return max(0.1, float(os.environ.get('AGENT_DRIVE_PROBE_S') or 5))
    except ValueError:
        return 5.0


def _marker(label):
    return slowfs.marker(label)


def _left_stuck(label):
    """A probe left behind by an earlier run is still alive (same pid and start time): the drive is still stuck."""
    return slowfs.left_stuck(_marker(label))


def _remember(label, pid):
    slowfs.remember(_marker(label), pid)


def _probe(path, label):
    if _left_stuck(label): return True
    try:
        done, child = slowfs.spawn([sys.executable, '-c', CHILD, path], deadline())
    except OSError:
        return False
    if done: return False
    _remember(label, child.pid)
    return True


def stuck(path):
    label = drive(path)
    if not label: return None
    if label not in _cache: _cache[label] = _probe(os.path.expanduser(path), label)
    return label if _cache[label] else None


def stuck_any(paths):
    for p in paths or ():
        lab = stuck(p)
        if lab: return lab
    return None


def paths_in(value):
    """Every string in a config value (dicts and lists walked) that lives on a drive."""
    out = []
    if isinstance(value, str):
        if value.startswith(('/', '~')) and drive(value): out.append(value)
    elif isinstance(value, dict):
        for v in value.values(): out += paths_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value: out += paths_in(v)
    return out


def message(label):
    return f'drive {label} sem resposta'


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__); return 0
    bad = 0
    for p in argv:
        lab = stuck(p)
        print(f'{p}: ' + (message(lab) if lab else 'ok')); bad |= bool(lab)
    sys.stdout.flush()
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
