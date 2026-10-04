#!/usr/bin/env python3
"""Hook fila_parada: a queued task that Planou does not release although slots are free wakes the session (PLN0266).

Seen on 30/09: tasks sat "na fila, nao liberada" for 30 min with free slots (busy 0 of WIP 3) and nothing woke the
session; the column of the task had a category that pauses. On every heavy tick the hook reads the queue the way
`fila ver` does (GET /agent/queue: wip, busy, tasks with state, released, blocked_by). A task that is `queued`, not
released, without an open `blocked_by`, while busy < wip, is "stalled". Planou sends no time for it, so the hook times
it itself: the first tick that sees the task stalled starts the clock (cache of the instance, state.json). After
`after_min` minutes (default 20) it prints

  == FILA PARADA <PID> (<reason>)

once per task. The reason is what Planou sends in `release_reason` (or `reason`) when the field exists, else
"sem motivo visível". A task that stops being stalled (released, left, blocked, no free slot) is forgotten, so it is
told again only if it leaves and comes back stalled. Nothing is told while the agent is paused by the cost cap, when
Planou does not answer (the local copy of the queue says nothing about slots) or in a dry tick.

Config: {"type": "fila_parada", "after_min": 20}
"""
from datetime import datetime, timezone

import core
from adapters import Gancho as Base

NO_REASON = 'sem motivo visível'
REASON_KEYS = ('release_reason', 'reason')


def _now():
    return datetime.now(timezone.utc)


def _num(v, default):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default


def stalled(view):
    """The tasks of a queue view (see watch_core.planou.queue_view) that Planou holds back with free slots: [(key, row)].
    Empty when the view is not Planou's own answer or has no free slot."""
    if not isinstance(view, dict) or view.get('source') != 'planou': return []
    wip, busy = view.get('wip'), view.get('busy')
    if not isinstance(wip, int) or not isinstance(busy, int) or isinstance(wip, bool) or busy >= wip: return []
    out = []
    for r in view.get('tasks') or []:
        if not isinstance(r, dict) or r.get('state') != 'queued' or r.get('released') is not False: continue
        if any(isinstance(b, dict) and b.get('open') is not False for b in r.get('blocked_by') or []): continue
        key = str(r.get('pid') or r.get('task_id') or '')
        if key: out.append((key, r))
    return out


def reason(row, clean=lambda t, n=120: t):
    for k in REASON_KEYS:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            v = clean(v, 120)
            if v: return v
    return NO_REASON


class Gancho(Base):
    tipo = 'fila_parada'
    json_key = 'fila_parada'

    def configura(self):
        self.after = max(0.0, _num(self.cfg.get('after_min'), 20.0))

    def view(self):
        """(planou module, queue view) for this instance, or (None, None) when it does not talk to Planou."""
        try:
            from watch_core import planou
            import paths
            planou.configure_agent(paths.NAME, paths.ROOT)
            if not planou.active() or not planou.queue_enabled() or planou.paused(): return None, None
            return planou, planou.queue_view()
        except Exception:
            return None, None

    def run(self, ctx):
        if ctx.dry: return None
        now = ctx.agora or _now()
        planou, view = self.view()
        if view is None: return None
        st = ctx.s.setdefault('fila_parada', {})
        since, told = st.setdefault('since', {}), st.setdefault('told', {})
        rows = dict(stalled(view))
        for k in list(since):
            if k not in rows: since.pop(k); told.pop(k, None)
        for k in list(told):
            if k not in rows: told.pop(k)
        due = []
        for k, r in rows.items():
            first = core.dt(since.setdefault(k, now.isoformat())) or now
            if k in told or (now - first).total_seconds() / 60 < self.after: continue
            told[k] = now.isoformat()
            due.append({'pid': k, 'title': r.get('title'), 'reason': reason(r, planou.clean_text),
                        'minutes': (now - first).total_seconds() / 60, 'wip': view['wip'], 'busy': view['busy']})
        return {'tasks': due} if due else None

    def texto(self, v):
        if not v: return None
        out = []
        for t in v['tasks']:
            out.append(f'== FILA PARADA {t["pid"]} ({t["reason"]})')
            out.append(f'-- na fila, nao liberada ha {t["minutes"]:.0f} min com vaga livre (busy {t["busy"]} de WIP {t["wip"]})'
                       + (f': {t["title"]}' if t.get('title') else ''))
        out.append('-- seguir "Fila parada" do comportamento task-queue: conferir a categoria da coluna, o dono e a sessao, '
                   'e corrigir pela /api quando for configuracao')
        return '\n'.join(out)
