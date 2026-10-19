"""Foco do dia (Planou 0.93.0, PLN0379): the agent that keeps a project's order recalculates its up to 3 tasks of the day.

Planou keeps, per project and per local day, up to 3 tasks that close the day (GET/POST /v1/agent/projects/{key}/focus).
The order is written by the project's owner agent, or by the agent `planou` for a project without an owner (the GET says
so: `can_write`). The server builds each day once by its rule; from then on only the keeper agent changes it, so a task
that gets blocked, a new due date or a promise for a day that arrives (a promise made for a future day pins nothing when
the day comes, PLN0372) only reaches the focus through this module.

Every heavy tick of an instance with Planou on calls keep() after the sync (planou_tick.after and agent.planou_after),
one GET per project this agent is allocated to (plus its configured project), only when something it knows changed (the
fingerprint of its own tasks and queue) or every FOCUS_EVERY (the person's promises, pins and removals reach the plugin by
no event). A project where `can_write` is false is asked again after NOT_KEEPER_EVERY.

The order (plan(), pure):
  1. a `locked` slot (pinned by the person, or done today in the focus) stays in its exact position;
  2. a task in `removed` (the person took it out today) never comes back on this day;
  3. every open promise for today (`promises`, done false) is in the focus and never leaves it;
  4. the other places follow the server's rule over what the agent knows (its synced tasks in cache/planou/state.json and
     its queue in cache/planou/queue.json): blocks another task, overdue or due today, P1, then priority and date;
  5. a slot already in the focus wins a tie, and a task the agent does not know keeps the rank of its slot's reason_kind
     (the agent never sees the whole project: it never drops a slot only because it does not know the task). A partial
     view only raises a slot: `blocks` never falls (the server looks at the dependencies of the whole project, the agent
     only at its queue's), `overdue`/`due_today` falls only when the agent knows both the due date and the Prazo
     (`deadline`, kept in state.json for the tasks it synced) and `priority` only when it knows the priority. A slot that
     stays keeps the server's reason_kind and reason when the agent has no better one (the POST rewrites them);
  6. a known task that got closed, went to backlog, got blocked or waits on someone leaves (unless locked or promised).
A day nobody built yet (`last_change_at` null) is left to the server: it builds it by its rule, with the whole project
in view, when the person opens the focus, and an order from the agent first would count as that build. The queue is read
fresh (GET /agent/queue, once per tick that reads a focus) when the queue is on: queue.json is a snapshot of the
hand-out, so without the fresh list its priority and dependencies are not used. A 422 that names an item (a task the
agent does not know went to backlog or closed) drops it and tries once more.
It writes only when the sequence of tasks changes, with the reason in one line (who entered and left, and why). Dates,
priority, promises, pins and removals are the person's: the request carries only the order and the reasons.

Answers: 409 focus_conflict (another write at the same time) reads again, recalculates and tries once more; any other
refusal (focus_locked, focus_removed, focus_not_keeper, 422) is an `AVISO (planou)` line (cache/planou/avisos.log,
never wakes the session). What was written goes to cache/planou/focus.log. The tick prints nothing for a write: the
history is on the Planou screen.

Foco de amanhã (Planou 0.100.0, PLN0405/PLN0423): at the day's close (the first heavy tick at or after
daily.HORA_FECHAMENTO, BRT, on a workday: the same close as the == DAILY, when the agent already prepares the next day)
the keeper also leaves the focus of the next workday ready, once per project and day. The target is the workday after
the `day` today's GET answers (the workspace's local date, never the machine's: from Friday it is Monday), read with
`?day=` so the person's pins for it ("Fixar amanhã também", `locked`) and its promises keep their places. That day
nobody built yet, so plan(build=True) writes it from what the agent knows, but only an order that fills all `max` places
(the person's locked ones count): a shorter one is not written and the day is left to the server's rule, which looks at
the whole project (urgent tasks of other people too). The server never builds by its rule a day that has a keeper and
any change, so a short order from the agent would start the day with empty places. The POST carries `day`. A GET that answers
another day (an older Planou ignores `?day=`) writes nothing; 409 focus_conflict or focus_locked (the person just
pinned) reads again and tries once; a 422 on `day` or any other refusal is one AVISO line, and the day is marked done
either way, also when nothing is written (only a network failure, a 429 or a 5xx is tried again on the next tick), so
nothing loops. Every AVISO of this step reads `foco de AAAA-MM-DD <key>`. The close is BRT on purpose, the same clock as
the == DAILY (this installation's workspace is in BRT); the target day itself always comes from the server's `day`.

Off switch: "planou": {"focus": false} in the instance config.
"""
import hashlib, json, re
from datetime import date, datetime, timedelta, timezone

from . import daily, planou as P

FILE = 'focus.json'
LOG = 'focus.log'
FOCUS_EVERY = timedelta(minutes=15)
NOT_KEEPER_EVERY = timedelta(hours=1)
REASON_MAX = 300
ITEM_REASON_MAX = 200
FROM_AGENT = ('promised', 'blocks', 'due_today', 'overdue', 'priority', 'other')
RANK_OF_KIND = {'promised': 0, 'person': 0, 'blocks': 1, 'overdue': 2, 'due_today': 2, 'priority': 3, 'other': 4}
OUT_STATES = ('closed', 'backlog', 'blocked', 'waiting')       # a known task in one of these leaves (unless locked/promised)
NEVER_SEND = ('closed', 'backlog')                             # the server refuses these outside their locked slot
KIND_TEXT = {'promised': 'prometida para hoje', 'blocks': 'outra tarefa espera esta', 'overdue': 'atrasada',
             'due_today': 'vence hoje', 'priority': 'P1', 'other': 'continua no foco'}
NEXT_TEXT = {**KIND_TEXT, 'promised': 'prometida para o dia', 'due_today': 'vence no dia'}
NEXT_PREFIX = 'Fechamento do dia: '
OUT_TEXT = {'closed': 'concluida', 'backlog': 'voltou ao backlog', 'blocked': 'bloqueada', 'waiting': 'aguardando alguem'}
_PID = re.compile(r'^([A-Z]{2,6})\d{3,}$')


def _project_of(pid):
    m = _PID.match(str(pid or ''))
    return m.group(1) if m else None


def _day(v):
    try: return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError: return None


def known(priorities=None, remote=None):
    """{pid: {'pid', 'project', 'state', 'priority', 'due', 'deadline', 'dates', 'blocks'}}: what this agent knows of its
    own tasks (state.json: the tasks it synced, kept current by task_changed) and of its queue. `priorities` ({pid: 1..4})
    fills the priority of synced tasks (state.json does not keep it). `dates` is True when both the due date and the Prazo
    are known (a synced task whose entry keeps `deadline`, None for none): only then can a date make a slot fall.
    `remote` is the tasks of GET /agent/queue (fresh priority, due date, block and blocked_by: a blocker of a queue task
    blocks an open task; no Prazo). Without it, queue.json is a snapshot taken when the task was handed out: its
    priority and dependencies may be stale, so they go without them (a partial view only raises a slot, never lowers
    it)."""
    out, synced = {}, {}
    for t in (P._load('state.json', {}).get('tasks') or {}).values():
        if not isinstance(t, dict) or not t.get('pid'): continue
        pid = str(t['pid'])
        st = 'backlog' if t.get('backlog') else t.get('state')
        out[pid] = synced[pid] = {'pid': pid, 'project': _project_of(pid), 'state': st,
                                  'priority': (priorities or {}).get(pid), 'due': t.get('due'),
                                  'deadline': t.get('deadline'), 'dates': 'deadline' in t, 'blocks': False}
    if remote is not None:
        blockers = {}
        for r in remote:
            for b in r.get('blocked_by') or []:
                if isinstance(b, dict) and b.get('pid') and b.get('open') is not False: blockers[str(b['pid'])] = True
        for r in remote:
            if not isinstance(r, dict) or not r.get('pid'): continue
            pid = str(r['pid'])
            kind = (r.get('mark') or {}).get('kind') if isinstance(r.get('mark'), dict) else None
            st = 'blocked' if r.get('blocked') or kind == 'blocked' else 'waiting' if kind == 'waiting' else 'todo'
            s = synced.get(pid) or {}          # the queue view has no Prazo: the synced entry's, when it keeps one
            out[pid] = {'pid': pid, 'project': r.get('project') or _project_of(pid), 'state': st,
                        'priority': r.get('priority') if isinstance(r.get('priority'), int) else None,
                        'due': r.get('due'), 'deadline': s.get('deadline'), 'dates': bool(s.get('dates')),
                        'blocks': pid in blockers}
        for pid in blockers:
            if pid in out: out[pid]['blocks'] = True
            else: out[pid] = {'pid': pid, 'project': _project_of(pid), 'state': 'todo', 'priority': None, 'due': None,
                              'deadline': None, 'dates': False, 'blocks': True}
        return out
    for t in P._queue()['tasks'].values():
        if not isinstance(t, dict) or t.get('left') or not t.get('pid'): continue
        pid = str(t['pid'])
        prog = t.get('state')
        st = 'blocked' if prog == 'blocked' else 'closed' if prog == 'done' else 'in_progress' if prog in ('started', 'in_review') else 'todo'
        out[pid] = {'pid': pid, 'project': t.get('project') or _project_of(pid), 'state': st, 'priority': None,
                    'due': t.get('due'), 'deadline': t.get('deadline'), 'dates': False, 'blocks': False}
    return out


def rank(t, day):
    """(rank, reason_kind) of a known task by the server's rule; rank 4 ('other') when nothing makes it urgent. Overdue
    is by the Prazo when there is one, else by the due date; due today when either date is today or past."""
    if t.get('blocks'): return 1, 'blocks'
    due, dl = _day(t.get('due')), _day(t.get('deadline'))
    if (dl < day) if dl else (due and due < day): return 2, 'overdue'
    if (due and due <= day) or (dl and dl <= day): return 2, 'due_today'
    if t.get('priority') == 1: return 3, 'priority'
    return 4, 'other'


def _item_reason(kind, t=None, slot=None, texts=KIND_TEXT):
    if slot and slot.get('reason') and kind == slot.get('reason_kind'): return str(slot['reason'])[:ITEM_REASON_MAX]
    d = t and (t.get('deadline') or t.get('due'))
    if kind == 'overdue' and d: return f'atrasada desde {str(d)[8:10]}/{str(d)[5:7]}'
    return texts.get(kind)


def _may_lower(kind, to, t):
    """Whether what the agent knows of `t` is enough to take a slot of the server's `kind` down to rank `to`: never a
    `blocks` slot (the server sees the dependencies of the whole project); below a date (rank > 2) only with both dates
    known; below P1 (rank > 3) only with the priority known."""
    if kind == 'blocks': return False
    if to > 2 and not t.get('dates'): return False
    if to > 3 and t.get('priority') is None: return False
    return True


def plan(focus, tasks, refused=(), build=False):
    """(items, reason) of the new order, or None when the order stays (or no valid order exists). `focus` is the GET
    answer, `tasks` the known() of this project, `refused` the pids the server just said are not open. A day nobody built
    yet (no change, `last_change_at` null) is left alone: the server builds it by its rule, with the whole project in
    view, when the person opens the focus; an order from the agent first would count as the day's build. `build` is the
    day's close writing the next workday (PLN0423): that day is built from what the agent knows, with texts for that day
    and the reason prefixed with NEXT_PREFIX; an order that does not fill the `max` places (counting the locked ones) is
None: the server's rule builds that day, with the whole project in view."""
    if not build and not focus.get('last_change_at'): return None
    texts = NEXT_TEXT if build else KIND_TEXT
    if refused: tasks = {**tasks, **{pid: {**(tasks.get(pid) or {'pid': pid}), 'state': 'closed'} for pid in refused}}
    day = _day(focus.get('day')) or date.today()
    size = int(focus.get('max') or 3)
    slots = sorted((s for s in focus.get('slots') or [] if isinstance(s, dict) and s.get('pid')), key=lambda s: s.get('position') or 0)
    removed = {str(x) for x in focus.get('removed') or []}
    locked = {int(s['position']): s for s in slots if s.get('locked') and s.get('position')}
    locked_pids = {s['pid'] for s in locked.values()}
    current = [s['pid'] for s in slots]
    promised = []
    for p in focus.get('promises') or []:
        pid = isinstance(p, dict) and p.get('pid')
        if pid and not p.get('done') and pid not in removed and pid not in promised:
            if (tasks.get(pid) or {}).get('state') not in NEVER_SEND: promised.append(pid)
    pool, left = {}, {}          # pool: pid -> (rank, current?, priority, due, order, kind, reason)
    for i, pid in enumerate(promised):
        if pid in locked_pids: continue
        pool[pid] = (0, pid in current, 0, '', i, 'promised', texts['promised'])
    for s in slots:
        pid = s['pid']
        if pid in locked_pids or pid in pool: continue
        t = tasks.get(pid)
        if pid in removed: continue
        if t and t.get('state') in OUT_STATES:
            left[pid] = OUT_TEXT[t['state']]
            continue
        sk = s.get('reason_kind') if s.get('reason_kind') in RANK_OF_KIND else 'other'
        r, kind = RANK_OF_KIND[sk], sk
        if t:
            kr, kk = rank(t, day)
            if kr < r or (kr > r and _may_lower(sk, kr, t)): r, kind = kr, kk
        if kind == 'person': kind = 'other'
        pool[pid] = (r, True, (t or {}).get('priority') or 9, str((t or {}).get('due') or '9999'), s.get('position') or 9,
                     kind, _item_reason(kind, t, s, texts))
    for pid, t in tasks.items():
        if pid in pool or pid in locked_pids or pid in removed or pid in left: continue
        if t.get('state') in OUT_STATES: continue
        r, kind = rank(t, day)
        if r > 3: continue
        pool[pid] = (r, False, t.get('priority') or 9, str(t.get('due') or '9999'), 99, kind, _item_reason(kind, t, texts=texts))
    order = sorted(pool, key=lambda pid: (pool[pid][0], not pool[pid][1], pool[pid][2], pool[pid][3], pool[pid][4], pid))
    # promised first, then the rest; a promise never leaves: the free places go to them before anything else
    free = [i for i in range(1, size + 1) if i not in locked]
    chosen = order[:len(free)]
    out = []
    it = iter(chosen)
    for pos in range(1, size + 1):
        if pos in locked:
            out.append((locked[pos]['pid'], None, None, True))
            continue
        pid = next(it, None)
        if pid is None:
            if any(p > pos for p in locked): return None       # a hole before a locked slot: nothing valid to write
            break
        out.append((pid, pool[pid][5], pool[pid][6], False))
    if build and len(out) < size: return None     # a short day: left to the server's rule (it sees the whole project)
    new = [x[0] for x in out]
    if new == current: return None
    for pid in current:
        if pid not in new and pid not in left: left[pid] = 'deu lugar a uma tarefa mais urgente'
    entered = [(pid, k, r) for pid, k, r, lk in out if pid not in current]
    parts = [f'entrou {pid} ({r or texts.get(k, k)})' for pid, k, r in entered]
    parts += [f'saiu {pid} ({why})' for pid, why in left.items() if pid in current and pid not in new]
    if not parts: parts = ['nova ordem: ' + ', '.join(new)]
    reason = '; '.join(parts)
    reason = (reason[:1].upper() + reason[1:])
    if build: reason = NEXT_PREFIX + reason
    reason = reason[:REASON_MAX - 1].rstrip() + '.'
    items = []
    for pid, kind, why, lk in out:
        item = {'task': pid}
        if not lk and kind in FROM_AGENT:
            item['reason_kind'] = kind
            if why: item['reason'] = str(why)[:ITEM_REASON_MAX]
        items.append(item)
    return items, reason


def _fingerprint(day, tasks):
    rows = sorted((pid, t.get('state'), t.get('priority'), t.get('due'), t.get('deadline'), t.get('blocks'))
                  for pid, t in tasks.items())
    return hashlib.sha256(json.dumps([day, rows], default=str).encode()).hexdigest()[:16]


def _projects():
    keys = []
    alloc = P.allocation()
    for k in ((alloc or {}).get('keys') or []) + [P._S.get('project')]:
        if k and str(k).upper() not in keys: keys.append(str(k).upper())
    return keys


def _remote_queue():
    """The tasks of GET /agent/queue (fresh priority, due date and dependencies), or None (queue off, Planou older or down)."""
    if not P.queue_enabled(): return None
    try:
        _, res = P._call('GET', '/agent/queue')
    except P.PlanouError:
        return None
    tasks = (res or {}).get('tasks') if isinstance(res, dict) else None
    return tasks if isinstance(tasks, list) else None


def _get(key, day=None):
    _, data = P._call('GET', f'/agent/projects/{key}/focus' + (f'?day={day}' if day else ''))
    return data if isinstance(data, dict) else {}


def next_workday(day):
    """The workday after `day` (Monday to Friday), as the Planou counts it: tomorrow, or Monday from Friday to Sunday."""
    d = day + timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return d


def close_due(now):
    """Whether `now` is past the day's close: a workday at or after daily.HORA_FECHAMENTO in BRT (the == DAILY's close)."""
    local = now.astimezone(daily.BRT)
    return local.weekday() < 5 and local.hour >= daily.HORA_FECHAMENTO


def _transient(e):
    return not e.status or e.status >= 500 or e.status == 429


_ITEM = re.compile(r'^items\[(\d+)\]')


def _keep_one(key, tasks, now):
    """(can_write, lines, day) for one project: lines are AVISO only, day is the `day` today's GET answered (the
    day's close counts the next workday from it). Reads, plans, writes; one more try after a 409 focus_conflict (read
    again) or a 422 that names an item (a task the agent does not know went to backlog or closed: it leaves)."""
    refused = []
    day = None
    for attempt in (1, 2):
        focus = _get(key)
        day = focus.get('day') or day
        if not focus.get('can_write'): return False, [], day
        planned = plan(focus, tasks, refused)
        if not planned: return True, [], day
        items, reason = planned
        try:
            _, res = P._call('POST', f'/agent/projects/{key}/focus', {'reason': reason, 'items': items})
        except P.PlanouError as e:
            if attempt == 1 and e.code == 'focus_conflict': continue
            bad = [int(m.group(1)) for f in e.fields for m in [_ITEM.match(str(f))] if m]
            if attempt == 1 and e.code == 'validation' and bad and bad[0] < len(items):
                refused.append(items[bad[0]]['task'])
                continue
            return True, [f'AVISO (planou): foco do dia {key}: {str(e)[:200]}'], day
        if (res or {}).get('changed') is not False:
            P._log(LOG, f'{key}\t{" ".join(i["task"] for i in items)}\t{reason}', now)
        return True, [], day
    return True, [], day


def _next_one(key, tasks, now, today=None):
    """The day's close for one project: writes the focus of the next workday (PLN0423). `today` is the `day` the
    tick's GET of today already answered (read again only when that GET did not run). Returns (done, lines): done is
    False only after a failure worth another try (network, 429, 5xx); every answer of the server marks the day done,
    also a short order that is not written. A failure is one AVISO `foco de AAAA-MM-DD <key>` line."""
    target = _day(today) and next_workday(_day(today)).isoformat()
    try:
        if not target:
            answer = _get(key)
            if not answer.get('can_write') or not _day(answer.get('day')): return True, []
            target = next_workday(_day(answer.get('day'))).isoformat()
        return _write_next(key, tasks, now, target)
    except P.PlanouError as e:
        label = target or next_workday(now.astimezone(daily.BRT).date()).isoformat()    # today's GET failed: BRT guess
        return not _transient(e), [f'AVISO (planou): foco de {label} {key}: {str(e)[:200]}']


def _write_next(key, tasks, now, target):
    refused = []
    for attempt in (1, 2):
        focus = _get(key, target)
        if str(focus.get('day') or '')[:10] != target: return True, []      # an older Planou: it answered today
        if not focus.get('can_write'): return True, []
        planned = plan(focus, tasks, refused, build=True)
        if not planned: return True, []
        items, reason = planned
        try:
            _, res = P._call('POST', f'/agent/projects/{key}/focus', {'reason': reason, 'items': items, 'day': target})
        except P.PlanouError as e:
            if _transient(e): return False, [f'AVISO (planou): foco de {target} {key}: {str(e)[:200]}']
            if attempt == 1 and e.code in ('focus_conflict', 'focus_locked'): continue    # the person just pinned it
            bad = [int(m.group(1)) for f in e.fields for m in [_ITEM.match(str(f))] if m]
            if attempt == 1 and e.code == 'validation' and bad and bad[0] < len(items):
                refused.append(items[bad[0]]['task'])
                continue
            return True, [f'AVISO (planou): foco de {target} {key}: {str(e)[:200]}']
        if (res or {}).get('changed') is not False:
            P._log(LOG, f'{key}\t{target}\t{" ".join(i["task"] for i in items)}\t{reason}', now)
        return True, []
    return True, []


def keep(priorities=None, projects=None, now=None, force=False):
    """The heavy tick's step: keeps the focus of each project this agent may order and, at the day's close, leaves the
    next workday's ready (once per project and day). Returns AVISO lines (never raises)."""
    if not P.active(): return []
    now = now or datetime.now(timezone.utc)
    try:
        local = known(priorities)
        know = None                      # with the fresh queue, fetched once and only when a project is read
        st = P._load(FILE, {})
        seen = st.get('projects') if isinstance(st.get('projects'), dict) else {}
        closed = st.get('next') if isinstance(st.get('next'), dict) else {}
        close = close_due(now)
        close_day = now.astimezone(daily.BRT).date().isoformat()
        lines = []
        # the close is BRT on purpose (the == DAILY's clock; this workspace is in BRT): the target day is the server's
        for key in projects or _projects():
            day = now.astimezone().date().isoformat()
            fp = _fingerprint(day, {pid: t for pid, t in local.items() if (t.get('project') or '').upper() == key})
            last = seen.get(key) or {}
            try: age = now - datetime.fromisoformat(last.get('at'))
            except (TypeError, ValueError): age = None
            every = FOCUS_EVERY if last.get('can_write', True) else NOT_KEEPER_EVERY
            today_due = force or age is None or age >= every or last.get('fp') != fp
            next_due = close and (closed.get(key) or {}).get('on') != close_day
            if not today_due and not (next_due and last.get('can_write', True)): continue
            if know is None: know = known(priorities, _remote_queue())
            mine = {pid: t for pid, t in know.items() if (t.get('project') or '').upper() == key}
            today = None
            if today_due:
                try:
                    can, out, today = _keep_one(key, mine, now)
                except P.PlanouError as e:
                    if e.status == 404 and e.code in ('not_found', 'http'):      # an older Planou or an unknown project
                        can, out = False, []
                    else:
                        lines.append(f'AVISO (planou): foco do dia {key}: {str(e)[:200]}')
                        continue
                lines += out
                seen[key] = {'fp': fp, 'at': now.isoformat(), 'can_write': can}
            if next_due and seen.get(key, {}).get('can_write', True):     # the day's close: the next workday (PLN0423)
                done, out = _next_one(key, mine, now, today)
                lines += out
                if done: closed[key] = {'on': close_day, 'at': now.isoformat()}
        P._save(FILE, {'projects': seen, 'next': closed})
        return lines
    except Exception as e:                       # never costs the tick
        return [f'AVISO (planou): foco do dia: {type(e).__name__}: {str(e)[:160]}']
