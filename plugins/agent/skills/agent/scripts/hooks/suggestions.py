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
- every new task is born under an epic (PLN0250), with the rule every path that creates a task shares since PLN0275
  (watch_core.epics, where the rule is written): an alias of `aliases`, else the words in common with the title of
  the epics of `epics`, else a new epic of the area of the subject ("plugin agent runner" -> "Plugins: agent runner"),
  created in the same sync; without `epics` no epic is created and the output says so on one line per task. A +1 never
  moves a task to another epic, and tasks created before this rule are not touched.

Config: {"type": "suggestions", "inbox": "data/suggestions.jsonl", "project": "PLN", "per_day": 3,
         "epics": [{"pid": "ABC0001", "title": "Relatórios e exportação"}, ...],
         "aliases": {"exportar csv": "ABC0001"}}
`epics` and `aliases` here are the fallback: `planou.epics` and `planou.epic_aliases` of the instance win, and the
sync of the sources, the product backlog and the health hook read the same ones (watch_core.epics.settings). An alias
to an epic missing from `epics` is skipped (the words decide) and flagged once in the output, so the config gets the
epic's pid. The epics created here are in cache/planou/epics.json, shared with the other paths.
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


# the epic picking is shared with the other paths that create a task (PLN0275): watch_core.epics
from watch_core.epics import (GENERIC, STOP, NO_SUBJECT_AREA, MAX_EPIC_TITLE, words, area_title, epic_key,  # noqa: E402,F401
                              pick_epic)
from watch_core import epics as E  # noqa: E402


def _norm(text):
    from watch_core.suggestions import norm
    return norm(text)


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
        # the epics and aliases: Planou's list, planou.epics or this hook's (watch_core.epics.settings, read in run)
        self.bad_aliases = set()

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
        st.setdefault('offset', 0); st.setdefault('tasks', {}); st.setdefault('days', {})
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
        # the epics created here live in cache/planou/epics.json, shared with the sync and the health hook (PLN0275);
        # the ones this hook kept in its own state before go there once
        own = E.load_own(p)
        if st.get('epics'):
            for slug, ep in st['epics'].items(): own.setdefault(slug, ep)
            E.save_own(p, own)
        st.pop('epics', None)
        epics, aliases, _ = E.settings(paths.ROOT, P=p, project=project, hook=self.cfg)
        picker = E.Picker(paths.NAME, epics, aliases, own)
        snapshot = copy.deepcopy((st['tasks'], st['days']))
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
                self.place(picker, t, e)
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
                # the new epic goes in the same batch, before its tasks (Planou resolves `epic` after the batch)
                for row in picker.rows([t.get('epic_own')], project): epic_batch[row['source_key']] = row
            batch[key] = body
        if batch:
            try:
                _, res = p._call('POST', '/agent/sync', {'agent': paths.NAME, 'generated_at': now.isoformat(),
                                                           'tasks': list(epic_batch.values()) + list(batch.values())})
            except p.PlanouError as ex:
                # nothing is lost: the offset stays and the tasks, counters and epics go back to before this batch
                msg = str(ex.message if getattr(ex, 'status', 0) else ex)[:200]
                st['tasks'], st['days'] = snapshot      # the new epics in `own` are not saved either
                if st.get('err') == msg: return None
                st['err'] = msg
                return {'kind': 'error', 'error': msg}
            st.pop('err', None)
            by_key = {f'{paths.NAME}:sug-{k}': k for k in batch}
            results = (res or {}).get('tasks') or []
            for r in results:
                if r.get('source_key') in epic_batch: picker.result(r)
            E.save_own(p, own)
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
                own_e = own.get(t.get('epic_own') or '') or {}
                ev_out['epic'] = {'pid': own_e.get('pid') if own_e else t.get('epic'), 'title': t.get('epic_title'),
                                  'new': bool(own_e) and own_e.get('first') == ev['key'], 'deleted': bool(own_e.get('deleted'))}
                if not t.get('epic'): ev_out['epic'] = None
            out.append(ev_out)
        bad = sorted(a for a in self.bad_aliases | picker.bad_aliases if a not in (st.get('bad_aliases') or []))
        if bad: st['bad_aliases'] = sorted(set(st.get('bad_aliases') or []) | set(bad))
        if not out and not bad: return None
        return {'kind': 'batch', 'events': out, 'bad_aliases': bad}

    def place(self, picker, t, e):
        """Puts the new task `t` under an epic: an existing one (its pid or source key) or a new one of the area."""
        hit = picker.place(e.get('subject'), e.get('title'), e['key'])
        t['epic'], t['epic_title'] = hit['epic'], hit['title']
        if hit['no_epics']: t['no_epics'] = True
        if hit['own'] and hit['epic']: t['epic_own'] = hit['own']

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
                    out.append(f'-- sem epics no config: {e.get("pid") or "?"} ficou sem epico; {E.NO_EPICS_HINT}')
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
        own = dict(st.get('epics') or {})
        try:
            from watch_core import planou
            planou.configure_agent(paths.NAME, paths.ROOT)
            own.update(E.load_own(planou))
        except (ImportError, OSError, ValueError):
            pass
        for e in own.values():
            flag = ' (apagado pela pessoa)' if e.get('deleted') else '' if e.get('created') else ' (ainda nao criado)'
            print(f'-- epico {e.get("pid") or "?"}: {e.get("title")} (criado aqui; areas: {", ".join(e.get("areas") or []) or "-"}){flag}')
        return True
