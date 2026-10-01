#!/usr/bin/env python3
"""Hook suggestions: the planou-dev instance turns the suggestions of the other agents into Planou tasks (PLN0007).

Any agent leaves a suggestion with `python3 -m watch_core.planou --agent <agent> sugestao ...` (watch_core.suggestions,
which explains why an inbox and not a direct sync). It lands as one JSON line in this instance's
data/suggestions.jsonl. On every heavy tick this hook reads the lines it has not read yet and sends one sync:

- a new suggestion (new title and subject) creates a task in the Planou project, without state (born in backlog: the
  person approves it), with `reporter: <agent that suggested>` and the description: what happened, what was expected,
  an example and the suggested priority;
- the same title and subject again (any agent, another day) adds a "+1 <agent> em <data>: ..." line to the description
  of the existing task (with base_version). A `conflict` (the person changed the task) stores the version Planou sent
  and sends the +1 again on the next tick (at most 3 times). Once the person edits the description by hand Planou keeps
  hers (the +1 is then only counted here);
- a suggestion the person deleted in Planou stays deleted (the sync answers `ignored`);
- at most `per_day` (3) suggestions per agent per day are taken, whatever the file says;
- every new task is born under an epic (PLN0250): the hook picks one of the project's epics by the subject and the
  title of the suggestion, and creates a new epic (in the same sync, `kind: "epic"`) when none fits. Without
  `epics` in the config no epic is known, so none is created (it would duplicate the person's epics): the task goes
  without a parent and the output says so on one line per task. Picking is
  deterministic: (1) an alias of the `aliases` table found as a phrase in the subject or the title (the longest wins);
  (2) words shared with the epic title, the subject's worth 2 and the title's worth 1, at least 2 points, generic
  words (planou, plugin, agente, de, e...) not counted; ties go to the first in the config. The new epic is named
  after the area of the subject ("plugin agent runner" -> "Plugins: agent runner", "planou: pedidos" -> "Planou:
  pedidos") and is matched again by later suggestions of the same area. A +1 never moves a task to another epic, and
  tasks created before this rule are not touched.

Config: {"type": "suggestions", "inbox": "data/suggestions.jsonl", "project": "PLN", "per_day": 3,
         "epics": [{"pid": "ABC0001", "title": "Relatórios e exportação"}, ...],
         "aliases": {"exportar csv": "ABC0001"}}
`epics` lists the open epics of the project with their pid: the /v1 has no route that lists a project's epics yet,
so the config tells the hook which ones exist (an epic missing there may be created again under the area name). An
`aliases` value is an epic title or pid of `epics`, or the title of an epic the hook created; an alias to any other
epic is skipped (the words decide) and flagged once in the output, so the config gets the epic's pid.
`project` defaults to the instance's planou.project. Without Planou (off, test mode, no key) the hook reads nothing and
the lines wait: the file is never truncated (other agents append to it), only the read offset moves.

Prints `== SUGESTOES (n)` with one line per task created or +1, and `AVISO (sugestoes)` once for a failure.

CLI: agent.py <instance> --suggestions   the tasks created so far and what is still waiting in the inbox
"""
import copy, hashlib, json, os
from datetime import datetime, timezone

import core, paths
from adapters import Gancho as Base

MAX_DESCRIPTION = 8000
MAX_RETRY = 3


def _now():
    return datetime.now(timezone.utc)


def _planou():
    try:
        from watch_core import planou
        planou.configure_agent(paths.NAME, paths.ROOT)
        return planou if planou.active() else None
    except Exception:
        return None


def _date_br(iso_day):
    try: return datetime.fromisoformat(str(iso_day)[:10]).strftime('%d/%m/%Y')
    except ValueError: return str(iso_day or '')


def _short(text, n=300):
    t = ' '.join(str(text or '').split())
    return t if len(t) <= n else t[:n - 1] + '…'


def description(e):
    lines = [f'Sugestão de {e["agent"]} em {_date_br(e.get("day"))}.']
    if e.get('subject'): lines.append(f'Assunto: {e["subject"]}')
    lines += ['', f'O que aconteceu: {e["what"]}', '', f'O que esperava: {e["expected"]}', '', f'Exemplo: {e["example"]}',
              '', f'Prioridade sugerida: P{e.get("priority") or 3}']
    return '\n'.join(lines)


def plus_one(e):
    return f'+1 {e["agent"]} em {_date_br(e.get("day"))}: {_short(e.get("what"))}'


# words that never decide an epic alone: they appear in every area
GENERIC = frozenset('planou plugin plugins agent agents agente agentes app tarefa tarefas'.split())
STOP = frozenset('de da do das dos e o a os as um uma uns umas em no na nos nas para pra por com sem que nao mais '
                 'ao aos se ou the and for of to in on is it'.split())
NO_SUBJECT_AREA = 'Sugestões dos agentes'
MAX_EPIC_TITLE = 80


def _norm(text):
    from watch_core.suggestions import norm
    return norm(text)


def _stem(w):
    return w[:-1] if len(w) > 4 and w.endswith('s') else w


def words(text):
    return {_stem(w) for w in _norm(text).split() if len(w) >= 3 and w not in STOP and _stem(w) not in GENERIC
            and w not in GENERIC}


def _has_phrase(phrase, text):
    return bool(phrase) and f' {phrase} ' in f' {text} '


def area_title(subject):
    """The title of a new epic, from the area of the subject: "plugin agent runner" -> "Plugins: agent runner",
    "planou: pedidos" -> "Planou: pedidos", "fila do agente" -> "Fila do agente"."""
    s = ' '.join(str(subject or '').replace(':', ' : ').split())
    toks = [t for t in s.split() if t != ':']
    if not toks: return NO_SUBJECT_AREA
    first = _norm(toks[0])
    if first in ('planou', 'plugin', 'plugins'):
        rest = ' '.join(toks[1:]).strip(' ,;.-')
        title = f'{"Planou" if first == "planou" else "Plugins"}: {rest or "geral"}'
    else:
        title = ' '.join(toks).strip(' ,;.-')
        title = title[:1].upper() + title[1:]
    return _short(title, MAX_EPIC_TITLE)


def epic_key(title):
    slug = '-'.join(_norm(title).split()) or 'geral'
    if len(slug) <= 60: return slug
    # two long areas with the same start must not share an epic
    return f'{slug[:53]}-{hashlib.sha1(slug.encode()).hexdigest()[:6]}'


def pick_epic(epics, aliases, subject, title):
    """The epic (one of `epics`, dicts with `title`) for a suggestion, or None. Deterministic: the longest alias found
    as a phrase in the subject or the title wins; else the most shared words (subject 2, title 1, at least 2); ties go
    to the first epic of the list."""
    ns, nt = _norm(subject), _norm(title)
    best = None
    for alias, target in aliases:
        na = _norm(alias)
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


class Gancho(Base):
    tipo = 'suggestions'
    json_key = 'suggestions'
    roda_em_dry = True

    def configura(self):
        c = self.cfg
        self.inbox = c.get('inbox') if isinstance(c.get('inbox'), str) else 'data/suggestions.jsonl'
        self.project = c.get('project') if isinstance(c.get('project'), str) else None
        try: self.per_day = max(1, int(c.get('per_day') or 3))
        except (TypeError, ValueError): self.per_day = 3
        self.epics = [{'pid': str(e['pid']).strip(), 'title': str(e['title']).strip()}
                      for e in (c.get('epics') if isinstance(c.get('epics'), list) else [])
                      if isinstance(e, dict) and str(e.get('pid') or '').strip() and str(e.get('title') or '').strip()]
        self.bad_aliases = set()
        self.aliases = {str(k): str(v) for k, v in (c.get('aliases') if isinstance(c.get('aliases'), dict) else {}).items()
                        if _norm(k) and str(v or '').strip()}

    def known_epics(self, own):
        """The configured epics, then the ones this hook created (not deleted by the person), in that order."""
        out = [dict(e, ref=e['pid'], own=False) for e in self.epics]
        for slug, e in own.items():
            if e.get('deleted'): continue
            out.append({'pid': e.get('pid'), 'title': e['title'], 'ref': e['source_key'], 'own': slug})
        return out

    def choose(self, own, e):
        """(epic dict with `ref`, or a new epic title) for the suggestion `e`."""
        known = self.known_epics(own)
        by = {}
        for k in known:
            by.setdefault(_norm(k['title']), k)
            if k.get('pid'): by.setdefault(_norm(k['pid']), k)
        aliases = []
        for alias, target in self.aliases.items():
            hit = by.get(_norm(target))
            # an alias to an epic missing from `epics` would create a copy of the person's epic: it falls through to
            # the words (and the config is flagged once in the output)
            if hit is None: self.bad_aliases.add(alias); continue
            aliases.append((alias, hit))
        # the areas of the epics this hook created match later suggestions of the same area
        for k in known:
            if k['own']:
                for a in own[k['own']].get('areas') or []: aliases.append((a, k))
        hit = pick_epic(known, aliases, e.get('subject'), e.get('title'))
        if hit is not None: return hit
        return area_title(e.get('subject'))

    def path(self):
        return paths.expand(self.inbox)

    def new_lines(self, offset):
        """(entries, new offset): complete lines after `offset` (a half-written last line waits for the next tick).
        A file smaller than the offset (recreated) is read from the start."""
        p = self.path()
        try:
            size = os.path.getsize(p)
        except OSError:
            return [], offset
        if size < offset: offset = 0
        with open(p, 'rb') as f:
            f.seek(offset)
            raw = f.read()
        end = raw.rfind(b'\n') + 1
        out = []
        for line in raw[:end].splitlines():
            try:
                e = json.loads(line.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(e, dict) and all(isinstance(e.get(k), str) and e.get(k) for k in
                                           ('agent', 'key', 'title', 'what', 'expected', 'example')):
                out.append(e)
        return out, offset + end

    def run(self, ctx):
        st = ctx.s.setdefault('suggestions', {})
        st.setdefault('offset', 0); st.setdefault('tasks', {}); st.setdefault('days', {}); st.setdefault('epics', {})
        if ctx.dry: return None
        p = _planou()
        if not p: return None
        project = self.project or p._S.get('project')
        if not project: return None
        entries, offset = self.new_lines(st['offset'])
        retry = {k: t for k, t in st['tasks'].items() if t.get('retry') and not t.get('deleted')}
        if not entries and not retry:
            st['offset'] = offset
            return None
        now = ctx.agora or _now()
        snapshot = copy.deepcopy((st['tasks'], st['days'], st['epics']))
        days = st['days']
        batch, events, touched = {}, [], dict(retry)
        for e in entries:
            day = str(e.get('day') or now.date().isoformat())[:10]
            counted = days.setdefault(day, {})
            if counted.get(e['agent'], 0) >= self.per_day:
                events.append({'kind': 'limit', 'agent': e['agent'], 'title': e['title']})
                continue
            counted[e['agent']] = counted.get(e['agent'], 0) + 1
            t = st['tasks'].get(e['key'])
            if t is None:
                t = {'title': _short(e['title'], 200), 'reporter': e['agent'], 'description': description(e),
                     'priority': int(e.get('priority') or 3) if str(e.get('priority') or 3).isdigit() else 3,
                     'plus': [], 'pid': None, 'version': None, 'created': False}
                self.place(st['epics'], t, e)
                st['tasks'][e['key']] = t
                events.append({'kind': 'new', 'key': e['key'], 'agent': e['agent']})
            else:
                if t.get('deleted'):
                    continue
                t['plus'].append({'agent': e['agent'], 'day': day})
                if not t.get('frozen'):
                    extra = '\n' + plus_one(e)
                    if len(t['description']) + len(extra) <= MAX_DESCRIPTION: t['description'] += extra
                events.append({'kind': 'plus', 'key': e['key'], 'agent': e['agent']})
            touched[e['key']] = t
        # keep only the last 14 days of counters
        for d in sorted(days)[:-14]: days.pop(d, None)
        epic_batch = {}
        for key, t in touched.items():
            body = {'source_key': f'{paths.NAME}:sug-{key}', 'project': project, 'title': t['title'],
                    'priority': t['priority'], 'reporter': t['reporter']}
            if not t.get('frozen'): body['description'] = t['description']
            if t.get('version') is not None: body['base_version'] = t['version']
            if not t.get('created') and t.get('epic'):
                # only at creation: a +1 (or a retry) never moves the task to another epic
                body['epic'] = t['epic']
                own = st['epics'].get(t.get('epic_own') or '')
                if own and not own.get('created') and not own.get('deleted'):
                    # the new epic goes in the same batch, before its tasks (Planou resolves `epic` after the batch)
                    epic_batch[own['source_key']] = {
                        'source_key': own['source_key'], 'project': project, 'title': own['title'], 'kind': 'epic',
                        'description': f'Épico das sugestões dos agentes sobre {own["title"]}, criado pelo {paths.NAME}.'}
            batch[key] = body
        if batch:
            try:
                _, res = p._call('POST', '/agent/sync', {'agent': paths.NAME, 'generated_at': now.isoformat(),
                                                           'tasks': list(epic_batch.values()) + list(batch.values())})
            except p.PlanouError as ex:
                # nothing is lost: the offset stays and the tasks, counters and epics go back to before this batch
                msg = str(ex.message if getattr(ex, 'status', 0) else ex)[:200]
                st['tasks'], st['days'], st['epics'] = snapshot
                if st.get('err') == msg: return None
                st['err'] = msg
                return {'kind': 'error', 'error': msg}
            st.pop('err', None)
            by_key = {f'{paths.NAME}:sug-{k}': k for k in batch}
            by_epic = {e['source_key']: slug for slug, e in st['epics'].items()}
            results = (res or {}).get('tasks') or []
            for r in results:
                slug = by_epic.get(r.get('source_key'))
                if slug and r.get('source_key') in epic_batch: self.epic_result(st['epics'][slug], r)
            for r in results:
                key = by_key.get(r.get('source_key'))
                if not key: continue
                t = st['tasks'][key]
                result = r.get('result')
                if r.get('warnings'): t['warnings'] = [_short(w, 200) for w in r['warnings'][:3]]
                else: t.pop('warnings', None)
                if result in ('created', 'updated', 'unchanged') and r.get('pid'):
                    t.update(pid=r['pid'], version=r.get('version'), created=True)
                    t.pop('retry', None)
                    if 'description' in (r.get('ignored_fields') or []): t['frozen'] = True
                elif result == 'conflict':
                    # the person changed the task (or another employee follows it): Planou answers with the current
                    # version; the +1 goes again on the next tick with it, at most MAX_RETRY times
                    if r.get('version') is not None: t['version'] = r['version']
                    t['retry'] = int(t.get('retry') or 0) + 1
                    if t['retry'] > MAX_RETRY: t.pop('retry', None); t['error'] = _short(r.get('reason'), 200)
                elif result == 'ignored':
                    t['deleted'] = True
                elif result == 'error':
                    t['error'] = _short(r.get('reason'), 200)
                t['result'] = result
        st['offset'] = offset
        out = []
        for ev in events:
            if ev['kind'] == 'limit':
                out.append({'kind': 'limit', 'agent': ev['agent'], 'title': _short(ev['title'], 80)})
                continue
            t = st['tasks'].get(ev['key']) or {}
            ev_out = {'kind': ev['kind'], 'agent': ev['agent'], 'pid': t.get('pid'), 'title': t.get('title'),
                      'priority': t.get('priority'), 'plus': len(t.get('plus') or []), 'result': t.get('result'),
                      'error': t.get('error'), 'frozen': bool(t.get('frozen')), 'warnings': t.get('warnings') or [],
                      'no_epics': bool(t.get('no_epics'))}
            if ev['kind'] == 'new':
                own = st['epics'].get(t.get('epic_own') or '') or {}
                ev_out['epic'] = {'pid': own.get('pid') if own else t.get('epic'), 'title': t.get('epic_title'),
                                  'new': bool(own) and own.get('first') == ev['key'], 'deleted': bool(own.get('deleted'))}
                if not t.get('epic'): ev_out['epic'] = None
            out.append(ev_out)
        bad = sorted(a for a in self.bad_aliases if a not in (st.get('bad_aliases') or []))
        if bad: st['bad_aliases'] = sorted(set(st.get('bad_aliases') or []) | set(bad))
        if not out and not bad: return None
        return {'kind': 'batch', 'events': out, 'bad_aliases': bad}

    def place(self, own, t, e):
        """Puts the new task `t` under an epic: an existing one (its pid or source key) or a new one of the area."""
        if not self.epics:
            # no epic of the project is known: a new one would duplicate the person's epics, so the task goes without
            # a parent and the output asks for `epics` in the config
            t['epic'], t['epic_title'], t['no_epics'] = None, None, True
            return
        hit = self.choose(own, e)
        if isinstance(hit, dict):
            t['epic'], t['epic_title'] = hit['ref'], hit['title']
            if hit['own']:
                t['epic_own'] = hit['own']
                area = _norm(e.get('subject'))
                areas = own[hit['own']].setdefault('areas', [])
                if area and area not in areas: areas.append(area)
            return
        slug = epic_key(hit)
        epic = own.get(slug)
        if epic and epic.get('deleted'):
            # the person deleted the epic of this area: the task goes without one rather than recreating it
            t['epic'], t['epic_title'] = None, None
            return
        if not epic:
            epic = own[slug] = {'title': hit, 'source_key': f'{paths.NAME}:epic-{slug}', 'pid': None, 'created': False,
                                'areas': [], 'first': e['key']}
        area = _norm(e.get('subject'))
        if area and area not in epic['areas']: epic['areas'].append(area)
        t['epic'], t['epic_title'], t['epic_own'] = epic['source_key'], epic['title'], slug

    @staticmethod
    def epic_result(epic, r):
        result = r.get('result')
        if result in ('created', 'updated', 'unchanged') and r.get('pid'):
            epic.update(pid=r['pid'], created=True)
            epic.pop('error', None)
        elif result == 'ignored':
            epic['deleted'] = True
        elif result == 'error':
            epic['error'] = _short(r.get('reason'), 200)

    def texto(self, v):
        if not v: return None
        if v['kind'] == 'error':
            return f'AVISO (sugestoes): sugestoes dos agentes esperam o Planou ({v["error"]}); vao no proximo tick'
        evs = v['events']
        out = [f'== SUGESTOES ({len(evs)})']
        for a in v.get('bad_aliases') or []:
            out.append(f'-- aviso: o apelido "{a}" aponta para um epico fora de `epics` no config; pôr o pid dele em `epics`')
        for e in evs:
            if e['kind'] == 'limit':
                out.append(f'-- limite: {e["agent"]} passou de {self.per_day} por dia; "{e["title"]}" ficou de fora')
            elif e.get('result') == 'error':
                out.append(f'-- recusada pelo Planou: {e["title"]} (de {e["agent"]}): {e.get("error")}')
            elif e.get('result') == 'conflict':
                out.append(f'-- {e["kind"] == "new" and "nova" or "+1"} {e.get("pid") or "?"}: {e["title"]} (de {e["agent"]}): '
                           f'a pessoa mexeu na tarefa; vai de novo no proximo tick')
            elif e.get('result') == 'ignored':
                out.append(f'-- {e["title"]} (de {e["agent"]}): a pessoa apagou essa sugestao; nada feito')
            elif e['kind'] == 'new':
                ep = e.get('epic')
                if ep:
                    where = (f'; epico {"novo " if ep.get("new") else ""}{ep.get("pid") or "?"} "{ep.get("title")}"')
                elif e.get('no_epics'):
                    where = '; sem epico'
                else:
                    where = '; sem epico (a pessoa apagou o epico da area)'
                out.append(f'-- nova {e.get("pid") or "?"}: {e["title"]} (de {e["agent"]}, P{e.get("priority")}; '
                           f'no backlog{where})')
                if not ep and e.get('no_epics'):
                    out.append(f'-- sem epics no config: {e.get("pid") or "?"} ficou sem epico; pôr os epicos abertos '
                               f'do projeto em `epics` do gancho suggestions')
                for w in e.get('warnings') or []:
                    out.append(f'   aviso do Planou: {w}')
            else:
                tail = '; descricao editada pela pessoa, +1 so contado aqui' if e.get('frozen') else ''
                out.append(f'-- +1 {e.get("pid") or "?"}: {e["title"]} (de {e["agent"]}; {e["plus"]} a mais){tail}')
        return '\n'.join(out)

    # ------------------------------------------------------------ CLI

    def args(self, ap):
        ap.add_argument('--suggestions', action='store_true', help='sugestoes dos agentes: tarefas criadas e o que espera')

    def cli(self, a, ctx):
        if not getattr(a, 'suggestions', False): return False
        st = ctx.s.get('suggestions') or {}
        entries, _ = self.new_lines(st.get('offset', 0))
        tasks = st.get('tasks') or {}
        print(f'{len(tasks)} sugestao(oes) no Planou; {len(entries)} esperando na caixa ({self.path()})')
        for t in tasks.values():
            flag = ' (apagada)' if t.get('deleted') else ' (descricao da pessoa)' if t.get('frozen') else ''
            epic = f'; epico {t.get("epic_title")}' if t.get('epic_title') else ''
            print(f'-- {t.get("pid") or "?"} P{t.get("priority")}: {t.get("title")} (de {t.get("reporter")}; '
                  f'+{len(t.get("plus") or [])}{epic}){flag}')
        for e in (st.get('epics') or {}).values():
            flag = ' (apagado pela pessoa)' if e.get('deleted') else '' if e.get('created') else ' (ainda nao criado)'
            print(f'-- epico {e.get("pid") or "?"}: {e.get("title")} (criado aqui; areas: {", ".join(e.get("areas") or []) or "-"}){flag}')
        return True
