"""Batch release in Planou: the rules the person sets in the project's Release section, the release queue mirrored there
and each deploy sent to the project's version history. Used by the hooks release_due and deploy_log.

- rules(): the "release" block of GET /v1/agent/projects/<project>, cached 10 min in cache/planou/release_rules.json (an
  answer without the block is cached too, so an older Planou is not asked on every tick; a failed call is not, so a short
  outage ends with Planou). None = use the local config.
- limit(branches): the projects whose current autonomy holds a release back: each branch answers to the project of its
  task, not to the config written when the instance was created (PLN0566). Same answer and cache as rules(). An instance
  that talks to Planou but gets no answer is held too, until it answers (no OK to ask for). [] = nothing holds it.
- mirror_queue(q): PUT /v1/agent/projects/<project>/release-queue with the whole queue; skipped when the same queue was
  already sent (cache/planou/release_queue.json keeps the last one sent).
- post_release(...): POST /v1/agent/projects/<project>/releases (Planou answers 201 created or 200 unchanged: a resend
  is harmless).

Nothing here raises: Planou off, test mode, no project or a failed call returns None / (False, reason). The local queue
file stays the source of truth; Planou only mirrors it.
"""
import json, re, urllib.parse
from datetime import datetime, timedelta, timezone

import paths

RULES_TTL = timedelta(minutes=10)
RULE_KEYS = ('min_branches', 'max_wait_min', 'e2e_every_h')


def _planou():
    """watch_core.planou configured for this instance, or None when it does not talk to Planou (off, test mode, no key,
    no project)."""
    try:
        from watch_core import planou
        pc = planou.configure_agent(paths.NAME, paths.ROOT)
        if not pc or not planou.active() or not planou._S.get('project'): return None
        return planou
    except Exception:
        return None


def _path(p, suffix='', project=None):
    return f'/agent/projects/{urllib.parse.quote(str(project or p._S["project"]))}{suffix}'


def _project(p, project, now, ttl):
    """{data: the valid "release" block or None, autonomy: the project's autonomy or None} of Planou's answer, cached."""
    cache = p._load('release_rules.json', {})
    hit = cache.get(project) or {}
    try: fresh = now - datetime.fromisoformat(hit.get('fetched_at')) < ttl
    except (TypeError, ValueError): fresh = False
    if fresh and 'autonomy' in hit: return hit
    try:
        _, body = p._call('GET', _path(p, project=project))
    except p.PlanouError:
        return {'data': None, 'autonomy': None}         # not cached: Planou back is the end of it
    body = body if isinstance(body, dict) else {}
    autonomy = body.get('autonomy') if isinstance(body.get('autonomy'), str) else None
    data = body.get('release')
    if not isinstance(data, dict) or not all(isinstance(data.get(k), (int, float)) and data.get(k) > 0 for k in RULE_KEYS):
        data = None
    cache[project] = hit = {'fetched_at': now.isoformat(), 'data': data, 'autonomy': autonomy}
    p._save('release_rules.json', cache)
    return hit


def rules(now=None, ttl=RULES_TTL):
    """{min_branches, max_wait_min, e2e_every_h, version_rule, integrator, customized} from Planou, or None."""
    p = _planou()
    if not p: return None
    return _project(p, p._S['project'], now or datetime.now(timezone.utc), ttl).get('data')


def project_of(pid):
    """The project of a task's pid: PLN0566 -> PLN. None when it is not a pid."""
    m = re.fullmatch(r'([A-Za-z]+)\d+', pid) if isinstance(pid, str) else None
    return m.group(1).upper() if m else None


def _queue_projects(p):
    """{pid: project} of the tasks in this instance's queue as Planou has them now (GET /agent/queue), or None when Planou
    did not answer. An older Planou without the route: {}."""
    try:
        _, res = p._call('GET', '/agent/queue')
    except p.PlanouError as e:
        return {} if e.status == 404 else None
    return {r.get('pid'): r.get('project') for r in (res or {}).get('tasks') or [] if isinstance(r, dict) and r.get('project')}


def limit(branches=None, now=None, ttl=RULES_TTL):
    """[(project, autonomy or None)]: the projects of `branches` (the configured one for a branch without a pid, and when
    there are no branches) that do not let a release go alone. Only an autonomous project does; an autonomy Planou did not
    say counts as held, the stricter side. [] when nothing holds it, or when the instance does not talk to Planou: then
    its config is the only rule, as it always was.

    The pid keeps the prefix the task was born with, so a task that moved is told by Planou's queue. That read is only
    for ttl 0, the moment the release goes (`start`): the tick looks at the prefix. A task outside this instance's queue
    (delivered, or with another employee) has no line there and also answers to the prefix of its pid."""
    p = _planou()
    if not p: return []
    now = now or datetime.now(timezone.utc)
    default = p._S['project']
    where = _queue_projects(p) if branches and ttl <= timedelta(0) else {}
    of = lambda b: (where or {}).get(b.get('pid')) or project_of(b.get('pid')) or default
    projects = sorted({of(b) for b in branches or []} or {default})
    if where is None: return [(x, None) for x in projects]    # the queue did not answer: where the tasks are is unknown
    levels = [(x, _project(p, x, now, ttl).get('autonomy')) for x in projects]
    return [(x, a) for x, a in levels if a != 'autonomous']


def limit_text(held):
    return ', '.join(f'{x} {a or "sem resposta do Planou"}' for x, a in held)


def limit_lines(held, name):
    """What to do about limit(): the person's OK for a project that holds the release; no OK for one that did not answer,
    where nothing is decided yet (the release still does not go)."""
    known, unknown = [h for h in held if h[1]], [h for h in held if not h[1]]
    out = []
    if known:
        out.append(f'autonomia: {limit_text(known)}: release pede o OK da pessoa, nao disparar sozinho')
        out.append(f'pedir com $PL pergunta; com o OK: agent.py {name} --release-queue start ok')
    if unknown:
        out.append(f'autonomia: {limit_text(unknown)}: nao disparar ainda e nao pedir OK; '
                   f'repetir agent.py {name} --release-queue start quando o Planou voltar')
    return out


def forget_rules():
    p = _planou()
    if p: p._save('release_rules.json', {})


def queue_body(q):
    return {'branches': [{'branch': b['branch'], 'pid': b.get('pid') or None, 'ready_at': b.get('ready_at')}
                         for b in q.get('branches') or []],
            'integrating_since': q.get('integrating_since') or None,
            'integrating': list(q.get('integrating') or [])}


def mirror_queue(q, force=False):
    """(sent, reason): True when Planou now has this queue (sent now or already there). reason: why not, or None."""
    p = _planou()
    if not p: return False, 'off'
    body = queue_body(q)
    sig = json.dumps(body, sort_keys=True, ensure_ascii=False)
    last = p._load('release_queue.json', {})
    if not force and last.get('project') == p._S['project'] and last.get('sig') == sig: return True, None
    try:
        p._call('PUT', _path(p, '/release-queue'), body)
    except p.PlanouError as e:
        if e.status == 404 and e.code == 'http': return False, 'off'        # an older Planou without the route
        return False, f'{e.status} {e.code}: {e.message}'[:200]
    p._save('release_queue.json', {'project': p._S['project'], 'sig': sig, 'at': datetime.now(timezone.utc).isoformat()})
    return True, None


def post_release(version, commit, deployed_at=None, changes='', kind='deploy'):
    """(ok, reason). ok also when Planou already had it (200 unchanged)."""
    p = _planou()
    if not p: return False, 'off'
    body = {'version': version, 'commit': commit, 'deployed_at': deployed_at, 'changes': changes or '', 'kind': kind}
    try:
        p._call('POST', _path(p, '/releases'), body)
    except p.PlanouError as e:
        return False, f'{e.status} {e.code}: {e.message}'[:200]
    return True, None


def active():
    return _planou() is not None
