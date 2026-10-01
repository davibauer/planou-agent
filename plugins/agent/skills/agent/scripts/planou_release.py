"""Batch release in Planou: the rules the person sets in the project's Release section, the release queue mirrored there
and each deploy sent to the project's version history. Used by the hooks release_due and deploy_log.

- rules(): the "release" block of GET /v1/agent/projects/<project>, cached 10 min in cache/planou/release_rules.json (a
  failure is cached too, so an older Planou without the block is not asked on every tick). None = use the local config.
- mirror_queue(q): PUT /v1/agent/projects/<project>/release-queue with the whole queue; skipped when the same queue was
  already sent (cache/planou/release_queue.json keeps the last one sent).
- post_release(...): POST /v1/agent/projects/<project>/releases (Planou answers 201 created or 200 unchanged: a resend
  is harmless).

Nothing here raises: Planou off, test mode, no project or a failed call returns None / (False, reason). The local queue
file stays the source of truth; Planou only mirrors it.
"""
import json, urllib.parse
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


def _path(p, suffix=''):
    return f'/agent/projects/{urllib.parse.quote(str(p._S["project"]))}{suffix}'


def rules(now=None, ttl=RULES_TTL):
    """{min_branches, max_wait_min, e2e_every_h, version_rule, integrator, customized} from Planou, or None."""
    p = _planou()
    if not p: return None
    now = now or datetime.now(timezone.utc)
    project = p._S['project']
    cache = p._load('release_rules.json', {})
    hit = cache.get(project) or {}
    try: fresh = now - datetime.fromisoformat(hit.get('fetched_at')) < ttl
    except (TypeError, ValueError): fresh = False
    if fresh: return hit.get('data')
    try:
        _, body = p._call('GET', _path(p))
        data = (body or {}).get('release') if isinstance(body, dict) else None
        if not isinstance(data, dict) or not all(isinstance(data.get(k), (int, float)) and data.get(k) > 0 for k in RULE_KEYS):
            data = None
    except p.PlanouError:
        data = None
    cache[project] = {'fetched_at': now.isoformat(), 'data': data}
    p._save('release_rules.json', cache)
    return data


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
