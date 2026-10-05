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

Off switch: "planou": {"focus": false} in the instance config.
"""
import hashlib, json, re
from datetime import date, datetime, timedelta, timezone

from . import planou as P

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


def _item_reason(kind, t=None, slot=None):
    if slot and slot.get('reason') and kind == slot.get('reason_kind'): return str(slot['reason'])[:ITEM_REASON_MAX]
    d = t and (t.get('deadline') or t.get('due'))
    if kind == 'overdue' and d: return f'atrasada desde {str(d)[8:10]}/{str(d)[5:7]}'
    return KIND_TEXT.get(kind)


def _may_lower(kind, to, t):
    """Whether what the agent knows of `t` is enough to take a slot of the server's `kind` down to rank `to`: never a
    `blocks` slot (the server sees the dependencies of the whole project); below a date (rank > 2) only with both dates
    known; below P1 (rank > 3) only with the priority known."""
    if kind == 'blocks': return False
    if to > 2 and not t.get('dates'): return False
    if to > 3 and t.get('priority') is None: return False
    return True


def plan(focus, tasks, refused=()):
    """(items, reason) of the new order, or None when the order stays (or no valid order exists). `focus` is the GET
    answer, `tasks` the known() of this project, `refused` the pids the server just said are not open. A day nobody built
    yet (no change, `last_change_at` null) is left alone: the server builds it by its rule, with the whole project in
    view, when the person opens the focus; an order from the agent first would count as the day's build."""
    if not focus.get('last_change_at'): return None
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
        pool[pid] = (0, pid in current, 0, '', i, 'promised', KIND_TEXT['promised'])
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
                     kind, _item_reason(kind, t, s))
    for pid, t in tasks.items():
        if pid in pool or pid in locked_pids or pid in removed or pid in left: continue
        if t.get('state') in OUT_STATES: continue
        r, kind = rank(t, day)
        if r > 3: continue
        pool[pid] = (r, False, t.get('priority') or 9, str(t.get('due') or '9999'), 99, kind, _item_reason(kind, t))
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
    new = [x[0] for x in out]
    if new == current: return None
    for pid in current:
        if pid not in new and pid not in left: left[pid] = 'deu lugar a uma tarefa mais urgente'
    entered = [(pid, k, r) for pid, k, r, lk in out if pid not in current]
    parts = [f'entrou {pid} ({r or KIND_TEXT.get(k, k)})' for pid, k, r in entered]
    parts += [f'saiu {pid} ({why})' for pid, why in left.items() if pid in current and pid not in new]
    if not parts: parts = ['nova ordem: ' + ', '.join(new)]
    reason = '; '.join(parts)
    reason = (reason[:1].upper() + reason[1:])[:REASON_MAX - 1].rstrip() + '.'
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


def _get(key):
    _, data = P._call('GET', f'/agent/projects/{key}/focus')
    return data if isinstance(data, dict) else {}


_ITEM = re.compile(r'^items\[(\d+)\]')


def _keep_one(key, tasks, now):
    """Lines for one project (AVISO only). Reads, plans, writes; one more try after a 409 focus_conflict (read again) or
    a 422 that names an item (a task the agent does not know went to backlog or closed: it leaves)."""
    refused = []
    for attempt in (1, 2):
        focus = _get(key)
        if not focus.get('can_write'): return False, []
        planned = plan(focus, tasks, refused)
        if not planned: return True, []
        items, reason = planned
        try:
            _, res = P._call('POST', f'/agent/projects/{key}/focus', {'reason': reason, 'items': items})
        except P.PlanouError as e:
            if attempt == 1 and e.code == 'focus_conflict': continue
            bad = [int(m.group(1)) for f in e.fields for m in [_ITEM.match(str(f))] if m]
            if attempt == 1 and e.code == 'validation' and bad and bad[0] < len(items):
                refused.append(items[bad[0]]['task'])
                continue
            return True, [f'AVISO (planou): foco do dia {key}: {str(e)[:200]}']
        if (res or {}).get('changed') is not False:
            P._log(LOG, f'{key}\t{" ".join(i["task"] for i in items)}\t{reason}', now)
        return True, []
    return True, []


def keep(priorities=None, projects=None, now=None, force=False):
    """The heavy tick's step: keeps the focus of each project this agent may order. Returns AVISO lines (never raises)."""
    if not P.active(): return []
    now = now or datetime.now(timezone.utc)
    try:
        local = known(priorities)
        know = None                      # with the fresh queue, fetched once and only when a project is read
        st = P._load(FILE, {})
        seen = st.get('projects') if isinstance(st.get('projects'), dict) else {}
        lines = []
        for key in projects or _projects():
            day = now.astimezone().date().isoformat()
            fp = _fingerprint(day, {pid: t for pid, t in local.items() if (t.get('project') or '').upper() == key})
            last = seen.get(key) or {}
            try: age = now - datetime.fromisoformat(last.get('at'))
            except (TypeError, ValueError): age = None
            every = FOCUS_EVERY if last.get('can_write', True) else NOT_KEEPER_EVERY
            if not force and age is not None and age < every and last.get('fp') == fp: continue
            if know is None: know = known(priorities, _remote_queue())
            mine = {pid: t for pid, t in know.items() if (t.get('project') or '').upper() == key}
            try:
                can, out = _keep_one(key, mine, now)
            except P.PlanouError as e:
                if e.status == 404 and e.code in ('not_found', 'http'):          # an older Planou or an unknown project
                    can, out = False, []
                else:
                    lines.append(f'AVISO (planou): foco do dia {key}: {str(e)[:200]}')
                    continue
            lines += out
            seen[key] = {'fp': fp, 'at': now.isoformat(), 'can_write': can}
        P._save(FILE, {'projects': seen})
        return lines
    except Exception as e:                       # never costs the tick
        return [f'AVISO (planou): foco do dia: {type(e).__name__}: {str(e)[:160]}']
