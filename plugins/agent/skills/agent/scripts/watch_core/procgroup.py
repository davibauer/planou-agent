"""Commands whose timeout stops the whole process group, not just the direct child (PLN0351).

subprocess.run(timeout=...) kills only the process it started: a `sh -c` command's children, or the git-remote-https a
`git fetch` starts, go on until they end by themselves (or never: a dev server). run() starts the command in a session
of its own and, on a timeout (or any exception while waiting, an interrupt included), stops that whole group: SIGTERM,
a short wait, then SIGKILL.

A process that left the group itself (setsid, a daemon) is not reached: that is the command's choice, not a leak of
the timeout.
"""
import os, signal, subprocess, time


def kill_group(pgid, grace=2.0, proc=None):
    """SIGTERM to the group, up to `grace` s for it to end, then SIGKILL. A group that is gone (or not ours) is left.
    `proc`: the leader's Popen, reaped while waiting (an unreaped leader keeps the group alive as a zombie)."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
        try: os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError): return
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if proc is not None: proc.poll()
            try: os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError): return
            time.sleep(0.05)


def run(args, timeout, input=None, **kw):
    """Like subprocess.run(args, capture_output=True, timeout=timeout, **kw), with the command in a process group of
    its own that is stopped whole on a timeout. Raises subprocess.TimeoutExpired (with what it printed) after the group
    is gone; returns a CompletedProcess otherwise."""
    kw.setdefault('stdout', subprocess.PIPE)
    kw.setdefault('stderr', subprocess.PIPE)
    with subprocess.Popen(args, start_new_session=True, stdin=kw.pop('stdin', subprocess.PIPE if input is not None
                                                                      else subprocess.DEVNULL), **kw) as p:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(p.pid, proc=p)
            try: out, err = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:      # a process outside the group still holds the pipe
                out, err = None, None
            raise subprocess.TimeoutExpired(args, timeout, output=out, stderr=err) from None
        except BaseException:
            kill_group(p.pid, 0.5, proc=p)
            raise
    return subprocess.CompletedProcess(args, p.returncode, out, err)
