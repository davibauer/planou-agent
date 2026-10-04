#!/usr/bin/env python3
"""Weekly process report of a project (PLN0176, behavior `flow-metrics`): lead time per state, the week's bottleneck
with real cases, the other flow numbers and at most 3 proposals, each with the metric it should move.

  python3 process_report.py <instance> [--project KEY] [--days 7 | --from AAAA-MM-DD --to AAAA-MM-DD]
                            [--from-file flow.json] [--deploys-log PATH] [--json]
                            [--plan-out plan.json] [--record] [--followup]
  python3 process_report.py <instance> --propose plan.json

Data: Planou's GET /v1/agent/projects/{key}/flow (the instance's key; a read, so it works in test mode too) or, with
`--from-file`, a JSON of the same shape (an older Planou without the route, a dry run, the tests). Optional: the deploys
log of the batch release (`--deploys-log`, default `behavior_config.flow-metrics.deploys_log`): batch size per deploy
(rollback and health alert lines of the deploy script are counted apart, never as deploys).

What it measures, for the tasks closed as done in the range (the deliveries):
- lead time from creation to done (in a batch-release project a task closes at the deploy, so this is creation to
  deploy; the report says how many closed within 15 min of a deploy) and from the first exit of the backlog to done;
- lead time per state: the hours each delivery spent in Backlog, Fila (A fazer), Com o agente, Com a pessoa,
  Revisão e release (in_review: the PR waits for review and for the batch), Precisa de você (Aguardando, Em
  refinamento) and Impedida; the median per delivery and the share of all delivery hours;
- throughput per day; rework (a step back from review, or a reopen); blocked episodes and how many went straight to
  done (the task waited for a merge or a release, not for the person); `Decidir:` tasks apart (a person's decision,
  not a delivery: only their wait); integrator wait (the last in_review until done); cost (API price of
  the turns) and worker deliveries (steps, duration, partial ones) per delivery; an autonomy proxy (deliveries without
  a person's move after leaving the backlog; the real index is PLN0107).
- not measured here (no source yet): CI reruns and empty wake-ups of the runner; the report says so.

Bottleneck: the state with the most delivery hours; its cases are the 3 deliveries with the most hours in it. With
backfill (a delivery with any rebuilt transition), only the deliveries with real history count, and the report says so
(all of them only when every delivery has backfill). Proposal titles stay within 80 characters, `Decidir: ` included.
Proposals: at most 3 (`--skip STATE` leaves out an entry, e.g. a change already made this week, to be measured
instead), from a fixed catalog keyed by the heaviest states and by rework and partial deliveries; each has
`kind` (`param`: a low-risk parameter with the value to apply and the one to go back to; `task`: a backlog task;
`role`: a rule or role change, which goes to the person), the metric and the direction it should move.
- `--plan-out F` writes a `scripts/backlog.py` plan: the `task` proposals under "tasks" (with the agent seal) and the
  `param` and `role` ones under "proposals", for `--propose`;
- `--propose F` (Planou 0.53.0, only with `"live": true`; test mode prints what it would send) sends each entry of
  "proposals" with POST /v1/agent/proposals: the person approves it with one click in Precisa de você and Planou applies
  it, with a record and Desfazer. A `param` needs an absolute `value` (the release rules come with it; the dev's "Ao
  mesmo tempo" has no route to read, so the coordinator writes today's value + 1) and the agent
  (`behavior_config.flow-metrics.dev_agent` or "agent" in the plan); a `role` proposes the target agent's whole
  instructions.md (the file on this machine plus the rule, with its sha256). A proposal Planou refuses (422 validation,
  404, 403, a Papel 409 such as `not_reported`) or that cannot be built here goes back to the old way: a `Decidir:` task
  in the plan's "tasks", with an AVISO; `422 unchanged` (already the value) is only said. The plan is rewritten with
  what was sent ("sent": proposal_id, ask_code, status, before, after) and the record gets the proposal id, so a rerun
  never sends twice (Planou's idempotency key also returns the same proposal);
- `--record` keeps the proposals with the metric's baseline in data/coach_proposals.json (not in test mode);
- `--followup` compares each recorded proposal older than 6 days with this week's value and marks it `funcionou` or
  `nao funcionou` (moved less than 10% in the expected direction); with `--record` the marks are saved. A proposal sent
  to Planou is read first (GET /v1/agent/proposals/{id}): only an applied one is measured; rejected, withdrawn, failed
  or reverted ones are marked as such, and one still open waits.

Output: the report in pt-BR (or the analysis as JSON with `--json`). Exit 0 ok, 1 no data (Planou off or refused; with
`--propose`, Planou unreachable), 2 bad arguments or file.
"""
import argparse, hashlib, json, os, re, statistics, sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BEHAVIOR = 'flow-metrics'          # old name: process-coach
RECORD = 'coach_proposals.json'
FOLLOWUP_AFTER = timedelta(days=6)
EFFECT_MIN = 0.10          # a proposal "worked" when its metric moved at least 10% the right way
DEPLOY_CLOSE = timedelta(minutes=15)
CASES = 3
TITLE_MAX = 80          # a proposal's title, with the `Decidir: ` of a role change (backlog.py's ideal)
DECISION_PREFIX = 'Decidir: '
MAX_PROPOSALS = 3

STAGES = [
    ('backlog', 'Backlog'),
    ('todo', 'Fila (A fazer)'),
    ('agent', 'Com o agente'),
    ('person_work', 'Com a pessoa, fazendo'),
    ('in_review', 'Revisão e release'),
    ('waiting', 'Precisa de você (Aguardando, Em refinamento)'),
    ('blocked', 'Impedida'),
]
STAGE_NAME = dict(STAGES)
STOP = ('blocked', 'waiting')
DECIDE = re.compile(r'^\s*Decidir\s*:', re.I)   # a person's decision, not a delivery of the process


def parse_ts(v):
    if not v: return None
    s = str(v).replace(' ', 'T', 1) if 'T' not in str(v) else str(v)
    if s.endswith('Z'): s = s[:-1] + '+00:00'
    if re.search(r'[+-]\d{2}$', s): s += ':00'      # "+00" (psql) -> "+00:00"
    s = re.sub(r'\.(\d+)', lambda m: '.' + (m[1] + '000000')[:6], s, count=1)   # python 3.10 wants 6 digits
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hours(a, b):
    return max(0.0, (b - a).total_seconds() / 3600)


def stage_of(x):
    s = x.get('to_stage')
    if s == 'in_progress': return 'person_work' if x.get('assignee_kind') == 'person' else 'agent'
    return s


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def p85(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs: return None
    k = max(0, -(-85 * len(xs) // 100) - 1)
    return round(xs[k], 1)


def fmt_h(h):
    if h is None: return 's/d'
    return f'{h:.1f} h' if h < 48 else f'{h / 24:.1f} d'


# ---------------------------------------------------------------- analysis

def zone_of(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or 'America/Sao_Paulo')
    except Exception:
        return timezone(timedelta(hours=-3))


def range_of(data, zone):
    f = date.fromisoformat(str(data['from'])[:10])
    t = date.fromisoformat(str(data['to'])[:10])
    start = datetime(f.year, f.month, f.day, tzinfo=zone).astimezone(timezone.utc)
    end = datetime(t.year, t.month, t.day, tzinfo=zone).astimezone(timezone.utc) + timedelta(days=1)
    return f, t, start, end


def intervals(task, until):
    """[(stage, start, end, transition, next_transition)] of the task's whole history, the open one until `until`."""
    xs = task.get('transitions') or []
    out = []
    for i, x in enumerate(xs):
        a = parse_ts(x.get('at'))
        nxt = xs[i + 1] if i + 1 < len(xs) else None
        b = parse_ts(nxt.get('at')) if nxt else until
        if a is None or b is None or x.get('to_stage') == 'closed': continue
        out.append((stage_of(x), a, b, x, nxt))
    return out


def done_close(task, start, end):
    """The time of the last close as done, when it falls in the range; else None."""
    if task.get('state') != 'closed' or task.get('resolution') not in (None, 'done'): return None
    closes = [parse_ts(x.get('at')) for x in task.get('transitions') or [] if x.get('to_stage') == 'closed']
    c = closes[-1] if closes else parse_ts(task.get('completed_at'))
    return c if c and start <= c < end else None


DEPLOY_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})(?::(\d{2}))? ([+-]\d{2})(\d{2})$')
LOG_VERSION_RE = re.compile(r'\bv\d+\.\d+\.\d+\b')


def deploy_line_kind(field):
    """'alert', 'rollback' or 'deploy' from the third field of a deploys log line. The deploy script (PLN0108) writes
    "alerta de saúde" there when the new version stayed live with the health check failing (exit 7), and
    "rollback para vX de vY (...)" when it went back (exit 6, or a rollback by hand)."""
    f = field.strip().lower()
    if f.startswith('alerta'): return 'alert'
    if f.startswith('rollback'): return 'rollback'
    return 'deploy'


def parse_deploys_log(path, start, end):
    """[{at, version, branches, kind}] from the batch release log (tab separated: date, commit, version (lote: a + b),
    ...). kind is 'deploy', 'rollback' (version = the one that went back live) or 'alert' (health alert, version = the
    one that stayed live); only 'deploy' lines are releases (batch size)."""
    out = []
    try:
        with open(os.path.expanduser(path), encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for ln in lines:
        parts = ln.split('\t')
        if len(parts) < 3: continue
        m = DEPLOY_TS_RE.match(parts[0].strip())
        if not m: continue
        at = datetime.fromisoformat(f'{m[1]}T{m[2]}:{m[3]}:{m[4] or "00"}{m[5]}:{m[6]}')
        if not (start <= at < end): continue
        kind = deploy_line_kind(parts[2])
        where = ' '.join(parts[3:]) if kind == 'alert' else parts[2]
        v = LOG_VERSION_RE.search(where) if kind != 'deploy' else re.match(r'^(v\d+\.\d+\.\d+)', parts[2].strip())
        lote = re.search(r'\(lote: ([^)]*)\)', parts[2]) if kind == 'deploy' else None
        branches = [b.strip() for b in lote[1].split('+')] if lote else []
        version = v[0] if v else (None if kind != 'deploy' else parts[2].strip()[:20])
        out.append({'at': at.isoformat(), 'version': version, 'branches': len(branches) or None, 'kind': kind})
    return out


def stage_table(rows):
    """Hours of the deliveries per state: total, share of all their hours, how many, median and p85."""
    total_by_stage = {k: round(sum(r['stages'][k] for r in rows), 1) for k, _ in STAGES}
    all_h = sum(total_by_stage.values()) or 0.0
    out = []
    for k, name in STAGES:
        vals = [r['stages'][k] for r in rows if r['stages'][k] > 0]
        out.append({'key': k, 'name': name, 'total_h': total_by_stage[k], 'share': round(total_by_stage[k] / all_h, 3) if all_h else 0.0,
                    'tasks': len(vals), 'median_h': med(vals), 'p85_h': p85(vals)})
    return out


def _heaviest(stage_rows):
    heavy = sorted((s for s in stage_rows if s['total_h'] > 0), key=lambda s: -s['total_h'])
    return heavy[0] if heavy else None


def bottleneck_of(rows, stage_rows):
    """The state with the most delivery hours and its 3 heaviest cases. With backfill (a delivery with any rebuilt
    transition), only the deliveries with real history count: the rebuilt ones put hours where the backfill guessed
    (PLN0198: Fila with them, Backlog with 47% without). `basis` says which: 'all' (no backfill), 'real' or
    'all_backfill_only' (every delivery has backfill, so all of them count)."""
    real = [r for r in rows if not r['backfill']]
    basis = 'all' if len(real) == len(rows) else 'real' if real else 'all_backfill_only'
    use = real if basis == 'real' else rows
    table = stage_table(use) if basis == 'real' else stage_rows
    b = _heaviest(table)
    if not b: return None
    cases = sorted(use, key=lambda r: -r['stages'][b['key']])[:CASES]
    out = dict(b, basis=basis, real_deliveries=len(real), backfill_deliveries=len(rows) - len(real),
               cases=[{'pid': r['pid'], 'title': r['title'][:TITLE_MAX], 'hours': round(r['stages'][b['key']], 1),
                       'lead_h': round(r['lead_created'], 1) if r['lead_created'] is not None else None} for r in cases])
    if basis == 'real':
        allb = _heaviest(stage_rows)
        out['with_backfill'] = {'key': allb['key'], 'name': allb['name'], 'share': allb['share']} if allb else None
    return out


def analyze(data, now=None, deploys=None):
    zone = zone_of(data.get('time_zone'))
    f, t, start, end = range_of(data, zone)
    now = now or parse_ts(data.get('generated_at')) or datetime.now(timezone.utc)
    until = min(end, now)
    tasks = data.get('tasks') or []

    first_at = min((parse_ts(x.get('at')) for tk in tasks for x in tk.get('transitions') or [] if x.get('at')), default=None)
    covered_h = hours(max(start, first_at), until) if first_at else 0.0
    backfill = sum(1 for tk in tasks for x in tk.get('transitions') or [] if x.get('source') == 'backfill')

    # deliveries: done in the range, with their whole history
    rows, decisions = [], []
    for tk in tasks:
        close = done_close(tk, start, end)
        if not close: continue
        if DECIDE.match(tk.get('title') or ''):
            created = parse_ts(tk.get('created_at'))
            decisions.append(hours(created, close) if created else None)
            continue
        per = {k: 0.0 for k, _ in STAGES}
        for st, a, b, _, _ in intervals(tk, close):
            if st in per: per[st] += hours(a, min(b, close))
        created = parse_ts(tk.get('created_at'))
        started = next((parse_ts(x['at']) for x in tk.get('transitions') or [] if x.get('to_stage') not in ('backlog', 'closed')), None)
        xs = tk.get('transitions') or []
        left_backlog = next((i for i, x in enumerate(xs) if x.get('to_stage') not in ('backlog', 'closed')), None)
        person_after = left_backlog is not None and any(
            x.get('by_kind') == 'person' for x in xs[left_backlog + 1:] if x.get('to_stage') != 'closed')
        rework = sum(1 for i, x in enumerate(xs) if i and (
            (x.get('from_stage') == 'in_review' and x.get('to_stage') in ('in_progress', 'todo'))
            or (x.get('from_stage') == 'closed' and x.get('to_stage') != 'closed')))
        last_review = None
        for st, a, b, _, _ in intervals(tk, close):
            if st == 'in_review': last_review = hours(a, min(b, close))
        dl = tk.get('deliveries') or []
        rows.append({
            'pid': tk.get('pid'), 'title': tk.get('title') or '', 'close': close, 'stages': per,
            'lead_created': hours(created, close) if created else None,
            'lead_started': hours(started, close) if started and started <= close else None,
            'person_touch': person_after, 'rework': rework, 'integrator_wait': last_review,
            'cost': float(tk.get('cost_usd') or 0), 'deliveries': dl,
            'backfill': any(x.get('source') == 'backfill' for x in xs),
        })

    n = len(rows)
    stage_rows = stage_table(rows)

    # time in the range per state, every task (clipped), like the Métricas screen
    range_by_stage = {k: 0.0 for k, _ in STAGES}
    episodes = []
    for tk in tasks:
        for st, a, b, x, nxt in intervals(tk, until):
            a2, b2 = max(a, start), min(b, until)
            if b2 > a2 and st in range_by_stage: range_by_stage[st] += hours(a2, b2)
            if st in STOP and a >= start:
                episodes.append({'pid': tk.get('pid'), 'stage': st, 'state': x.get('to_state_name') or STAGE_NAME[st],
                                 'hours': round(hours(a, min(b, until)), 1), 'open': nxt is None,
                                 'ended_in': (nxt or {}).get('to_stage'), 'reason': x.get('reason'),
                                 'decision': bool(DECIDE.match(tk.get('title') or ''))})
    closed_eps = [e for e in episodes if not e['open'] and e['stage'] == 'blocked']
    # a blocked task that goes straight to done waited for a merge or a release, not for the person
    to_done = [e for e in closed_eps if e['ended_in'] == 'closed']

    per_day = {}
    for r in rows:
        d = r['close'].astimezone(zone).date().isoformat()
        per_day[d] = per_day.get(d, 0) + 1

    deploy_times = [parse_ts(d.get('deployed_at')) for d in data.get('deploys') or [] if d.get('kind', 'deploy') == 'deploy']
    deploy_times = [d for d in deploy_times if d]
    at_deploy = sum(1 for r in rows if any(timedelta(0) <= r['close'] - d <= DEPLOY_CLOSE for d in deploy_times))

    dls = [d for r in rows for d in r['deliveries']]
    dev = [d for d in dls if d.get('role') == 'dev']
    cost_total = round(sum(r['cost'] for r in rows), 2)

    metrics = {
        'deliveries': n,
        'lead_created_median_h': med([r['lead_created'] for r in rows]),
        'lead_created_p85_h': p85([r['lead_created'] for r in rows]),
        'lead_started_median_h': med([r['lead_started'] for r in rows]),
        'lead_started_p85_h': p85([r['lead_started'] for r in rows]),
        'throughput_per_day': round(n / max(1.0, covered_h / 24), 1) if covered_h else None,
        'rework_rate': round(sum(1 for r in rows if r['rework']) / n, 3) if n else None,
        'blocked_h': round(range_by_stage['blocked'], 1),
        'waiting_h': round(range_by_stage['waiting'], 1),
        'blocked_to_done_share': round(len(to_done) / len(closed_eps), 3) if closed_eps else None,
        'decision_wait_median_h': med(decisions),
        'integrator_wait_median_h': med([r['integrator_wait'] for r in rows]),
        'autonomy_proxy': round(sum(1 for r in rows if not r['person_touch']) / n, 3) if n else None,
        'cost_per_delivery_usd': round(cost_total / n, 2) if n else None,
        'partial_share': round(sum(1 for d in dev if d.get('result') == 'partial') / len(dev), 3) if dev else None,
        'worker_steps_avg': round(statistics.mean(d.get('steps') or 0 for d in dev)) if dev else None,
        'worker_minutes_avg': round(statistics.mean((d.get('duration_ms') or 0) for d in dev) / 60000, 1) if dev else None,
    }
    for s in stage_rows: metrics[f'stage_{s["key"]}_median_h'] = s['median_h']

    bottleneck = bottleneck_of(rows, stage_rows)

    open_now = {}
    for tk in tasks:
        if tk.get('state') == 'closed' or not tk.get('transitions'): continue
        st = stage_of(tk['transitions'][-1])
        open_now[st] = open_now.get(st, 0) + 1
    stuck = sorted((e for e in episodes if e['open']), key=lambda e: -e['hours'])[:5]

    batches = [d['branches'] for d in deploys or [] if d.get('kind', 'deploy') == 'deploy' and d.get('branches')]
    log_kinds = [d.get('kind', 'deploy') for d in deploys or []]
    return {
        'project': data.get('project'), 'from': f.isoformat(), 'to': t.isoformat(), 'time_zone': data.get('time_zone'),
        'generated_at': now.isoformat(), 'covered_h': round(covered_h, 1), 'first_data_at': first_at.isoformat() if first_at else None,
        'approximate_until': data.get('approximate_until'), 'backfill_transitions': backfill,
        'backfill_deliveries': sum(1 for r in rows if r['backfill']),
        'stages': stage_rows, 'range_stages': {k: round(v, 1) for k, v in range_by_stage.items()},
        'bottleneck': bottleneck, 'metrics': metrics, 'per_day': per_day,
        'closed_at_deploy': at_deploy, 'deploys': len(deploy_times),
        'batch_avg': round(statistics.mean(batches), 1) if batches else None,
        'log_rollbacks': log_kinds.count('rollback'), 'log_health_alerts': log_kinds.count('alert'),
        'stops': {'episodes': len(episodes), 'blocked_closed': len(closed_eps), 'blocked_to_done': len(to_done),
                  'stuck_now': stuck, 'blocked_now': sum(1 for e in episodes if e['open'] and e['stage'] == 'blocked')},
        'decisions': len(decisions),
        'open_now': open_now, 'cost_total_usd': cost_total,
        'not_measured': ['reruns de CI (sem fonte no Planou)', 'acordadas vazias do runner (sem registro; PLN0155)'],
    }


# ---------------------------------------------------------------- proposals

CATALOG = {
    'in_review': {
        'kind': 'param', 'code': 'lote-mais-curto',
        'title': 'Release em lote mais curto: 1 branch pronta vai depois de 15 min',
        'what': 'Na seção Release do projeto, "esperar no máximo" de 30 para 15 min (a regra de 2 branches fica). '
                'Volta: 30 min.',
        'why': 'A maior parte do lead time das entregas é a PR pronta esperando a revisão e o lote.',
        'param': {'name': 'release.max_wait_min', 'to': 15, 'back': 30},
        'proposal': {'param': 'release_max_wait_min', 'value': 15},
        'metric': 'stage_in_review_median_h', 'direction': 'down'},
    'todo': {
        'kind': 'param', 'code': 'wip-dev',
        'title': 'Mais uma vaga "Ao mesmo tempo" para o dev enquanto a fila espera',
        'what': 'Aba Fila do dev: "Ao mesmo tempo" +1 (até 8), por uma semana. Volta: o valor de hoje.',
        'why': 'As tarefas prontas passam mais tempo na fila (A fazer) do que em qualquer outro estado.',
        'param': {'name': 'queue.wip', 'to': '+1', 'back': 'atual'},
        'proposal': {'param': 'wip', 'delta': 1},
        'metric': 'stage_todo_median_h', 'direction': 'down'},
    'agent': {
        'kind': 'task', 'code': 'tarefas-menores',
        'title': 'Quebrar em duas a tarefa que passa de 4 h com o agente',
        'what': 'O agente de produto e o dev quebram, antes de começar, o que estimam em mais de 4 h; o coordenador '
                'lista as que passaram disso na semana.',
        'why': 'Com o agente é o maior pedaço do lead time: tarefa grande prende a vaga e volta mais.',
        'metric': 'stage_agent_median_h', 'direction': 'down'},
    'blocked': {
        'kind': 'role', 'code': 'impedida-so-com-pessoa',
        'title': 'Impedida só quando precisa da pessoa, não para esperar release',
        'what': 'Regra do papel do dev: antes de `fila blocked`, conferir se o que falta é de outro agente (dependência, '
                'release, CI) e esperar sem pedir; só vira pedido o que é decisão, conta ou acesso da pessoa.',
        'why': 'Impedida pesa no lead time e boa parte das impedidas vai direto para concluída: esperava merge ou '
               'release, não a pessoa.',
        'metric': 'blocked_to_done_share', 'direction': 'down'},
    'waiting': {
        'kind': 'role', 'code': 'aguardando-com-recomendacao',
        'title': 'Pedido com recomendação, seguida quando a pessoa demora',
        'what': 'Regra do papel: todo pedido leva a opção recomendada; passado o prazo do pedido, o agente segue pela '
                'recomendada no que é reversível e avisa.',
        'why': 'As decisões da pessoa (Decidir:) e o Precisa de você seguram as tarefas que dependem delas.',
        'metric': 'decision_wait_median_h', 'direction': 'down'},
    'backlog': {
        'kind': 'role', 'code': 'selo-ao-criar',
        'title': 'Selo "o agente pode fazer" já na criação da tarefa',
        'what': 'Regra do papel de quem cria tarefa no projeto (produto, saúde, sugestões): marcar o selo quando o '
                'agente faz sem decisão da pessoa; o coordenador lista as do Backlog sem selo que poderiam ter.',
        'why': 'As entregas passam mais tempo no Backlog do que fazendo: a vaga livre só puxa o que tem selo.',
        'metric': 'stage_backlog_median_h', 'direction': 'down'},
    'person_work': {
        'kind': 'task', 'code': 'passar-para-agente',
        'title': 'Tarefas feitas pela pessoa que um agente poderia fazer',
        'what': 'Listar as entregas da semana feitas pela pessoa e marcar quais um agente faria; as próximas iguais vão '
                'com o selo.',
        'why': 'Com a pessoa, fazendo é o maior pedaço do lead time.',
        'metric': 'autonomy_proxy', 'direction': 'up'},
    'rework': {
        'kind': 'task', 'code': 'retrabalho',
        'title': 'Conferência antes da PR para cortar o retrabalho',
        'what': 'Lista curta do que o revisor mais devolveu na semana, lida pelo dev antes de abrir a PR.',
        'why': 'Mais de 15% das entregas voltaram da revisão ou foram reabertas.',
        'metric': 'rework_rate', 'direction': 'down'},
    'partial': {
        'kind': 'task', 'code': 'entregas-parciais',
        'title': 'Menos entregas parciais do worker',
        'what': 'Ver por que as entregas parciais da semana pararam (contexto, teste, trava) e ajustar o pedido ao worker.',
        'why': 'Mais de 20% das entregas dos workers terminaram parciais.',
        'metric': 'partial_share', 'direction': 'down'},
}


def _fits(key, a):
    """A catalog entry only when its own numbers back it (a stage entry: time in that state; 'agent': long tasks)."""
    st = {s['key']: s for s in a['stages']}
    m = a['metrics']
    if key == 'rework': return (m.get('rework_rate') or 0) > 0.15
    if key == 'partial': return (m.get('partial_share') or 0) > 0.20
    if key == 'blocked': return (st['blocked']['total_h'] > 0) or (m.get('blocked_to_done_share') or 0) > 0.30
    if key == 'agent': return (st['agent']['p85_h'] or 0) > 4
    if key == 'in_review': return max(st['in_review']['median_h'] or 0, m.get('integrator_wait_median_h') or 0) > 1
    if key == 'person_work': return (m.get('autonomy_proxy') or 1) < 0.9
    if key == 'waiting': return st['waiting']['total_h'] > 0 or (m.get('decision_wait_median_h') or 0) > 8
    return key in st and st[key]['total_h'] > 0


def propose(a, skip=()):
    """At most 3: the bottleneck's entry, then the signals (blocked waiting for a release, rework, partial deliveries),
    then the next heaviest states; `skip` leaves entries out (a change already made: it is measured, not proposed)."""
    by_total = [s['key'] for s in sorted(a['stages'], key=lambda s: -s['total_h']) if s['total_h'] > 0]
    first = [a['bottleneck']['key']] if a.get('bottleneck') else by_total[:1]
    order = first + ['blocked', 'waiting', 'rework', 'partial'] + [k for k in by_total if k not in first]
    props, seen = [], set(skip)
    for k in order:
        if len(props) >= MAX_PROPOSALS: break
        if k in seen or k not in CATALOG or not _fits(k, a): continue
        seen.add(k)
        p = dict(CATALOG[k], source=k)
        p['title'] = short_title(p['title'], TITLE_MAX - (len(DECISION_PREFIX) if p['kind'] != 'task' else 0))
        p['baseline'] = a['metrics'].get(p['metric'])
        props.append(p)
    return props


def short_title(title, limit):
    """At most `limit` characters, cut at a word with "..." (a refused proposal becomes a `Decidir:` task)."""
    title = ' '.join(str(title).split())
    if len(title) <= limit: return title
    cut = title[:limit - 3].rsplit(' ', 1)[0].rstrip(' ,;:')
    return cut + '...'


PROPOSAL_END = {'rejected': 'recusada', 'cancelled': 'retirada', 'failed': 'falhou', 'reverted': 'desfeita'}
FINAL = ('funcionou', 'nao funcionou') + tuple(PROPOSAL_END.values())


def followup(a, recorded, now, states=None):
    """`states` ({proposal_id: GET /v1/agent/proposals/{id}}): a proposal sent to Planou is measured only once applied;
    rejected, withdrawn, failed or reverted ones get that mark, an open one waits."""
    out, states = [], states or {}
    for p in recorded:
        if p.get('status') in FINAL: continue
        at = parse_ts(p.get('recorded_at'))
        if not at or now - at < FOLLOWUP_AFTER: continue
        st = states.get(p.get('proposal_id')) if p.get('proposal_id') else None
        if st:
            p['proposal_status'] = st.get('status')
            if st.get('status') == 'open':
                p['note'] = f'proposta {st.get("ask_code")} ainda sem resposta em Precisa de você'
                out.append(dict(p, status='aguardando'))
                continue
            if st.get('status') in PROPOSAL_END:
                p['status'] = PROPOSAL_END[st['status']]
                p['note'] = f'proposta {st.get("ask_code")}' + (f': {st.get("result")}' if st.get('result') else '')
                p['checked_at'] = now.isoformat()
                out.append(p)
                continue
        base, cur = p.get('baseline'), a['metrics'].get(p.get('metric'))
        if base in (None, 0) or cur is None:
            p['status'], p['note'] = 'sem dados', f'base {base}, agora {cur}'
        else:
            change = (cur - base) / abs(base)
            good = change <= -EFFECT_MIN if p.get('direction') == 'down' else change >= EFFECT_MIN
            p['status'] = 'funcionou' if good else 'nao funcionou'
            p['note'] = f'{p["metric"]}: {base} -> {cur} ({change:+.0%})'
        p['checked_at'] = now.isoformat()
        out.append(p)
    return out


def plan_for(a, props, bc=None):
    """The backlog.py plan: `task` proposals under "tasks"; `param` and `role` ones under "proposals", for --propose
    (the agent of the "Ao mesmo tempo" and of the Papel: behavior_config.flow-metrics.dev_agent)."""
    bc = bc or {}
    week = date.fromisoformat(a['to']).isocalendar()
    tasks, proposals = [], []
    for p in props:
        done = [f'{p["metric"]} {"cai" if p["direction"] == "down" else "sobe"} pelo menos 10% na semana '
                f'seguinte (base {p["baseline"]})']
        if p['kind'] == 'task':
            tasks.append({'code': p['code'], 'title': p['title'], 'what': p['what'], 'why': p['why'], 'done_when': done,
                          'priority': 'P3', 'decide': False,
                          'agent_can_do': 'mudança de processo pequena, com métrica e volta definidas'})
            continue
        e = {'code': p['code'], 'change': p['kind'], 'title': p['title'], 'what': p['what'], 'why': p['why'],
             'done_when': done, 'metric': p['metric'], 'direction': p['direction'], 'baseline': p['baseline']}
        pr = p.get('proposal') or {}
        if p['kind'] == 'param':
            e['param'] = pr.get('param')
            if str(pr.get('param', '')).startswith('release_'):
                e.update(project=a['project'], value=pr.get('value'))
            else:
                # no route reads another agent's "Ao mesmo tempo": the coordinator writes today's value + delta here
                e.update(agent=bc.get('dev_agent'), value=pr.get('value'), delta=pr.get('delta'))
        else:
            e.update(agent=bc.get('dev_agent'), doc=pr.get('doc') or 'instructions', rule=p['what'])
        proposals.append(e)
    return {'goal': {'ref': f'coach-{week[0]}-w{week[1]:02d}', 'title': f'Processo {a["project"]}: semana {week[1]}'},
            'project': a['project'], 'week': a['to'], 'tasks': tasks, 'proposals': proposals}


# ---------------------------------------------------------------- proposals in Planou (--propose)

REFUSED = (400, 403, 404, 409, 422)     # Planou will not take it: back to a `Decidir:` task
ROLE_FILES = {'instructions': 'instructions.md'}


class NotProposable(Exception):
    """The proposal cannot be built on this machine (no value, no agent, the file is not here)."""


def proposal_fields(e, plan):
    """The body of POST /v1/agent/proposals for a plan entry (without change, title and idempotency key)."""
    arrow = 'cair' if e.get('direction') == 'down' else 'subir'
    reason = (f'{e.get("why") or ""} {e.get("what") or ""} Métrica: {e.get("metric")} deve {arrow} pelo menos 10% na semana '
              f'seguinte (base {e.get("baseline")}). {(plan.get("goal") or {}).get("title") or ""}').strip()[:4000]
    if e.get('change') == 'param':
        v = e.get('value')
        if isinstance(v, bool) or not isinstance(v, int):
            raise NotProposable('sem o valor absoluto: escreva "value" no plano'
                                + (' (o "Ao mesmo tempo" de hoje + 1)' if e.get('param') == 'wip' else ''))
        f = {'param': e.get('param'), 'value': v, 'reason': reason}
        if e.get('param') == 'wip':
            if not e.get('agent'): raise NotProposable('sem o agente: behavior_config.flow-metrics.dev_agent ou "agent" no plano')
            f['agent'] = e['agent']
        elif e.get('param') == 'priority':
            if not e.get('task'): raise NotProposable('sem a tarefa ("task") da prioridade')
            f['task'] = e['task']
        else:
            f['project'] = e.get('project') or plan.get('project')
        return f
    agent, doc = e.get('agent'), e.get('doc') or 'instructions'
    if not agent: raise NotProposable('sem o agente do papel: behavior_config.flow-metrics.dev_agent ou "agent" no plano')
    if doc not in ROLE_FILES: raise NotProposable(f'papel "{doc}": só instructions sai daqui')
    from watch_core import config as C
    path = os.path.join(C.agent_root(agent), 'config', ROLE_FILES[doc])
    try:
        with open(path, 'rb') as fh: raw = fh.read()
    except OSError:
        raise NotProposable(f'{ROLE_FILES[doc]} do {agent} não está nesta máquina') from None
    text = raw.decode('utf-8', 'replace')
    rule = ' '.join(str(e.get('rule') or e.get('what') or '').split())
    if not rule: raise NotProposable('sem o texto da regra ("rule")')
    if rule in ' '.join(text.split()): raise NotProposable('UNCHANGED')
    content = e.get('content') or f'{text.rstrip()}\n\n## {e.get("title")}\n\n{rule}\n'
    return {'agent': agent, 'doc': doc, 'name': ROLE_FILES[doc], 'content': content,
            'base_sha256': hashlib.sha256(raw).hexdigest(), 'reason': reason, **({'task': e['task']} if e.get('task') else {})}


def decision_task(e, why):
    """A refused proposal goes back to the old way: a `Decidir:` task in the Backlog."""
    return {'code': e['code'], 'title': e['title'],
            'what': f'{e.get("what") or ""} (A proposta pelo Planou não foi aceita: {why}. Quem aplica é a pessoa.)'.strip(),
            'why': e.get('why') or '', 'done_when': e.get('done_when') or [], 'priority': 'P3', 'decide': True,
            'agent_can_do': False}


def send_proposals(instance, root, cfg, plan_path, now=None):
    """--propose: sends the plan's "proposals"; see the module doc. Returns the exit code."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(os.path.expanduser(plan_path), encoding='utf-8') as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as e:
        print(f'ERRO: plano ilegível: {e}', file=sys.stderr)
        return 2
    entries = [e for e in plan.get('proposals') or [] if isinstance(e, dict)]
    todo = [e for e in entries if not e.get('sent') and not e.get('fallback')]
    if not todo:
        print('PROPOSTAS: nada a enviar')
        return 0
    if cfg.get('live') is not True:
        for e in todo:
            print(f'MODO TESTE: proposta {e.get("code")} não enviada ({e.get("change")} '
                  f'{e.get("param") or e.get("doc") or "?"}): {e.get("title")}')
        return 0
    from watch_core import planou as P
    P.configure_agent(instance)
    if not P.active():
        print('ERRO: Planou desligado para esta instância (config ou chave): propostas não enviadas', file=sys.stderr)
        return 1
    tasks = plan.setdefault('tasks', [])
    failed, sent = False, []
    ref_of = (plan.get('goal') or {}).get('ref') or 'coach'
    for e in todo:
        try:
            fields = proposal_fields(e, plan)
            res = P.propose(e.get('change'), e.get('title'), ref=f'{ref_of}-{e.get("code")}', **fields)
        except NotProposable as x:
            if str(x) == 'UNCHANGED':
                e['sent'] = {'status': 'unchanged'}
                print(f'JA ESTA: {e.get("code")}: a regra já está no papel do {e.get("agent")}')
                continue
            e['fallback'] = str(x)
            tasks.append(decision_task(e, str(x)))
            print(f'AVISO: proposta {e.get("code")} não montada ({x}): vai como Decidir: no Backlog')
            continue
        except P.PlanouError as x:
            if x.status == 422 and x.code == 'unchanged':
                e['sent'] = {'status': 'unchanged'}
                print(f'JA ESTA: {e.get("code")}: o Planou já tem esse valor ({x.message})')
            elif x.status in REFUSED:
                why = f'{x.status} {x.code}' + (f': {x.message}' if x.message else '')
                e['fallback'] = why
                tasks.append(decision_task(e, why))
                print(f'AVISO: o Planou não aceitou a proposta {e.get("code")} ({why}): vai como Decidir: no Backlog')
            else:
                failed = True
                print(f'ERRO: proposta {e.get("code")} não enviada ({x.status} {x.code}: {x.message}); rodar de novo', file=sys.stderr)
            continue
        e['sent'] = {k: res.get(k) for k in ('proposal_id', 'ask_code', 'status', 'before', 'after')}
        sent.append(e)
        change = f': {res.get("before")} -> {res.get("after")}' if res.get('before') is not None or res.get('after') is not None else ''
        print(f'PROPOSTA {res.get("ask_code")} {e.get("code")}{change}: {e.get("title")}'
              + (' (já existia)' if res.get('reused') else '') + ' -> Precisa de você, Aprovações')
    from watch_core import fileio
    fileio.write_json(os.path.expanduser(plan_path), plan, ensure_ascii=False, indent=1)
    if sent:
        rec_path = os.path.join(root, 'data', RECORD)
        try:
            with open(rec_path, encoding='utf-8') as fh: recorded = json.load(fh)
        except (OSError, ValueError):
            recorded = []
        week = plan.get('week') or ''
        for e in sent:
            r = next((x for x in recorded if x.get('code') == e['code'] and x.get('week', '') == week), None)
            if r is None:
                r = {'code': e['code'], 'week': week, 'title': e['title'], 'kind': e['change'], 'metric': e.get('metric'),
                     'direction': e.get('direction'), 'baseline': e.get('baseline'), 'recorded_at': now.isoformat(),
                     'status': 'aberta', 'param': e.get('param')}
                recorded.append(r)
            r.update({'proposal_id': e['sent']['proposal_id'], 'ask_code': e['sent']['ask_code'],
                      'before': e['sent']['before'], 'after': e['sent']['after']})
        os.makedirs(os.path.dirname(rec_path), exist_ok=True)
        fileio.write_json(rec_path, recorded, ensure_ascii=False, indent=1)
    if tasks and any(e.get('fallback') for e in todo):
        print(f'PLANO: {len(tasks)} tarefa(s) para o backlog.py (as recusadas viraram Decidir:)')
    return 1 if failed else 0


def proposal_states(instance, recorded):
    """{proposal_id: GET /v1/agent/proposals/{id}} of the recorded proposals still waiting for a verdict; {} when Planou
    does not answer (the followup then measures them as before)."""
    ids = [p['proposal_id'] for p in recorded if p.get('proposal_id') and p.get('status') not in FINAL]
    if not ids: return {}
    from watch_core import planou as P
    try:
        P.configure_agent(instance)
    except Exception:
        return {}
    if not (P._S.get('base_url') and P._key()): return {}
    out = {}
    for i in ids:
        try:
            out[i] = P.proposal_state(i)
        except P.PlanouError:
            continue
    return out


# ---------------------------------------------------------------- output

def render(a, props, follow=()):
    m = a['metrics']
    L = [f'RELATORIO DE PROCESSO {a["project"]}: {a["from"]} a {a["to"]} ({a["time_zone"]})']
    if a['first_data_at'] and a['covered_h'] < 24 * 6:
        L.append(f'AVISO: o histórico começa em {a["first_data_at"][:16]} UTC; o período tem só {a["covered_h"]:.0f} h de dados.')
    if a['backfill_transitions']:
        L.append(f'AVISO: {a["backfill_transitions"]} transições são reconstruídas (backfill, até {str(a["approximate_until"])[:16]}); '
                 f'{a["backfill_deliveries"]} das entregas têm parte do histórico aproximado.')
    L.append('')
    L.append(f'Entregas (concluídas como feitas): {m["deliveries"]}; vazão {m["throughput_per_day"]}/dia; por dia: '
             + (', '.join(f'{d} {c}' for d, c in sorted(a['per_day'].items())) or '-'))
    L.append(f'Lead time criação até concluída: mediana {fmt_h(m["lead_created_median_h"])}, p85 {fmt_h(m["lead_created_p85_h"])}'
             f' ({a["closed_at_deploy"]} de {m["deliveries"]} fecharam até 15 min depois de um dos {a["deploys"]} deploys)')
    L.append(f'Lead time saída do Backlog até concluída: mediana {fmt_h(m["lead_started_median_h"])}, p85 {fmt_h(m["lead_started_p85_h"])}')
    L.append('')
    L.append('Lead time por estado (horas das entregas em cada estado):')
    L.append('  estado | total | parte | entregas | mediana | p85')
    for s in a['stages']:
        if not s['total_h'] and not s['tasks']: continue
        L.append(f'  {s["name"]} | {s["total_h"]:.1f} h | {s["share"]:.0%} | {s["tasks"]} | {fmt_h(s["median_h"])} | {fmt_h(s["p85_h"])}')
    L.append('')
    b = a['bottleneck']
    if b:
        L.append(f'GARGALO: {b["name"]}, {b["share"]:.0%} das horas das entregas ({b["total_h"]:.1f} h em {b["tasks"]} entregas, '
                 f'mediana {fmt_h(b["median_h"])}, p85 {fmt_h(b["p85_h"])}). Casos:')
        if b.get('basis') == 'real':
            w = b.get('with_backfill')
            L.insert(-1, f'Gargalo calculado só com as {b["real_deliveries"]} entregas de histórico real; as {b["backfill_deliveries"]} '
                         f'com histórico reconstruído ficam de fora'
                         + (f' (com elas, sairia {w["name"]}, {w["share"]:.0%}).' if w and w['key'] != b['key'] else '.'))
        elif b.get('basis') == 'all_backfill_only':
            L.insert(-1, 'Gargalo calculado com histórico reconstruído: todas as entregas do período têm backfill.')
        for c in b['cases']:
            L.append(f'  {c["pid"]} {fmt_h(c["hours"])} em {b["name"]} (lead {fmt_h(c["lead_h"])}): {c["title"]}')
    else:
        L.append('GARGALO: sem entregas no período.')
    L.append('')
    st = a['stops']
    L.append('Outros números:')
    L.append(f'  retrabalho: {pct(m["rework_rate"])} das entregas voltaram da revisão ou foram reabertas')
    L.append(f'  impedida {a["range_stages"]["blocked"]:.1f} h e Precisa de você {a["range_stages"]["waiting"]:.1f} h no período (todas as tarefas); '
             f'{st["blocked_to_done"]} de {st["blocked_closed"]} saídas de Impedida foram direto para concluída (esperavam merge ou '
             f'release, não a pessoa); {st["blocked_now"]} impedidas agora')
    L.append(f'  decisões da pessoa (Decidir:) fechadas: {a["decisions"]}, mediana {fmt_h(m["decision_wait_median_h"])} da criação à '
             f'resposta (fora das entregas)')
    L.append(f'  espera do integrador (última revisão e release até concluir): mediana {fmt_h(m["integrator_wait_median_h"])}'
             + (f'; lote médio {a["batch_avg"]} branches por deploy' if a['batch_avg'] else '')
             + (f'; {a["log_rollbacks"]} volta(s) de versão' if a.get('log_rollbacks') else '')
             + (f'; {a["log_health_alerts"]} alerta(s) de saúde depois do deploy' if a.get('log_health_alerts') else ''))
    L.append(f'  custo: {a["cost_total_usd"]:.2f} USD de API nas entregas, {m["cost_per_delivery_usd"]} USD por entrega; '
             f'worker: {m["worker_steps_avg"]} passos e {m["worker_minutes_avg"]} min em média, {pct(m["partial_share"])} parciais')
    L.append(f'  autonomia (substituto até a PLN0107): {pct(m["autonomy_proxy"])} das entregas sem movimento da pessoa depois do Backlog')
    L.append(f'  abertas agora: ' + ', '.join(f'{STAGE_NAME.get(k, k)} {v}' for k, v in sorted(a['open_now'].items(), key=lambda kv: -kv[1])))
    if st['stuck_now']:
        L.append('  paradas agora: ' + '; '.join(f'{e["pid"]} {e["state"]} há {fmt_h(e["hours"])}' for e in st['stuck_now']))
    L.append('  não medido: ' + '; '.join(a['not_measured']))
    if follow:
        L.append('')
        L.append('Efeito das propostas anteriores:')
        for p in follow:
            L.append(f'  {p.get("status", "?").upper()}: {p.get("title")} ({p.get("note")})')
    L.append('')
    L.append(f'PROPOSTAS ({len(props)}):')
    kinds = {'param': 'parâmetro, proposta no Planou com Desfazer', 'task': 'tarefa no backlog',
             'role': 'regra do papel, proposta no Planou com Desfazer'}
    for i, p in enumerate(props, 1):
        L.append(f'  {i}. {p["title"]} [{kinds[p["kind"]]}]')
        L.append(f'     o que: {p["what"]}')
        L.append(f'     métrica: {p["metric"]} deve {"cair" if p["direction"] == "down" else "subir"} (base {p["baseline"]})')
    return '\n'.join(L)


def pct(v):
    return 's/d' if v is None else f'{v:.0%}'


# ---------------------------------------------------------------- instance glue

def instance_config(instance):
    from watch_core import config as C
    root = C.agent_root(instance)
    try:
        with open(os.path.join(root, 'config', 'config.json'), encoding='utf-8') as f:
            return root, json.load(f)
    except (OSError, ValueError):
        return root, {}


def fetch(instance, project, f, t):
    from watch_core import planou as P
    P.configure_agent(instance)
    if not (P._S.get('base_url') and P._key()):
        raise RuntimeError('sem chave ou URL do Planou nesta instância: use --from-file')
    q = f'?from={f.isoformat()}&to={t.isoformat()}'
    try:
        _, data = P._call('GET', f'/agent/projects/{project}/flow{q}')
    except P.PlanouError as e:
        if e.status == 404 and 'rojeto' not in (e.message or ''):
            raise RuntimeError('o Planou ainda não tem a rota /flow (PLN0176): use --from-file') from None
        raise RuntimeError(f'Planou recusou: {e.status} {e.message}') from None
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(prog='process_report.py', description='Weekly process report (behavior flow-metrics).')
    ap.add_argument('instance')
    ap.add_argument('--project')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--from', dest='date_from')
    ap.add_argument('--to', dest='date_to')
    ap.add_argument('--from-file')
    ap.add_argument('--deploys-log')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--plan-out')
    ap.add_argument('--record', action='store_true')
    ap.add_argument('--followup', action='store_true')
    ap.add_argument('--propose', metavar='PLAN', help='send the plan\'s param and role proposals to Planou (live only)')
    ap.add_argument('--skip', action='append', default=[], metavar='STATE',
                    help='a catalog entry not to propose (a change already made this week: measure it instead)')
    a = ap.parse_args(argv)

    root, cfg = instance_config(a.instance)
    from watch_core import behavior_names
    bc = behavior_names.options(cfg, BEHAVIOR)
    if a.propose:
        return send_proposals(a.instance, root, cfg, a.propose)
    project = a.project or bc.get('project') or (cfg.get('planou') or {}).get('project')
    if a.from_file:
        try:
            with open(os.path.expanduser(a.from_file), encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            print(f'ERRO: arquivo ilegível: {e}', file=sys.stderr)
            return 2
    else:
        if not project:
            print('ERRO: diga o projeto (--project ou planou.project da instância)', file=sys.stderr)
            return 2
        try:
            t = date.fromisoformat(a.date_to) if a.date_to else date.today()
            f = date.fromisoformat(a.date_from) if a.date_from else t - timedelta(days=max(1, a.days) - 1)
        except ValueError:
            print('ERRO: datas em AAAA-MM-DD', file=sys.stderr)
            return 2
        try:
            data = fetch(a.instance, project, f, t)
        except RuntimeError as e:
            print(f'ERRO: {e}', file=sys.stderr)
            return 1
    if not isinstance(data, dict) or not isinstance(data.get('tasks'), list) or 'from' not in data:
        print('ERRO: resposta sem tasks/from', file=sys.stderr)
        return 2

    zone = zone_of(data.get('time_zone'))
    _, _, start, end = range_of(data, zone)
    log = a.deploys_log or bc.get('deploys_log')
    deploys = parse_deploys_log(log, start, end) if log else None
    an = analyze(data, deploys=deploys)
    now = parse_ts(an['generated_at'])
    props = propose(an, a.skip)

    rec_path = os.path.join(root, 'data', RECORD)
    try:
        with open(rec_path, encoding='utf-8') as fh:
            recorded = json.load(fh)
    except (OSError, ValueError):
        recorded = []
    follow = followup(an, recorded, now, proposal_states(a.instance, recorded)) if a.followup else []

    if a.plan_out:
        with open(os.path.expanduser(a.plan_out), 'w', encoding='utf-8') as fh:
            json.dump(plan_for(an, props, bc), fh, ensure_ascii=False, indent=1)
    if a.record:
        if cfg.get('live') is not True:
            print('AVISO: modo teste: propostas não gravadas (--record só com "live": true)', file=sys.stderr)
        else:
            known = {p.get('code') + p.get('week', '') for p in recorded}
            week = an['to']
            for p in props:
                if p['code'] + week in known: continue
                recorded.append({'code': p['code'], 'week': week, 'title': p['title'], 'kind': p['kind'], 'metric': p['metric'],
                                 'direction': p['direction'], 'baseline': p['baseline'], 'recorded_at': now.isoformat(),
                                 'status': 'aberta', 'param': p.get('param')})
            from watch_core import fileio
            os.makedirs(os.path.dirname(rec_path), exist_ok=True)
            fileio.write_json(rec_path, recorded, ensure_ascii=False, indent=1)

    if a.json:
        print(json.dumps({'analysis': an, 'proposals': props, 'followup': follow}, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(an, props, follow))
    return 0


if __name__ == '__main__':
    sys.exit(main())
