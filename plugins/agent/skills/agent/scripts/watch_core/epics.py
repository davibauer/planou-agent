"""The epic of a new task the agent creates in Planou (PLN0250, PLN0275): one rule for every path that creates one.

Who uses it: the suggestions hook (hooks/suggestions.py), the sync of the sources (watch_core.planou.payload/sync), the
backlog of the product behavior (backlog.py, plan without "epic") and the health hook (hooks/health.py). The epic goes
only when the task is created: an update never moves a task to another epic (the person may move it on the screen).

Picking is deterministic, from a subject (the area: the suggestion's subject, the source of an item, the goal of a
plan, the health checks) and a title:
  1. an alias of the `aliases` table found as a phrase in the subject or the title (the longest wins);
  2. words shared with the epic title, the subject's worth 2 and the title's worth 1, at least 2 points; generic words
     (planou, plugin, agente, de, e...) do not count; ties go to the first epic of the config;
  3. none fits: a new epic named after the area of the subject ("plugin agent runner" -> "Plugins: agent runner",
     "planou: pedidos" -> "Planou: pedidos"), created in the same sync before its tasks (`kind: "epic"`, source key
     `<agent>:epic-<slug>`), and matched again by later tasks of the same area;
  4. no `epics` in the config: no epic is known, so none is created (it would duplicate the person's epics): the task
     goes without one and the caller says so ("sem epics no config").
An epic the person deleted (the sync answers `ignored`) is never created again: the tasks of that area go without one.

Where the epics come from (`settings`): the open epics Planou lists for the project (GET
/v1/agent/projects/{key}/epics, PLN0274), used only once the /v1 contract this plugin ships has that route and the call
answers; else `planou.epics` in the instance config; else the `epics` of the suggestions hook (where they lived before
PLN0275). The aliases: `planou.epic_aliases`, else the suggestions hook's `aliases`. `epics` is a list of
{"pid", "title"} with the open epics of the project; an alias value is an epic title or pid of the known epics, or the
title of an epic created here.

The epics created here are shared by every path of the instance in cache/planou/epics.json
({slug: {title, source_key, pid, created, deleted, areas, first}}), so the suggestions, the sync and the health hook use
the same area epic and the same "deleted by the person" mark.
"""
import hashlib, json, os

STORE = 'epics.json'
# words that never decide an epic alone: they appear in every area
GENERIC = frozenset('planou plugin plugins agent agents agente agentes app tarefa tarefas'.split())
STOP = frozenset('de da do das dos e o a os as um uma uns umas em no na nos nas para pra por com sem que nao mais '
                 'ao aos se ou the and for of to in on is it'.split())
NO_SUBJECT_AREA = 'Sugestões dos agentes'
MAX_EPIC_TITLE = 80
NO_EPICS_HINT = 'pôr os epicos abertos do projeto em `planou.epics` do config (ou em `epics` do gancho suggestions)'


def norm(text):
    from watch_core.suggestions import norm as n
    return n(text)


def _short(text, limit):
    s = ' '.join(str(text or '').split())
    return s if len(s) <= limit else s[:limit - 3].rstrip() + '...'


def _stem(w):
    return w[:-1] if len(w) > 4 and w.endswith('s') else w


def words(text):
    return {_stem(w) for w in norm(text).split() if len(w) >= 3 and w not in STOP and _stem(w) not in GENERIC
            and w not in GENERIC}


def _has_phrase(phrase, text):
    return bool(phrase) and f' {phrase} ' in f' {text} '


def area_title(subject):
    """The title of a new epic, from the area of the subject: "plugin agent runner" -> "Plugins: agent runner",
    "planou: pedidos" -> "Planou: pedidos", "fila do agente" -> "Fila do agente"."""
    s = ' '.join(str(subject or '').replace(':', ' : ').split())
    toks = [t for t in s.split() if t != ':']
    if not toks: return NO_SUBJECT_AREA
    first = norm(toks[0])
    if first in ('planou', 'plugin', 'plugins'):
        rest = ' '.join(toks[1:]).strip(' ,;.-')
        title = f'{"Planou" if first == "planou" else "Plugins"}: {rest or "geral"}'
    else:
        title = ' '.join(toks).strip(' ,;.-')
        title = title[:1].upper() + title[1:]
    return _short(title, MAX_EPIC_TITLE)


def epic_key(title):
    slug = '-'.join(norm(title).split()) or 'geral'
    if len(slug) <= 60: return slug
    # two long areas with the same start must not share an epic
    return f'{slug[:53]}-{hashlib.sha1(slug.encode()).hexdigest()[:6]}'


def pick_epic(epics, aliases, subject, title):
    """The epic (one of `epics`, dicts with `title`) for a task, or None. Deterministic: the longest alias found as a
    phrase in the subject or the title wins; else the most shared words (subject 2, title 1, at least 2); ties go to
    the first epic of the list."""
    ns, nt = norm(subject), norm(title)
    best = None
    for alias, target in aliases:
        na = norm(alias)
        if target is not None and (_has_phrase(na, ns) or _has_phrase(na, nt)):
            n = len(na.split())
            if best is None or n > best[0]: best = (n, target)
    if best: return best[1]
    ws, wt = words(subject), words(title)
    best = None
    for e in epics:
        ew = words(e.get('title'))
        score = 2 * len(ew & ws) + len(ew & (wt - ws))
        if score >= 2 and (best is None or score > best[0]): best = (score, e)
    return best[1] if best else None


# ------------------------------------------------------------------ config

def clean_epics(raw):
    return [{'pid': str(e['pid']).strip(), 'title': str(e['title']).strip()}
            for e in (raw if isinstance(raw, list) else [])
            if isinstance(e, dict) and str(e.get('pid') or '').strip() and str(e.get('title') or '').strip()]


def clean_aliases(raw):
    return {str(k): str(v) for k, v in (raw if isinstance(raw, dict) else {}).items() if norm(k) and str(v or '').strip()}


def _instance_config(root):
    for rel in ('config/config.json', 'config.json'):
        try:
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                c = json.load(f)
        except (OSError, ValueError):
            continue
        return c if isinstance(c, dict) else {}
    return {}


def _config_lists(c, hook=None):
    """(epics, aliases, where) from the config dict: `planou.epics`, else the suggestions hook's `epics`; aliases from
    `planou.epic_aliases`, else the suggestions hook's `aliases`."""
    pc = c.get('planou') if isinstance(c.get('planou'), dict) else {}
    hooks = c.get('hooks') if isinstance(c.get('hooks'), list) else c.get('ganchos') if isinstance(c.get('ganchos'), list) else []
    if not isinstance(hook, dict):
        hook = next((h for h in hooks if isinstance(h, dict) and (h.get('type') or h.get('tipo')) == 'suggestions'), {})
    aliases = clean_aliases(pc['epic_aliases'] if isinstance(pc.get('epic_aliases'), dict) else hook.get('aliases'))
    if isinstance(pc.get('epics'), list): return clean_epics(pc['epics']), aliases, 'planou.epics'
    if isinstance(hook.get('epics'), list): return clean_epics(hook['epics']), aliases, 'hooks.suggestions'
    return [], aliases, None


ROUTE = '/v1/agent/projects/{key}/epics'
_ROUTE_KNOWN, _REMOTE = [], {}


def route_known():
    """True when the /v1 contract this plugin ships (planou_v1.openapi.json, copied from Planou by
    tools/sync_planou_schema.py) has the route that lists a project's epics (PLN0274). Until the Planou that has it is
    released and the copy updated, the agent never calls it (a Planou without it answers 404 anyway)."""
    if not _ROUTE_KNOWN:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'planou_v1.openapi.json')) as f:
                _ROUTE_KNOWN.append(ROUTE in (json.load(f).get('paths') or {}))
        except (OSError, ValueError):
            _ROUTE_KNOWN.append(False)
    return _ROUTE_KNOWN[0]


def remote(P, project):
    """The open epics of `project` from Planou ([{"pid", "title"}]), or None when the route is unknown, Planou is off or
    the call fails (404 in an older Planou, network): the caller falls back to the config. Once per process."""
    if not project or P is None or not route_known(): return None
    if project in _REMOTE: return _REMOTE[project]
    out = None
    try:
        if P.active():
            import urllib.parse
            _, data = P._call('GET', f'/agent/projects/{urllib.parse.quote(str(project))}/epics')
            rows = (data or {}).get('epics') if isinstance(data, dict) else None
            if isinstance(rows, list):
                out = clean_epics([e for e in rows if isinstance(e, dict) and e.get('open', True) is not False])
    except Exception:       # PlanouError or a broken answer: the config decides
        out = None
    _REMOTE[project] = out
    return out


def settings(root=None, cfg=None, P=None, project=None, hook=None):
    """(epics, aliases, where) of the instance. The epics: the open epics Planou lists for `project` (PLN0274, when the
    route is known and answers with at least one), else `planou.epics` of the config, else the suggestions hook's
    `epics`. The aliases: `planou.epic_aliases`, else the suggestions hook's `aliases`. `where`: "planou",
    "planou.epics", "hooks.suggestions" or None (no epics: no epic is created). `hook`: the suggestions hook entry to
    fall back on (the hook passes its own; the other paths take the first one of the config)."""
    c = cfg if isinstance(cfg, dict) else _instance_config(os.path.expanduser(root)) if root else {}
    epics, aliases, where = _config_lists(c, hook)
    live = remote(P, project)
    if live: return live, aliases, 'planou'
    return epics, aliases, where


# ------------------------------------------------------------------ the epics created here (shared store)

def load_own(P):
    """{slug: epic} created by this instance, from cache/planou/epics.json (`P`: watch_core.planou, configured)."""
    own = P._load(STORE, {})
    return own if isinstance(own, dict) else {}


def save_own(P, own):
    """Merges `own` into the store under its lock: an entry another path saved meanwhile keeps its pid, its "deleted"
    mark and its areas."""
    if not own: return
    with P._locked(STORE):
        cur = load_own(P)
        for slug, e in own.items():
            c = cur.get(slug)
            if not isinstance(c, dict):
                cur[slug] = e
                continue
            m = dict(c)
            if e.get('created') and e.get('pid'):
                m.update(pid=e['pid'], created=True); m.pop('error', None)
            elif e.get('error') and not m.get('created'):
                m['error'] = e['error']
            if e.get('deleted'): m['deleted'] = True
            areas = list(m.get('areas') or [])
            m['areas'] = areas + [a for a in e.get('areas') or [] if a not in areas]
            cur[slug] = m
        P._save(STORE, cur)


def description(title, agent):
    return f'Épico de {title}, criado pelo {agent} para as tarefas novas da área que não cabiam em nenhum épico aberto.'


class Picker:
    """Picks the epic of each new task of one run. `own` is the dict of the epics created here (load_own), changed in
    place: save it (save_own) only after Planou answered."""

    def __init__(self, agent, epics, aliases, own):
        self.agent = agent
        self.epics = clean_epics(epics)
        self.aliases = clean_aliases(aliases)
        self.own = own if isinstance(own, dict) else {}
        self.bad_aliases = set()

    def known(self):
        """The configured epics, then the ones created here (not deleted by the person), in that order."""
        out = [dict(e, ref=e['pid'], own=False) for e in self.epics]
        for slug, e in self.own.items():
            if not isinstance(e, dict) or e.get('deleted'): continue
            out.append({'pid': e.get('pid'), 'title': e['title'], 'ref': e['source_key'], 'own': slug})
        return out

    def choose(self, subject, title):
        """(epic dict with `ref`, or the title of a new epic)."""
        known = self.known()
        by = {}
        for k in known:
            by.setdefault(norm(k['title']), k)
            if k.get('pid'): by.setdefault(norm(k['pid']), k)
        aliases = []
        for alias, target in self.aliases.items():
            hit = by.get(norm(target))
            # an alias to an epic missing from `epics` would create a copy of the person's epic: it falls through to
            # the words (and the caller flags it once)
            if hit is None: self.bad_aliases.add(alias); continue
            aliases.append((alias, hit))
        # the areas of the epics created here match later tasks of the same area
        for k in known:
            if k['own']:
                for a in self.own[k['own']].get('areas') or []: aliases.append((a, k))
        hit = pick_epic(known, aliases, subject, title)
        return hit if hit is not None else area_title(subject)

    def place(self, subject, title, first):
        """The epic of a new task: {"epic": ref (pid or source key) | None, "title", "own": slug | None,
        "no_epics": bool, "new": bool (`first` is the task that asked for this new epic), "deleted": bool}."""
        if not self.epics:
            return {'epic': None, 'title': None, 'own': None, 'no_epics': True, 'new': False, 'deleted': False}
        hit = self.choose(subject, title)
        area = norm(subject)
        if isinstance(hit, dict):
            if hit['own']:
                areas = self.own[hit['own']].setdefault('areas', [])
                if area and area not in areas: areas.append(area)
            return {'epic': hit['ref'], 'title': hit['title'], 'own': hit['own'] or None, 'no_epics': False,
                    'new': False, 'deleted': False}
        slug = epic_key(hit)
        epic = self.own.get(slug)
        if epic and epic.get('deleted'):
            # the person deleted the epic of this area: the task goes without one rather than recreating it
            return {'epic': None, 'title': None, 'own': slug, 'no_epics': False, 'new': False, 'deleted': True}
        if not epic:
            epic = self.own[slug] = {'title': hit, 'source_key': f'{self.agent}:epic-{slug}', 'pid': None,
                                     'created': False, 'areas': [], 'first': first}
        if area and area not in epic['areas']: epic['areas'].append(area)
        return {'epic': epic['source_key'], 'title': epic['title'], 'own': slug, 'no_epics': False,
                'new': epic.get('first') == first and not epic.get('created'), 'deleted': False}

    def rows(self, slugs, project):
        """The sync rows of the new epics among `slugs` (not created yet, not deleted), to go before their tasks in
        the same batch (Planou resolves `epic` after the whole batch)."""
        out, seen = [], set()
        for slug in slugs:
            e = self.own.get(slug or '')
            if not e or e.get('created') or e.get('deleted') or slug in seen: continue
            seen.add(slug)
            row = {'source_key': e['source_key'], 'title': e['title'], 'kind': 'epic',
                   'description': description(e['title'], self.agent)}
            if project: row['project'] = project
            out.append(row)
        return out

    def is_epic(self, source_key):
        return any(isinstance(e, dict) and e.get('source_key') == source_key for e in self.own.values())

    def result(self, r):
        """Applies a sync result row of an epic created here. True when `r` was one."""
        sk = r.get('source_key')
        slug = next((s for s, e in self.own.items() if isinstance(e, dict) and e.get('source_key') == sk), None)
        if slug is None: return False
        epic, result = self.own[slug], r.get('result')
        if result in ('created', 'updated', 'unchanged') and r.get('pid'):
            epic.update(pid=r['pid'], created=True)
            epic.pop('error', None)
        elif result == 'ignored':
            epic['deleted'] = True
        elif result == 'error':
            epic['error'] = _short(r.get('reason'), 200)
        return True
