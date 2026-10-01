#!/usr/bin/env python3
"""A goal becomes a refined backlog (PLN0115, behavior `product`): the agent writes the breakdown of a person's goal as a
JSON plan and this command creates the tasks in Planou in one sync.

  python3 backlog.py <instance> <plan.json|->  [--dry] [--max N] [--update]
  python3 backlog.py <instance> --list [GOAL]

Plan (JSON, keys in English; texts in the instance language):

  {"goal": {"ref": "PLN0200", "title": "A pessoa exporta a lista em CSV"},   ref: the goal task's PID, or a short slug
   "project": "PLN",              optional; default: the instance's planou.project
   "epic": true | "PLN0150",      optional; true creates an epic named after the goal, a PID links the tasks to that one
   "column": "A fazer",           optional; the project state for tasks that go out of backlog (autonomous project only)
   "tasks": [{"code": "csv-model", "title": "...", "what": "...", "why": "...", "done_when": ["...", "..."],
              "priority": "P2", "depends_on": ["other-code" | "PLN0123"], "agent_can_do": "why an agent can do it" | false,
              "decide": false, "ready_reason": "...", "estimate_h": 2,
              "details": ["Esboço no Planou:", "- ...", "Tamanho: M"]}]}   details: optional, one description line each

What goes up, per task (source_key `<agent>:meta-<goal>-<code>`, so a rerun never duplicates):
- title (with `Decidir: ` when `decide`), priority 1..4, description with "O que", "Por quê", "Pronto quando", the goal;
- `blocked_by` with the source keys of the same plan (Planou resolves them after the whole batch exists) or PIDs;
- `agent_can_do` {value, reason} (never on a `Decidir:` task);
- no state: the task is born in Backlog for the person to approve. Only in an autonomous project, a task with
  `ready_reason` goes to `todo` (and to `column`, when given: a column owned by another agent sends it to that agent's
  queue). In an autonomous project a backlog task goes WITHOUT `agent_can_do`: Planou's idle pull hands a backlog task
  with the seal to the agent that follows it, and that is this agent (the one that created it), not the one who builds
  it; the seal then goes as a line of the description.

Limits: at most `behavior_config.product.max_tasks` tasks per goal (default 8; `--max` after the person's OK), the
texts are checked for secrets, the plan is refused under `"confidentiality": "minimum"` and in test mode (`--dry`
works in both). A task already created is not sent again unless `--update` (then without state: the person's moves
win). The tasks are recorded in cache/planou/state.json and sent.json like the agent's other tasks (so `pronta`,
`fila refinar` and the events know them) and the plan in cache/planou/product_plans.json (`--list`).

Output: one line per task (CRIADA, ATUALIZADA, JA EXISTE, RECUSADA, AVISO) and `RESUMO: ...`, the note for the goal.
Exit 0 ok, 1 Planou refused or failed, 2 invalid plan or command.
"""
import argparse, json, os, re, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watch_core import planou as P   # noqa: E402

DEFAULT_MAX = 8
CODE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
PID_RE = re.compile(r'^[A-Za-z]{2,6}\d{3,}$')
TITLE_MAX, TITLE_SHORT, DESCRIPTION_MAX = 200, 80, 8000
DETAILS_MAX = 20
PRIORITIES = {'1': 1, '2': 2, '3': 3, '4': 4, 'p1': 1, 'p2': 2, 'p3': 3, 'p4': 4}
PLANS = 'product_plans.json'
STATE_LABEL = {'todo': 'A fazer', None: 'Backlog'}


class PlanError(Exception):
    pass


def _one(text, limit=None):
    t = ' '.join(str(text or '').split())
    return t[:limit] if limit else t


def slug(ref):
    s = re.sub(r'[^a-z0-9]+', '-', str(ref or '').lower()).strip('-')[:40].strip('-')
    if not s: raise PlanError('goal.ref: diga o PID da meta (ex.: PLN0200) ou um nome curto')
    return s


def instance_max(agent):
    try:
        with open(os.path.join(P.agent_root(agent), 'config', 'config.json'), encoding='utf-8') as f:
            c = json.load(f)
        v = ((c.get('behavior_config') or {}).get('product') or {}).get('max_tasks')
        return int(v) if isinstance(v, int) and v > 0 else DEFAULT_MAX
    except (OSError, ValueError, AttributeError):
        return DEFAULT_MAX


def description(t, goal):
    if t.get('decide'):
        lines = [f'O que decidir: {t["what"]}']
        if t.get('why'): lines.append(f'Por quê: {t["why"]}')
    else:
        lines = [f'O que: {t["what"]}', f'Por quê: {t["why"]}']
    lines += t.get('details') or []
    if t.get('done_when'):
        lines.append('Pronto quando:')
        lines += [f'- {d}' for d in t['done_when']]
    if t.get('_seal_note'): lines.append(t['_seal_note'])
    lines.append(f'Meta: {goal["ref"]}' + (f' ({goal["title"]})' if goal.get('title') else '') + '.')
    return '\n'.join(lines)


def validate(plan, known_codes=(), max_tasks=DEFAULT_MAX):
    """Normalized plan or PlanError with every problem found (nothing is sent when one exists)."""
    if not isinstance(plan, dict): raise PlanError('o plano precisa ser um objeto JSON com "goal" e "tasks"')
    err = []
    g = plan.get('goal')
    if isinstance(g, str): g = {'ref': g}
    if not isinstance(g, dict) or not str(g.get('ref') or '').strip():
        err.append('"goal": {"ref": "<PID da meta ou nome curto>", "title": "..."}')
        g = {'ref': 'meta'}
    goal = {'ref': _one(g.get('ref'), 60), 'title': _one(g.get('title'), 160)}
    try: goal['slug'] = slug(goal['ref'])
    except PlanError as e: err.append(str(e)); goal['slug'] = 'meta'
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or not tasks: raise PlanError('\n'.join(err + ['"tasks": a lista das tarefas (pelo menos uma)']))
    if len(tasks) > max_tasks:
        err.append(f'{len(tasks)} tarefas passam do limite de {max_tasks} por meta: pedir OK à pessoa ($PL pergunta) e '
                   f'rodar de novo com --max {len(tasks)}, ou juntar tarefas')
    out, codes, warns = [], set(), []
    for i, t in enumerate(tasks, 1):
        where = f'tarefa {i}'
        if not isinstance(t, dict): err.append(f'{where}: precisa ser um objeto'); continue
        code = str(t.get('code') or '').strip()
        if not CODE_RE.match(code): err.append(f'{where}: "code" curto, minúsculas, números e hífen (ex.: "csv-rota")'); code = f'#{i}'
        elif code in codes: err.append(f'{where}: "code" {code} repetido')
        codes.add(code)
        where = f'tarefa {code}'
        decide = t.get('decide') is True
        title = _one(t.get('title'))
        if decide: title = P._DECISION_HEAD.sub('', title) or title
        if not title: err.append(f'{where}: "title" vazio')
        full = (P.DECISION_PREFIX if decide else '') + title
        if len(full) > TITLE_MAX: err.append(f'{where}: título com {len(full)} caracteres (até {TITLE_MAX}; o ideal é até {TITLE_SHORT})')
        elif len(full) > TITLE_SHORT: warns.append(f'{where}: título longo ({len(full)} caracteres): o ideal é até {TITLE_SHORT}')
        what, why = _one(t.get('what')), _one(t.get('why'))
        if not what: err.append(f'{where}: "what" (o que fazer, ou o que decidir) vazio')
        if not why and not decide: err.append(f'{where}: "why" (por quê) vazio')
        done = t.get('done_when')
        if done is None or isinstance(done, str): done = [done] if done else []
        done = [_one(d) for d in done if _one(d)] if isinstance(done, list) else None
        if done is None: err.append(f'{where}: "done_when" precisa ser uma lista de critérios'); done = []
        if not done and not decide: err.append(f'{where}: "done_when" vazio: diga como se confere que ficou pronta')
        pr = t.get('priority', 'P3')
        prio = PRIORITIES.get(str(pr).strip().lower())
        if prio is None: err.append(f'{where}: "priority" {pr!r}: use P1, P2, P3 ou P4'); prio = 3
        deps = t.get('depends_on') or []
        if isinstance(deps, str): deps = [deps]
        if not isinstance(deps, list): err.append(f'{where}: "depends_on" precisa ser uma lista'); deps = []
        deps = [str(d).strip() for d in deps if str(d).strip()]
        can = t.get('agent_can_do')
        if decide and can: err.append(f'{where}: uma decisão da pessoa ("decide") não leva "agent_can_do"')
        if can is True: err.append(f'{where}: "agent_can_do": diga o motivo curto (texto), ou false')
        if can not in (None, False, True) and not isinstance(can, str): err.append(f'{where}: "agent_can_do" é um texto ou false')
        ready = _one(t.get('ready_reason'))
        if len(ready) > P.READY_MAX: err.append(f'{where}: "ready_reason" até {P.READY_MAX} caracteres')
        if ready and decide: err.append(f'{where}: uma decisão da pessoa fica no Backlog: sem "ready_reason"')
        est = t.get('estimate_h')
        if est is not None and (isinstance(est, bool) or not isinstance(est, (int, float)) or not 0 <= est <= 100000):
            err.append(f'{where}: "estimate_h" em horas, de 0 a 100000'); est = None
        details = t.get('details') or []
        details = [_one(d, 300) for d in details if _one(d)] if isinstance(details, list) else None
        if details is None: err.append(f'{where}: "details" precisa ser uma lista de linhas'); details = []
        if len(details) > DETAILS_MAX: err.append(f'{where}: "details" até {DETAILS_MAX} linhas')
        texts = [title, what, why, ready, can if isinstance(can, str) else ''] + done + details
        if any(P.looks_secret(x) for x in texts): err.append(f'{where}: um texto parece ter um segredo (token, chave): reescreva sem ele')
        out.append({'code': code, 'title': full, 'what': what, 'why': why, 'done_when': done, 'priority': prio,
                    'depends_on': deps, 'agent_can_do': _one(can, 200) if isinstance(can, str) else (False if can is False else None),
                    'decide': decide, 'ready_reason': ready or None, 'estimate_h': est, 'details': details})
    # dependencies: a code of this plan (or of an earlier run of the same goal) or a PID; no self, no cycle
    known = set(known_codes) | codes
    graph = {}
    for t in out:
        for d in t['depends_on']:
            if d == t['code']: err.append(f'tarefa {t["code"]}: depende dela mesma')
            elif d not in known and not PID_RE.match(d): err.append(f'tarefa {t["code"]}: "depends_on" {d!r} não é código do plano nem PID')
        graph[t['code']] = [d for d in t['depends_on'] if d in codes and d != t['code']]
        if len(t['depends_on']) > 20: err.append(f'tarefa {t["code"]}: até 20 dependências')
    state = {}

    def cyc(n, path):
        if state.get(n) == 1: return path[path.index(n):] + [n]
        if state.get(n) == 2: return None
        state[n] = 1
        for m in graph.get(n, []):
            c = cyc(m, path + [n])
            if c: return c
        state[n] = 2
        return None
    for n in graph:
        c = cyc(n, [])
        if c: err.append('dependência circular: ' + ' -> '.join(c)); break
    epic = plan.get('epic')
    if epic not in (None, False, True) and not (isinstance(epic, str) and PID_RE.match(epic.strip())):
        err.append('"epic": true (cria um épico com o nome da meta) ou o PID de um épico que já existe')
    if epic is True and not goal['title']: err.append('"epic": true precisa de "goal.title" (o nome do épico)')
    column = _one(plan.get('column'), 200) or None
    if err: raise PlanError('\n'.join(err))
    return {'goal': goal, 'project': _one(plan.get('project'), 20) or None, 'epic': epic.strip().upper() if isinstance(epic, str) else epic,
            'column': column, 'tasks': out, 'warnings': warns}


def _column(project, name):
    """(state dict or None, error) of `column` in the project."""
    data = P.project_states(project)
    if not data: return None, None          # unknown (older Planou, offline dry run): Planou answers with a warning
    for s in data.get('states') or []:
        if P.state_key(s.get('name')) == P.state_key(name): return s, None
    return None, f'"column": o projeto não tem o estado {name!r} (estados: {", ".join(s.get("name") or "" for s in data.get("states") or [])})'


def build(plan, agent, autonomy, now):
    """The sync body (tasks in the plan's order, the epic first) and, per code, where the task goes."""
    g = plan['goal']
    prefix = f'{agent}:meta-{g["slug"]}'
    project = plan['project'] or P._S.get('project')
    origin = {'type': 'other', 'label': _one(f'Meta {g["ref"]}' + (f' · {g["title"]}' if g['title'] else ''), 200),
              'url': None, 'author': None, 'at': now.isoformat()}
    body, where = [], {}
    epic_ref = None
    if plan['epic'] is True:
        epic_ref = prefix
        e = {'source_key': prefix, 'source_id': g['ref'], 'kind': 'epic', 'title': g['title'][:TITLE_MAX], 'priority': 3,
             'origin': origin, 'description': f'Épico da meta {g["ref"]}: {len(plan["tasks"])} tarefas refinadas pelo agente {agent}.'}
        if project: e['project'] = project
        body.append(e); where['(epico)'] = None
    elif plan['epic']:
        epic_ref = plan['epic']
    codes = {t['code'] for t in plan['tasks']}
    for t in plan['tasks']:
        out_of_backlog = bool(t['ready_reason']) and autonomy == 'autonomous'
        if t['agent_can_do'] and autonomy == 'autonomous' and not out_of_backlog:
            t['_seal_note'] = f'Um agente pode fazer: {t["agent_can_do"]}.'
        x = {'source_key': f'{prefix}-{t["code"]}', 'source_id': f'{g["ref"]}/{t["code"]}', 'title': t['title'],
             'priority': t['priority'], 'origin': origin, 'description': description(t, g)[:DESCRIPTION_MAX]}
        if project: x['project'] = project
        if t['depends_on']:
            x['blocked_by'] = [f'{prefix}-{d}' if d in codes or not PID_RE.match(d) else d.upper() for d in t['depends_on']]
        if t['agent_can_do'] and not t.get('_seal_note'): x['agent_can_do'] = {'value': True, 'reason': t['agent_can_do']}
        elif t['agent_can_do'] is False: x['agent_can_do'] = {'value': False, 'reason': None}
        if t['estimate_h'] is not None: x['estimate_h'] = t['estimate_h']
        if epic_ref: x['epic'] = epic_ref
        if out_of_backlog:
            x['state'] = 'todo'; x['ready_reason'] = t['ready_reason']
            if plan['column']: x['project_state'] = plan['column']
        body.append(x)
        where[t['code']] = 'todo' if out_of_backlog else None
    return body, where


def _code_of(agent, sk):
    return sk[len(agent) + 1:] if sk.startswith(agent + ':') else sk


def _pid_line(r):
    return r.get('pid') or '?'


def run(agent, plan_raw, dry=False, max_tasks=None, update=False, now=None):
    now = now or datetime.now(timezone.utc)
    pc = P.configure_agent(agent)
    if not pc: raise PlanError(f'{agent}: a instância não tem o bloco "planou" no config')
    if P._S['confidentiality'] == 'minimum':
        raise PlanError('"confidentiality": "minimum" nesta instância: o plano não sobe (descrição e critério de pronto '
                        'são o trabalho). Use "title" ou "detail" no bloco "planou" da instância de produto')
    if not dry and not P.active():
        raise PlanError('Planou desligado para esta instância (modo teste, sem chave ou sem "live": true): use --dry para conferir')
    known = P._load('state.json', {}).get('tasks') or {}
    pre = None
    try:
        g0 = plan_raw.get('goal')
        pre = f'meta-{slug(g0.get("ref") if isinstance(g0, dict) else g0)}-'
    except (PlanError, AttributeError): pass
    earlier = {c[len(pre):] for c in known if pre and c.startswith(pre)}
    plan = validate(plan_raw, earlier, max_tasks or instance_max(agent))
    project = plan['project'] or P._S.get('project')
    if not project: raise PlanError('sem projeto: "project" no plano ou planou.project na instância')
    autonomy = P.autonomy(project, now) if P.active() else None
    lines = [f'AVISO: {w}' for w in plan['warnings']]
    if plan['column']:
        col, e = _column(project, plan['column']) if P.active() else (None, None)
        if e: raise PlanError(e)
        if autonomy != 'autonomous':
            lines.append(f'AVISO: "column" só vale em projeto autônomo; {project} é {autonomy or "desconhecido (vale semi_autonomous)"}: tudo nasce no Backlog')
        elif col is not None:
            owner = (col.get('owner') or {}).get('name')
            lines.append(f'COLUNA {col.get("name")}: ' + (f'dono {owner}, as tarefas com ready_reason vão para a fila dele'
                                                           if owner else 'sem dono, as tarefas com ready_reason ficam com a pessoa'))
    for t in plan['tasks']:
        if t['ready_reason'] and autonomy != 'autonomous':
            lines.append(f'AVISO: tarefa {t["code"]}: fica no Backlog, "ready_reason" só tira do Backlog em projeto autônomo '
                         f'({project}: {autonomy or "desconhecido"})')
    body, where = build(plan, agent, autonomy, now)
    g = plan['goal']
    prefix = f'{agent}:meta-{g["slug"]}-'
    skip = {}
    for x in body:
        code = _code_of(agent, x['source_key'])
        e = known.get(code)
        if e and e.get('pid'):
            if not update: skip[x['source_key']] = e; continue
            for k in ('state', 'ready_reason', 'project_state', 'origin'): x.pop(k, None)
            x['base_version'] = e.get('version')
    send = [x for x in body if x['source_key'] not in skip]
    if dry:
        for x in body:
            dest = 'épico' if x.get('kind') == 'epic' else STATE_LABEL[x.get('state')] + (f' / {x["project_state"]}' if x.get('project_state') else '')
            tag = 'JA EXISTE ' + skip[x['source_key']]['pid'] if x['source_key'] in skip else 'SIMULADA'
            lines.append(f'  {tag} [P{x["priority"]}] {x["title"]} ({dest})' + _extras(x, {}, prefix))
        lines.append(f'RESUMO (simulado): meta {g["ref"]}: {len([x for x in body if x.get("kind") != "epic"])} tarefas em {project}; nada foi enviado')
        return 0, lines
    results = {}
    if send:
        try:
            _, res = P._call('POST', '/agent/sync', {'agent': agent, 'generated_at': now.isoformat(), 'tasks': send})
        except P.PlanouError as e:
            lines.append(f'FALHOU: o Planou não recebeu o plano ({e.message if e.status else e}); nada foi criado, rode de novo')
            return 1, lines
        results = {r.get('source_key'): r for r in (res or {}).get('tasks') or []}
        _record(agent, plan, send, results, now)
    pid_of = {sk: e.get('pid') for sk, e in skip.items()}
    pid_of.update({sk: r.get('pid') for sk, r in results.items() if r.get('pid')})
    rc, made, counts = 0, [], {}
    for x in body:
        sk = x['source_key']
        epic = x.get('kind') == 'epic'
        name = f'[P{x["priority"]}] {x["title"]}'
        if sk in skip:
            lines.append(f'  JA EXISTE {skip[sk]["pid"]} {name} (não reenviada; --update reenvia)')
            if not epic: made.append(skip[sk]['pid']); counts['já existia'] = counts.get('já existia', 0) + 1
            continue
        r = results.get(sk) or {'result': 'error', 'reason': 'sem resposta do Planou para esta tarefa'}
        notes = list(r.get('warnings') or []) + ([r['reason']] if r.get('reason') and r.get('result') != 'error' else [])
        res = r.get('result')
        if res in ('created', 'updated', 'unchanged') and r.get('pid'):
            dest = 'épico' if epic else ('Backlog' if 'state' not in x or P._in_backlog(notes) else STATE_LABEL['todo'])
            if x.get('project_state') and dest != 'Backlog': dest += f' / {x["project_state"]}'
            word = {'created': 'CRIADA', 'updated': 'ATUALIZADA', 'unchanged': 'SEM MUDANCA'}[res]
            lines.append(f'  {word} {r["pid"]} {name} ({dest})' + _extras(x, pid_of, prefix)
                         + (f'; com {(r.get("assignee") or {}).get("name")}' if (r.get('assignee') or {}).get('kind') == 'agent' else ''))
            if not epic: made.append(r['pid']); counts[dest.split(' / ')[0]] = counts.get(dest.split(' / ')[0], 0) + 1
        else:
            rc = 1
            lines.append(f'  RECUSADA {name}: {res} ({r.get("reason") or "sem motivo"})')
        for n in notes: lines.append(f'  AVISO {_pid_line(r)}: {n}')
        if notes: P.note_warnings(_code_of(agent, sk), notes, now)
    where_txt = ', '.join(f'{n} {"no" if k == "Backlog" else "em" if k == "A fazer" else ""} {k}'.replace('  ', ' ')
                          for k, n in counts.items())
    lines.append(f'RESUMO: meta {g["ref"]} quebrada em {len(made)} tarefas em {project} ({where_txt or "nenhuma criada"}): '
                 f'{", ".join(made) or "-"}.' + (' Aprovar = mover para A fazer ou passar para o agente.' if counts.get('Backlog') else ''))
    return rc, lines


def _extras(x, pid_of, prefix):
    out = []
    if x.get('blocked_by'):
        out.append('depende de ' + ', '.join(pid_of.get(b) or (b[len(prefix):] if b.startswith(prefix) else b) for b in x['blocked_by']))
    if (x.get('agent_can_do') or {}).get('value'): out.append('um agente pode fazer')
    return ('; ' + '; '.join(out)) if out else ''


def _record(agent, plan, sent, results, now):
    """state.json and sent.json, like the full sync (so pronta, fila refinar and the events know these tasks), and the
    plan in product_plans.json."""
    with P._locked('state.json'):
        st = P._load('state.json', {})
        tasks = st.setdefault('tasks', {})
        kept = P._load('sent.json', {})
        for x in sent:
            r = results.get(x['source_key']) or {}
            if r.get('result') not in ('created', 'updated', 'unchanged') or not r.get('pid'): continue
            code = _code_of(agent, x['source_key'])
            prev = tasks.get(code) or {}
            notes = list(r.get('warnings') or []) + ([r['reason']] if r.get('reason') else [])
            entry = {**prev, 'pid': r['pid'], 'version': r.get('version'), 'title': x['title'], 'due': None,
                     'state': x.get('state') or prev.get('state') or 'backlog',
                     'project_state': x.get('project_state') or prev.get('project_state')}
            if (not prev and 'state' not in x) or P._in_backlog(notes): entry['backlog'] = True
            elif 'state' in x and 'state' not in (r.get('ignored_fields') or []): entry.pop('backlog', None)
            tasks[code] = entry
            kept[code] = {k: v for k, v in {**(kept.get(code) or {}), **x}.items() if k not in ('description', 'base_version', 'assignee')}
        P._save('state.json', st)
        P._save('sent.json', kept)
    g = plan['goal']
    plans = P._load(PLANS, {})
    p = plans.setdefault(g['slug'], {'ref': g['ref'], 'created_at': now.isoformat(), 'tasks': {}})
    p.update(title=g['title'] or p.get('title'), project=plan['project'] or P._S.get('project'), updated_at=now.isoformat())
    for x in sent:
        r = results.get(x['source_key']) or {}
        if not r.get('pid'): continue
        code = 'epic' if x.get('kind') == 'epic' else x['source_key'].split(f'meta-{g["slug"]}-', 1)[-1]
        p['tasks'][code] = {'pid': r['pid'], 'title': x['title'], 'priority': x['priority'],
                            'state': 'epic' if x.get('kind') == 'epic' else x.get('state') or 'backlog'}
    P._save(PLANS, plans)


def list_plans(agent, goal=None):
    P.configure_agent(agent)
    plans = P._load(PLANS, {})
    key = slug(goal) if goal else None
    lines = []
    for s, p in sorted(plans.items(), key=lambda kv: kv[1].get('updated_at') or ''):
        if key and s != key: continue
        lines.append(f'META {p.get("ref")}: {p.get("title") or "-"} ({p.get("project") or "-"}, {str(p.get("updated_at") or "")[:16]})')
        for code, t in (p.get('tasks') or {}).items():
            lines.append(f'  {t.get("pid")} [P{t.get("priority")}] {t.get("title")} ({t.get("state")})')
    return lines or ['nenhuma meta quebrada por esta instância' + (f' com {goal}' if goal else '')]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='backlog.py', description='A goal becomes a refined backlog in Planou (behavior product).')
    ap.add_argument('instance')
    ap.add_argument('plan', nargs='?', help="the plan JSON file, or '-' for stdin")
    ap.add_argument('--dry', action='store_true', help='check the plan and show where each task would go; nothing is sent')
    ap.add_argument('--max', type=int, help='tasks allowed for this goal (after the person\'s OK); default behavior_config.product.max_tasks or 8')
    ap.add_argument('--update', action='store_true', help='send again the tasks already created (without state)')
    ap.add_argument('--list', nargs='?', const='', metavar='GOAL', help='the goals already broken down (or one of them)')
    a = ap.parse_args(argv)
    if a.list is not None:
        print('\n'.join(list_plans(a.instance, a.list or None)))
        return 0
    if not a.plan: ap.error('diga o arquivo do plano (ou - para ler do stdin)')
    try:
        raw = json.load(sys.stdin) if a.plan == '-' else json.load(open(os.path.expanduser(a.plan), encoding='utf-8'))
    except (OSError, ValueError) as e:
        print(f'ERRO: plano ilegível: {e}', file=sys.stderr)
        return 2
    try:
        rc, lines = run(a.instance, raw, dry=a.dry, max_tasks=a.max, update=a.update)
    except PlanError as e:
        print('ERRO: plano recusado, nada foi enviado:\n' + '\n'.join(f'  {x}' for x in str(e).splitlines()), file=sys.stderr)
        return 2
    print('\n'.join(lines))
    return rc


if __name__ == '__main__':
    sys.exit(main())
