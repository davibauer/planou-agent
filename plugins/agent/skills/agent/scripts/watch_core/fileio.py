"""Atomic file writes and a cross-process lock for read-modify-write of the agent's state files.

Several processes write the same files: the runner (light loop and heavy tick) and the CLI (`fila started`, `pergunta`,
...) of the same agent. A fixed `<name>.tmp` breaks when two of them write at once: the second `os.replace` finds the
temporary file already renamed by the first (FileNotFoundError), or renames the first one's half-written content.

  write(path, text_or_fn)   writes into a unique temporary file in the same directory, then os.replace (atomic)
  write_json(path, value)   the same for a JSON value (json.dump keyword arguments pass through)
  locked(path)              exclusive flock on `<path>.lock` for load, change, save; reentrant in the same thread
                            (a nested call does not lock again: a second flock on another descriptor of the same file
                            would block the process on itself)
"""
import contextlib
import fcntl
import json
import os
import tempfile
import threading

_HELD = threading.local()


def write(path, content, mode=None):
    """content: str, or fn(file) that writes into the open text file. mode: permission bits for the new file (None =
    the old file's, when there is one; otherwise the process umask default)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            if callable(content): content(f)
            else: f.write(content)
        if mode is None:
            try: mode = os.stat(path).st_mode & 0o7777
            except OSError:
                um = os.umask(0); os.umask(um); mode = 0o666 & ~um
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise


def write_json(path, value, mode=None, **dump):
    write(path, lambda f: json.dump(value, f, **dump), mode=mode)


@contextlib.contextmanager
def locked(path):
    held = getattr(_HELD, 'paths', None)
    if held is None: held = _HELD.paths = {}
    key = os.path.abspath(path)
    if key in held:
        held[key] += 1
        try: yield
        finally: held[key] -= 1
        return
    os.makedirs(os.path.dirname(key), exist_ok=True)
    with open(key + '.lock', 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        held[key] = 1
        try: yield
        finally:
            del held[key]
            fcntl.flock(lk, fcntl.LOCK_UN)


def holding(path_fn):
    """Decorator: runs the function under locked(path_fn())."""
    def deco(fn):
        def run(*a, **k):
            with locked(path_fn()): return fn(*a, **k)
        run.__name__, run.__doc__, run.__wrapped__ = fn.__name__, fn.__doc__, fn
        return run
    return deco
