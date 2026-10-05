"""A thread pool map that a source's time budget can cut (PLN0385).

`with ThreadPoolExecutor(n) as ex: list(ex.map(...))` ignores the budget: the interrupt (agent.FonteLenta, raised by
SIGALRM in the main thread) leaves the `with` through shutdown(wait=True), which waits for every queued call to run to
the end. `mapa` cancels what was not started, does not wait for what is running, and sets `parar` so a call that is
running can give up between tries (`parar.wait(s)` in place of `time.sleep(s)`).
"""
from concurrent.futures import ThreadPoolExecutor


def mapa(fn, itens, n=2, parar=None):
    """list(map(fn, itens)) on `n` threads. On any interrupt (BaseException): cancels the calls not started, sets
    `parar` (a threading.Event, if given) and re-raises without waiting for the calls already running."""
    ex = ThreadPoolExecutor(n)
    try:
        out = list(ex.map(fn, itens))
    except BaseException:
        if parar is not None: parar.set()
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown()
    return out
