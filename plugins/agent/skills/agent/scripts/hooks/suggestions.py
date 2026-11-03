#!/usr/bin/env python3
"""Hook suggestions: the planou instance turns the suggestions of the other agents into Planou tasks (PLN0007).

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
     agent.py <instance> --suggestions selar <PID|sug-key> "<reason>"   the session of the agent that owns the project
         triaged the suggestion and an agent can do it (PLN0440): one sync of that task with only
         `agent_can_do` {value: true, reason}, the title the state keeps (the sync requires it; a title the person
         set stays hers in Planou) and `base_version`, so Planou's idle slot can pull it. Refused, with nothing sent,
         by watch_core.suggestions.seal_refusal (a `Decidir:`/`Decide:`/`Decisao:` title, or a title or subject about
         cost, billing, account, secret, credential, client, production or deploy: those stay with the person), for a
         reason that looks like a secret, a task not created yet, deleted by the person or already sealed here.
         `selar <PID|sug-key> -` sends `agent_can_do: false` (the seal comes off). A `conflict` (the person changed the
         task after the version kept here) writes nothing, not even the new version: the person's change wins. The
         seal goes once: a later +1 never sends it again and a second `selar` is refused, so a seal the person removed
         in Planou stays removed (Planou does not say who removed it; base_version is the only guard).
"""
import copy, hashlib, json, os, sys
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


def _name():
    """This agent's name in Planou (source keys, reporter, sync): "planou.agent" of the instance, else the instance
    (PLN0282). Read after _planou() configured the module."""
    try:
        from watch_core import planou
        return planou.name() or paths.NAME
    except Exception:
        return paths.NAME


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
        picker = E.Picker(_name(), epics, aliases, own)
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
                t = {'title': _short(e['title'], 200), 'subject': _short(e.get('subject'), 120) if e.get('subject') else None,
                     'reporter': e['agent'], 'description': description(e),
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
            body = {'source_key': f'{_name()}:sug-{key}', 'project': project, 'title': t['title'],
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
                _, res = p._call('POST', '/agent/sync', {'agent': _name(), 'generated_at': now.isoformat(),
                                                           'tasks': list(epic_batch.values()) + list(batch.values())})
            except p.PlanouError as ex:
                # nothing is lost: the offset stays and the tasks, counters and epics go back to before this batch
                msg = str(ex.message if getattr(ex, 'status', 0) else ex)[:200]
                st['tasks'], st['days'] = snapshot      # the new epics in `own` are not saved either
                if st.get('err') == msg: return None
                st['err'] = msg
                return {'kind': 'error', 'error': msg}
            st.pop('err', None)
            by_key = {f'{_name()}:sug-{k}': k for k in batch}
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
        ap.add_argument('--suggestions', nargs='*', metavar='ARG', default=None,
                        help='sugestoes dos agentes: tarefas criadas e o que espera; selar PID|sug-chave "motivo" '
                             '(a triagem: um agente pode fazer; "-" tira o selo)')

    def cli(self, a, ctx):
        args = getattr(a, 'suggestions', None)
        if args is None: return False
        if args[:1] == ['selar']: return self.seal(args[1:], ctx)
        if args: raise SystemExit('uso: --suggestions | --suggestions selar PID|sug-chave "motivo" | --suggestions selar PID|sug-chave -')
        st = ctx.s.get('suggestions') or {}
        entries, _ = self.new_lines(st.get('offset', 0))
        tasks = st.get('tasks') or {}
        print(f'{len(tasks)} sugestao(oes) no Planou; {len(entries)} esperando na caixa ({self.path()})')
        for t in tasks.values():
            flag = ' (apagada)' if t.get('deleted') else ' (descricao da pessoa)' if t.get('frozen') else ''
            if t.get('seal') and not t.get('deleted'): flag += f' (selada: {t["seal"]})'
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


    # ------------------------------------------------------------ the seal (PLN0440)

    @staticmethod
    def subject_of(t):
        if t.get('subject'): return t['subject']
        for line in str(t.get('description') or '').splitlines():
            if line.startswith('Assunto: '): return line[len('Assunto: '):]
        return ''

    @classmethod
    def seal_problem(cls, t):
        """Why this suggestion task never takes the seal ('' = it can): the shared refusal of watch_core.suggestions."""
        from watch_core.suggestions import seal_refusal
        return seal_refusal(t.get('title'), cls.subject_of(t))

    def seal(self, args, ctx):
        if len(args) < 2: raise SystemExit('uso: --suggestions selar PID|sug-chave "motivo"  (ou "-" para tirar o selo)')
        ref, reason = args[0].strip(), ' '.join(args[1:]).strip()
        # read outside the lock only to decide; the write below reads the state again under core.state_lock()
        tasks = (ctx.s.get('suggestions') or {}).get('tasks') or {}
        key = ref[4:] if ref.lower().startswith('sug-') else ref
        if key not in tasks:
            key = next((k for k, t in tasks.items() if str(t.get('pid') or '').upper() == ref.upper()), None)
        if not key: raise SystemExit(f'{ref}: nao e uma sugestao desta instancia (ver --suggestions)')
        t = tasks[key]
        name = t.get('pid') or f'sug-{key}'
        if t.get('deleted'): raise SystemExit(f'{name}: a pessoa apagou essa sugestao; nada enviado')
        if not t.get('created') or not t.get('pid'):
            raise SystemExit(f'{name}: ainda nao foi criada no Planou (vai no proximo tick); selar depois')
        off = reason == '-'
        if not off:
            if not reason: raise SystemExit('diga o motivo curto de um agente poder fazer')
            why = self.seal_problem(t)
            if why: raise SystemExit(f'{name}: {why}; nada enviado')
            if t.get('seal'):
                raise SystemExit(f'{name}: ja selada aqui ("{t["seal"]}"); nada enviado. Se a pessoa tirou o selo no '
                                 'Planou, fica tirado')
            p = _planou()
            if p and p.looks_secret(reason): raise SystemExit(f'{name}: o motivo parece ter um segredo: reescreva sem ele')
            reason = _short(reason, 200)
        else:
            p = _planou()
        if not p: raise SystemExit(f'{name}: Planou desligado (ou modo teste, ou sem chave): nada enviado')
        project = self.project or p._S.get('project')
        # the sync requires the title: the one kept here (a title the person set stays hers, Planou skips it); no
        # description, priority, state or reporter, and base_version so a change of the person wins (conflict)
        body = {'source_key': f'{_name()}:sug-{key}', 'title': _short(t.get('title'), 200),
                'agent_can_do': {'value': False, 'reason': None} if off else {'value': True, 'reason': reason}}
        if t.get('version') is not None: body['base_version'] = t['version']
        if project: body['project'] = project
        try:
            _, res = p._call('POST', '/agent/sync', {'agent': _name(), 'generated_at': _now().isoformat(), 'tasks': [body]})
        except p.PlanouError as ex:
            raise SystemExit(f'{name}: o Planou nao respondeu ({str(ex.message if getattr(ex, "status", 0) else ex)[:200]}); '
                             'nada mudou, tentar de novo')
        r = next((x for x in (res or {}).get('tasks') or [] if x.get('source_key') == body['source_key']), {})
        if r.get('result') == 'ignored':
            raise SystemExit(f'{name}: a pessoa apagou essa sugestao no Planou; nada feito')
        if r.get('result') == 'conflict':
            # nothing saved, not even the new version: a second selar conflicts again, so the change of the person
            # (the seal she removed, the title she wrote) is never overwritten from here
            raise SystemExit(f'{name}: a pessoa mexeu na tarefa no Planou depois do ultimo envio daqui; nada mudou. '
                             'Conferir a tarefa com ela')
        if r.get('result') not in ('created', 'updated', 'unchanged'):
            raise SystemExit(f'{name}: o Planou recusou ({r.get("result")}: {r.get("reason") or "sem motivo"})')
        # only this task's seal and version, on the state read again under the lock: the runner may have saved the
        # offset and the other tasks in the meantime
        with core.state_lock():
            s = core.load_state()
            cur = ((s.get('suggestions') or {}).get('tasks') or {}).get(key)
            if cur is not None:
                if off: cur.pop('seal', None)
                else: cur['seal'] = reason
                if r.get('version') is not None: cur['version'] = r['version']
                core.save_state(s)
        print(f'{name}: selo tirado' if off else f'{name}: selada ("um agente pode fazer": {reason}); '
              'a vaga livre do Planou pode puxar')
        return True
