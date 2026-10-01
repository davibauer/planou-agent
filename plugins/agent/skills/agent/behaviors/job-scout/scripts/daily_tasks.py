#!/usr/bin/env python3
"""The job hunt as a task section on the daily Notion page (optional; config "daily_tasks").

The page, its sections and the checklist engine belong to watch_core (shared with work-watch: every work instance
writes its `## <CODE>` section on the same day page). This file only says what is the scout's:

  To do today   open recruiter messages (reply) and interviews: things that need an action from the user
  Shortlist     the day's "apply today" list (frozen once a day): opportunities, not obligations, so they get their
                own block instead of sitting in "To do today" (26/09/2026, user's request)
  Waiting on    applications sent (applied / replied), with the company and since when (in Planou, "Aguardando por"
                the company and the application date)
  Done today    what was applied or answered today

A box ticked on the page is read on the next tick: "Apply" -> the job becomes `applied`; a message -> the pending item is
resolved; anything else -> done for the day. No speech and no "Yesterday" block (speech=False): the section joins the
page the work agents (work-watch) created and never creates it; config.json of watch_core `notion.last_sections` keeps it at the
bottom of the page.

config.json: "daily_tasks": {"code": "LKD", "apply_top": 3, "labels": "pt"}   (missing key = feature off)
Needs the Notion token of watch_core (~/.config/watch-core/secrets/notion.env) and notion.month_page in its config.

Planou (optional, independent of the Notion page): the same list goes to Planou through watch_core.planou.
config.json: "planou": {"project": "CAR", "confidentiality": "title", "publish": true}   (missing key = off)
plus "planou.enabled" and "planou.base_url" in watch_core's config and the agent key in secrets/planou.env
(`python3 -m watch_core.planou --agent job-scout key set`). Completing a task in Planou works like ticking its box
on the page (apply_box); the event is acknowledged afterwards.

Project states (Planou 0.21.0): when the project's states are customized (GET /agent/projects/CAR/states, cached an
hour), each task also goes with `project_state`, the name `planou.stage_map` gives to its funnel stage: "Aplicar" (a<id>)
open = `apply`, "Retorno" (w<id>) open = the job's status (`applied`/`replied`), "Preparar entrevista" (i<id>) open =
`interview`, a closed a/w/i task = the job's status (`applied`, `interview`, `offer`, `hired`, `lost`, ...), a recruiter
message (m<key>) = `message`. Names are never made up: a stage without a name the project has prints one
`AVISO (planou): etapa ...` a day and the task goes with `state` only. A name whose category disagrees with the task
(an open stage for a closed task, or the other way round, or backlog) is not sent: a matching project_state wins over
`state`, so it would reopen or close the task. With the default states (customized false) only `state` goes, as before.
The person moving a task to a state that the map names changes the job's status the same way (apply_stage).
A move BACK in the funnel (Retorno -> Aplicar, Entrevista -> Retorno, Entrevista -> Aplicar, or reopening "Aplicar"
after applying) is a restart: the task of the stage the job left (w<id>, i<id>) is sent closed "Sem ação", dated by the
move, with the note "a vaga voltou para ..." in its description; the task of the stage it went back to opens again
(a<id> joins today's list). Moving forward again later reopens the same task (same source_key: the sync reopens it).
"""
import json
import re
from datetime import datetime, timedelta, timezone

import criteria
import paths
from watch_core import deadlines
from watch_core import planou
from watch_core import tasks as tc

APPLY_TOP = 3
SHORTLIST = {'pt': 'Shortlist (oportunidades para aplicar)', 'en': 'Shortlist (jobs worth applying to)'}
OPEN_JOB = {'applied': 'Aguardando', 'replied': 'Aguardando', 'interview': 'A fazer'}
# A task leaves the list when its job moves on, but Planou only changes what the sync sends: a task that simply stops
# being sent stays open there forever (28/09/2026: "Retorno" still waiting after the interview was booked). So the task
# is sent once more, closed, dated by the move: for planou.CLOSED_WINDOW, and after that while Planou still has it open
# (planou.open_codes(): a runner stopped longer than the window must still close it, PLN0028).
WAITING = ('applied', 'replied')
AFTER_WAITING = {'interview': 'Feito', 'offer': 'Feito', 'hired': 'Feito', 'lost': 'Sem ação'}   # w<id> "Retorno"
AFTER_INTERVIEW = {'offer': 'Feito', 'hired': 'Feito', 'lost': 'Sem ação'}                       # i<id> "Preparar entrevista"
# A move BACK in the funnel is a restart (28/09/2026, user's decision): the task of the stage the job left is sent once
# more, closed without action ("Sem ação") and dated by the move, with a note saying where the job went; the task of the
# stage it went back to reopens as usual. Per task: (the stages it belongs to, the earlier statuses that abandon it).
RESTART = {'w': (WAITING, ('interested',)), 'i': (('interview',), ('interested', 'applied', 'replied'))}
BACK_NAME = {'interested': 'Aplicar', 'applied': 'Retorno', 'replied': 'Retorno'}
# planou.stage_map: funnel stage -> the name of a state of the Planou project. `apply` is the open "Aplicar" task (the job
# is still `interested`), `message` a recruiter message; the rest are job statuses. The optional ones print no warning
# when missing from the map (no funnel column is expected for them).
FUNNEL = ('interested', 'applied', 'replied', 'interview', 'offer', 'hired', 'lost')
STAGE_STATUS = {'apply': 'interested'}
OPTIONAL_STAGES = ('interested', 'message')


def settings():
    c = criteria.load().get('daily_tasks')
    return c if isinstance(c, dict) and c.get('code') else None


def _today(now):
    return now.astimezone(tc.BRT).date().isoformat()


def _iso(x):
    return tc._data(x) if x else None


def _when(v, status):
    """When the job last entered `status` (history), or None."""
    return next((h.get('when') for h in reversed(v.get('history') or []) if h.get('status') == status), None)


def _who(v):
    r = v.get('recruiter')
    name = (r.get('name') if isinstance(r, dict) else r) or ''
    return f'{name} ({v.get("company")})' if name and v.get('company') else name or v.get('company') or '?'


def _moved_on(v, left, to):
    """(when, status) the job left the `left` stages for one of `to` (the first such entry after the last one in `left`),
    or (None, None). Later moves (an offer after the interview, lost after it) change neither, so the closed task is sent
    the same way every time."""
    hist = v.get('history') or []
    last = max((i for i, h in enumerate(hist) if h.get('status') in left), default=None)
    if last is None: return None, None
    h = next((h for h in hist[last + 1:] if h.get('status') in to), {})
    return h.get('when'), h.get('status')


def _restarted(v, left, to):
    """(when, entry) the job went straight from one of `left` back to one of `to` and stayed there (the current status is
    in `to`), or (None, None). Dated by the move, so every tick sends the same closed task; once the job moves on again
    the next move is the one that counts."""
    if v.get('status') not in to: return None, None
    hist = v.get('history') or []
    k = max((i for i, h in enumerate(hist) if h.get('status') not in to), default=None)
    if k is None or hist[k].get('status') not in left or k + 1 >= len(hist): return None, None
    return hist[k + 1].get('when'), hist[k + 1]


def _back_note(entry):
    """'a vaga voltou para "Aplicar" em 28/09 11:00': the state the person named, or the funnel stage's usual name."""
    m = re.search(r'para "([^"]+)"', str(entry.get('note') or ''))
    name = m.group(1) if m else BACK_NAME.get(entry.get('status'), entry.get('status') or '?')
    return f'Sem ação: a vaga voltou para "{name}" em {planou.when_label(entry.get("when"))}; esta etapa recomeça de lá.'


def _back_to_apply(s, jid, now):
    """The job went back to `interested`: its "Aplicar" task opens again today (joins today's list, even before the list
    is frozen) and a box ticked before no longer counts. The next days follow the shortlist rules."""
    st = s.setdefault('daily_tasks', {})
    st.get('done', {}).pop(f'a{jid}', None)
    lists, day = st.setdefault('apply', {}), _today(now)
    if day in lists:
        if jid not in lists[day]: lists[day].append(jid)
    else:
        extra = st.setdefault('apply_extra', {}).setdefault(day, [])
        if jid not in extra: extra.append(jid)


def _recent(when, now, code=None, still=()):
    """`when` is within planou.CLOSED_WINDOW, or `code` is a task Planou still has open (`still`, planou.open_codes())."""
    if when and code in still: return True
    try: return bool(when) and now - datetime.fromisoformat(when.replace('Z', '+00:00')) <= planou.CLOSED_WINDOW
    except ValueError: return False


def view(s):
    """The dict the engine reads and writes: the scout's state stays the only source; the engine's own bookkeeping
    (ids LKD-N, the rendered page, overrides) lives in s['daily_tasks']."""
    st = s.setdefault('daily_tasks', {})
    return {'acoes': [], 'pendencias': [], 'tarefas_notion': st.setdefault('engine', {}),
            'daily': st.setdefault('page', {})}


def apply_list(s, now, shortlist_rank, n):
    """The day's "apply today" ids, frozen at the first tick of the day so the list does not shift while jobs move.
    The lists of the last days stay (planou.CLOSED_WINDOW, or longer while a leftover is still open in Planou) so their
    leftovers can be closed in Planou."""
    st = s.setdefault('daily_tasks', {}); day = _today(now)
    lists = st.setdefault('apply', {})
    if day not in lists: lists[day] = shortlist_rank(s, now)[:n]
    extra = st.get('apply_extra') or {}
    lists[day] += [j for j in extra.pop(day, []) if j not in lists[day]]      # sent back to "Aplicar" before the freeze
    for k in [k for k in extra if k < day]: extra.pop(k)
    keep = _today(now - planou.CLOSED_WINDOW - timedelta(days=1))
    still = planou.open_codes()
    for k in [k for k in lists if k < keep and not any(f'a{j}' in still for j in lists[k])]: lists.pop(k)
    return lists[day]


def _left_shortlist(s, now):
    """{job id: the last day it was on the list} for the jobs of the last days' lists that are not in today's."""
    lists = (s.get('daily_tasks') or {}).get('apply') or {}
    day, out = _today(now), {}
    for k in sorted(k for k in lists if k < day):
        for jid in lists[k]: out[jid] = k
    for jid in lists.get(day) or []: out.pop(jid, None)
    return out


def _process(company):
    """First sentence of the hiring process saved in the reputation cache ('prova: sim. ...'), or ''."""
    try: rep = json.load(open(paths.REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): return ''
    c = (company or '').lower()
    e = next((x for k, x in rep.items() if c and (k.lower() in c or c in k.lower())), None) or {}
    return str(e.get('process') or '').partition('. ')[0].rstrip('.')


def items(s, now, shortlist_rank, n=APPLY_TOP, secao=SHORTLIST['pt']):
    """{code: item} in the engine's format (fontes_extras)."""
    out, day, still = {}, _today(now), planou.open_codes()
    jobs = (s.get('jobs') or {}).get('tracked') or {}
    done = (s.get('daily_tasks') or {}).get('done') or {}
    for p in s.get('pending') or []:
        if p.get('source') != 'mensagens' or p.get('status') == 'ignorar': continue
        fim = p.get('resolved_at') if p.get('status') == 'resolvida' else None
        if fim and not _recent(fim, now, f'm{p["key"]}', still): continue          # resolved: sent closed while Planou takes it (not only that day)
        who = (p.get('who') or '').strip()
        out[f'm{p["key"]}'] = {'titulo': f'Responder {who}: {" ".join((p.get("subject") or "").split())[:160]}',
                               'curto': tc._corta(f'Responder {who} no LinkedIn'), 'status': 'Feito' if fim else 'A fazer',
                               'origem': 'mensagens', 'quem': who, 'entrou': _iso(p.get('since') or p.get('created')),
                               'concluida': _iso(fim), 'prio': 1,
                               'link': f'https://www.linkedin.com/messaging/thread/{p["ref"]}/' if p.get('ref') else '',
                               'trecho': ' '.join((p.get('subject') or '').split()),
                               # "responde até sexta?": the Prazo, resolved against the day THAT message came (`since`
                               # is the first message of the conversation: without msg_at, no deadline)
                               'deadline': deadlines.resolve(p.get('subject'), p['msg_at'], message=True) if p.get('msg_at') else None}
    today = apply_list(s, now, shortlist_rank, n)
    left = _left_shortlist(s, now)
    for jid in today + [j for j in left if j not in today]:
        v = jobs.get(jid)
        if not v: continue
        name = f'{v.get("title") or "?"} ({v.get("company") or "?"})'
        applied = _when(v, 'applied')
        if jid in left:
            # off the list: applied = done on the day it was; else the day's chance passed without action
            if applied and f'a{jid}' not in done and v.get('status') != 'interested':
                st, fim = 'Feito', applied
            elif f'a{jid}' in done: st, fim = 'Feito', done[f'a{jid}']
            else:                                   # closed at the end of its last day on the list
                st, fim = 'Sem ação', (datetime.fromisoformat(left[jid]) + timedelta(days=1)).date().isoformat() + 'T00:00:00-03:00'
            if not _recent(fim, now, f'a{jid}', still): continue
            entrou = _iso(left[jid] + 'T09:00:00-03:00')
        else:
            st = 'Feito' if v.get('status') != 'interested' and applied and _today(datetime.fromisoformat(applied)) == day else \
                 'Descartada' if v.get('status') != 'interested' else 'Feito' if f'a{jid}' in done else 'A fazer'
            fim = applied if st == 'Feito' and applied else done.get(f'a{jid}')
            entrou = _iso(day + 'T09:00:00-03:00')
        proc = _process(v.get('company')) or ('prova no anúncio: ' + '/'.join(v['tech_test']) if v.get('tech_test') else 'processo não pesquisado')
        out[f'a{jid}'] = {'titulo': f'Aplicar: {name} — processo: {proc} — {v.get("note") or ""}', 'curto': tc._corta(f'Aplicar: {name}'),
                          'status': st, 'origem': 'shortlist', 'secao': secao, 'entrou': entrou,
                          'concluida': _iso(fim), 'prio': 2, 'link': v.get('url') or f'https://www.linkedin.com/jobs/view/{jid}',
                          'deadline': deadlines.valid(v.get('apply_until'))}          # the ad's closing date, never the posting expiry
    for jid, v in jobs.items():
        name = f'{v.get("title") or "?"} ({v.get("company") or "?"})'
        link = v.get('url') or f'https://www.linkedin.com/jobs/view/{jid}'
        st = OPEN_JOB.get(v.get('status'))
        moved, to = _moved_on(v, WAITING, AFTER_WAITING) if v.get('status') in AFTER_WAITING else (None, None)
        if st == 'Aguardando' or _recent(moved, now, f'w{jid}', still):
            # "Retorno": waiting on the company; once the job moved on, the same task closed on the day it moved
            closed = AFTER_WAITING[to] if st != 'Aguardando' else None
            since = (_when(v, 'replied') if closed else _when(v, v['status'])) or _when(v, 'applied')
            proc = _process(v.get('company'))
            out[f'w{jid}'] = {'titulo': f'Retorno: {name}' + (f' — processo: {proc}' if proc else ''), 'curto': tc._corta(f'Retorno: {name}'),
                              'status': closed or st, 'origem': 'candidatura', 'quem': _who(v),
                              'entrou': _iso(since), 'concluida': _iso(moved) if closed else None, 'prio': 5, 'link': link,
                              # Planou "Aguardando por": the company, since the application (not the reply)
                              'aguardando_quem': v.get('company') or _who(v),
                              'aguardando_desde': _iso(_when(v, 'applied') or since)}
        key = f'i{jid}'
        moved, to = _moved_on(v, ('interview',), AFTER_INTERVIEW) if v.get('status') in AFTER_INTERVIEW else (None, None)
        if st == 'A fazer' or _recent(moved, now, key, still):
            since = _when(v, 'interview') or _when(v, 'applied')
            closed = 'Feito' if key in done else AFTER_INTERVIEW[to] if st != 'A fazer' else None
            out[key] = {'titulo': f'Preparar entrevista: {name}', 'curto': tc._corta(f'Preparar entrevista: {name}'),
                        'status': closed or st, 'origem': 'entrevista', 'entrou': _iso(since),
                        'concluida': _iso(done.get(key) or moved) if closed else None, 'prio': 1, 'link': link,
                        'deadline': deadlines.valid(v.get('interview_at'))}      # prepare by the interview day
        for kind, (left, to) in RESTART.items():
            back, entry = _restarted(v, left, to)
            if f'{kind}{jid}' in out or not _recent(back, now, f'{kind}{jid}', still): continue
            title = {'w': 'Retorno', 'i': 'Preparar entrevista'}[kind]
            out[f'{kind}{jid}'] = {'titulo': f'{title}: {name}', 'curto': tc._corta(f'{title}: {name}'), 'status': 'Sem ação',
                                   'origem': {'w': 'candidatura', 'i': 'entrevista'}[kind], 'quem': _who(v) if kind == 'w' else '',
                                   'entrou': _iso(_when(v, left[0]) or _when(v, left[-1])), 'concluida': _iso(back),
                                   'prio': 5 if kind == 'w' else 1, 'link': link, 'evidencia': _back_note(entry), 'restart': True}
    for code, it in out.items():
        _origin(code, it, jobs)
        it['planou_stage'] = None if it.get('restart') else _stage(code, it, jobs)
        _ready(code, it, jobs)
    return out


def _ready(code, it, jobs):
    """Planou 0.25.0: why an open task is ready by itself (else it is born in backlog, a suggestion). A recruiter message,
    an application sent and an interview booked already happen at the source; the shortlist ("Aplicar") is the scout's
    judgment, an opportunity and not an obligation: backlog, unless the session marks it (`pronta`). Closed tasks need
    none (watch_core.planou sends a reason only for open states)."""
    kind, company = code[:1], (jobs.get(code[1:]) or {}).get('company') or ''
    if kind == 'm':
        it['ready_reason'] = f'{it.get("quem") or "recrutador"} escreveu no LinkedIn e espera resposta'
        it['ready_reason_min'] = 'mensagem de recrutador esperando resposta'
    elif kind == 'w':
        it['ready_reason'] = f'candidatura enviada{" a " + company if company else ""}, aguardando retorno'
        it['ready_reason_min'] = 'candidatura enviada, aguardando retorno'
    elif kind == 'i':
        it['ready_reason'] = f'entrevista marcada{" com " + company if company else ""}'
        it['ready_reason_min'] = 'entrevista marcada'


def _stage(code, it, jobs):
    """The funnel stage of a task (the key of planou.stage_map), or None."""
    kind = code[:1]
    if kind == 'm': return 'message'
    status = (jobs.get(code[1:]) or {}).get('status')
    if planou.STATE_OF.get(it.get('status'), ('todo',))[0] != 'closed':
        return {'a': 'apply', 'i': 'interview'}.get(kind) or status
    return status if status in FUNNEL else None


def _origin(code, it, jobs):
    """Planou: where the task came from (a recruiter message, a job of the shortlist, an application, an interview) and a
    description with what to do; watch_core.planou applies the confidentiality."""
    from watch_core import planou
    kind = code[:1]
    v = jobs.get(code[1:]) or {}
    link = it.get('link') or None
    board = 'linkedin' if code[1:].isdigit() else 'job_board'
    if kind == 'm':
        label = ' · '.join(x for x in ('LinkedIn', f'mensagem de {it.get("quem") or "?"}', planou.when_label(it.get('entrou'))) if x)
        org = {'type': 'linkedin', 'label': label, 'url': link, 'author': it.get('quem') or None, 'at': it.get('entrou')}
        desc = [f'De onde veio: {label}.', f'Trecho: "{it["trecho"][:600]}"' if it.get('trecho') else 'Trecho: o estado não guardou o texto.',
                f'O que fazer: responder {it.get("quem") or "a mensagem"} no LinkedIn (rascunho no Precisa de você).']
    else:
        what = {'a': 'vaga da lista curta', 'w': 'candidatura enviada', 'i': 'entrevista marcada'}.get(kind, 'vaga')
        company = v.get('company') or '?'
        label = ' · '.join(x for x in (planou.ORIGIN_NAME[board], what, company, planou.when_label(it.get('entrou'))) if x)
        org = {'type': board, 'label': label, 'url': link, 'author': None, 'at': it.get('entrou')}
        desc = [f'De onde veio: {label}.', f'O que fazer: {" ".join(str(it.get("titulo") or "").split())[:1500]}']
        if v.get('note'): desc.append(f'Nota do julgamento: {v["note"]}')
    if it.get('restart'): desc.insert(0, it['evidencia'])
    it['planou_origin'], it['planou_description'] = org, '\n'.join(desc)


def reopen_box(s, code, now=None):
    """A task reopened in Planou: undoes what ticking its box did. Returns True when something changed."""
    now = now or datetime.now(timezone.utc)
    if code.startswith('m'):
        p = next((p for p in s.get('pending') or [] if p['key'] == code[1:] and p.get('status') == 'resolvida'), None)
        if not p: return False
        p.update(status='aberta'); p.pop('resolved_at', None); p.pop('how', None); return True
    if code.startswith('a'):
        done = (s.get('daily_tasks') or {}).get('done') or {}
        v = ((s.get('jobs') or {}).get('tracked') or {}).get(code[1:])
        changed = done.pop(code, None) is not None
        if v and v.get('status') == 'applied':
            v['status'] = 'interested'; v.setdefault('history', []).append({'when': now.isoformat(), 'status': 'interested', 'note': 'reaberta no Planou'})
            _back_to_apply(s, code[1:], now)          # a restart like any move back: "Retorno" closes without action
            changed = True
        return changed
    if code.startswith('i'):
        return (s.get('daily_tasks') or {}).get('done', {}).pop(code, None) is not None
    return False


def apply_box(s, code, status, now=None):
    """A box ticked on the page (or a task completed or reopened in Planou). Returns True when the scout's state changed."""
    if status == 'A fazer': return reopen_box(s, code, now)
    if status != 'Feito': return False
    now = now or datetime.now(timezone.utc)
    if code.startswith('m'):
        p = next((p for p in s.get('pending') or [] if p['key'] == code[1:] and p.get('status') == 'aberta'), None)
        if not p: return False
        p.update(status='resolvida', resolved_at=now.isoformat(), how='marcada no Notion'); return True
    if code.startswith('a'):
        v = ((s.get('jobs') or {}).get('tracked') or {}).get(code[1:])
        if not v or v.get('status') != 'interested': return False
        v['status'] = 'applied'; v.setdefault('history', []).append({'when': now.isoformat(), 'status': 'applied'})
        return True
    if code.startswith('i'):
        s.setdefault('daily_tasks', {}).setdefault('done', {})[code] = now.isoformat(); return True
    return False


def configure(cfg, s, shortlist_rank):
    tc.configura(cfg['code'], speech=False, rotulos=tc.ROT_EN if cfg.get('labels') == 'en' else {},
                 fontes_extras=lambda v, agora: items(s, agora or datetime.now(timezone.utc), shortlist_rank,
                                                     int(cfg.get('apply_top') or APPLY_TOP),
                                                     SHORTLIST['en' if cfg.get('labels') == 'en' else 'pt']))


def _due_today(v, s, now):
    """Messages and interviews go to "To do today" (the engine plans by due date); the shortlist has its own block."""
    pr = v['tarefas_notion'].setdefault('prazos', {})
    day = _today(now)
    its = tc._itens_crus(v, now)
    for c, it in its.items():
        if it['status'] == 'A fazer' and not it.get('secao') and c not in pr: pr[c] = day
    for c in [c for c in pr if c not in its or its[c]['status'] != 'A fazer' or its[c].get('secao')]: pr.pop(c)


def tick(s, shortlist_rank, dry=False, now=None):
    """Publishes the section. Returns lines for the tick output (only when a box ticked on the page changed something)."""
    cfg = settings()
    if not cfg: return []
    now = now or datetime.now(timezone.utc)
    configure(cfg, s, shortlist_rank)
    v = view(s)
    _due_today(v, s, now)
    lines = tc.tick(v, dry, lambda c, st: apply_box(s, c, st, now))
    return [ln for ln in lines if 'marcada na pagina' in ln or 'DECISAO' in ln]


def planou_settings():
    c = criteria.load().get('planou')
    return c if isinstance(c, dict) and c.get('publish', True) is not False else None


def apply_answer(s, ev, now=None):
    """An answer given in Planou ("Precisa de você"), applied to the scout's state. Returns True when it changed.

    draft_sent on a recruiter message (task m<key>): the pending message is resolved, as the user answered it.
    approved on an application (task a<id>): only the OK is recorded in the job history. Nothing is sent because of it:
    the application goes on through the usual flow, with the user (hard rule of this phase).
    """
    now = now or datetime.now(timezone.utc)
    p = ev.get('payload') or {}
    key = p.get('task_source_key') or ''
    code = key.split(':', 1)[1] if key.startswith('job-scout:') else ''
    kind = ev.get('type')
    if kind == 'draft_sent' and code.startswith('m'):
        pend = next((x for x in s.get('pending') or [] if x['key'] == code[1:] and x.get('status') == 'aberta'), None)
        if not pend: return False
        pend.update(status='resolvida', resolved_at=now.isoformat(), how='respondido pelo usuario (Enviei no Planou)')
        return True
    if kind in ('approved', 'rejected') and code.startswith('a'):
        v = ((s.get('jobs') or {}).get('tracked') or {}).get(code[1:])
        if not v: return False
        note = ('OK da candidatura registrado no Planou (nada enviado; o envio segue com o usuario)' if kind == 'approved'
                else 'candidatura recusada pelo usuario no Planou')
        v.setdefault('history', []).append({'when': now.isoformat(), 'status': v.get('status'), 'note': f'{note} [{p.get("code")}]'})
        return True
    return False


# Planou "Ferramentas do agente": the scout's sources, by the name the tick uses for them (s['broken'] keeps the failing
# ones, with the error and since when). The LinkedIn cookie has no known expiry (session.json keeps only when it was
# saved), so none is sent.
TOOLS = {   # source: (label, credential type, how to fix it)
    'mensagens': ('LinkedIn (mensagens)', 'session_cookie',
                  'Grave de novo os cookies li_at e JSESSIONID do LinkedIn (linkedin_msgs.py --session).'),
    'recomendadas': ('LinkedIn (recomendadas)', 'session_cookie',
                     'Grave de novo os cookies li_at e JSESSIONID do LinkedIn (linkedin_msgs.py --session).'),
    'vagas': ('LinkedIn (busca pública)', 'none',
              'Veja o erro do tick: bloqueio temporário ou o HTML da busca mudou (linkedin_jobs.py).'),
    'mercado': ('Sites de vaga remota', 'none', 'Veja o erro do tick: um site mudou ou está fora (job_boards.py).'),
    'notion': ('Notion', 'api_key', 'Confira o token do Notion (secrets/notion.env do watch-core) e o acesso à base.'),
}


def tools(s, logged_in):
    """The tools list: the four sources (the logged-in ones `off` when the config does not turn them on) and Notion
    when the funnel base is set up."""
    names = ['mensagens', 'recomendadas', 'vagas', 'mercado'] + (['notion'] if (s.get('notion_jobs') or {}).get('db') else [])
    srcs = [{'name': n, 'label': TOOLS[n][0], 'credential': TOOLS[n][1], 'fix': TOOLS[n][2],
             'off': n in ('mensagens', 'recomendadas') and not logged_in} for n in names]
    return planou.source_tools(srcs, s.get('broken') or {})


def _stage_warning(s, stage, data, now):
    """The line for a stage without a state of the project, once a day per stage ([] when already said today)."""
    warned = s.setdefault('planou_stages', {}).setdefault('warned', {})
    day = _today(now)
    if warned.get(stage) == day: return []
    warned[stage] = day
    names = ', '.join(x.get('name') or '?' for x in (data or {}).get('states') or [])
    return [f'AVISO (planou): etapa "{stage}" sem estado correspondente no projeto {(data or {}).get("project") or planou._S["project"]} '
            f'(estados: {names or "nenhum"})']


def project_states(s, its, pc, now):
    """Puts `project_state` on the tasks whose stage the map names with a state the project has (customized projects
    only). Returns the day's warnings for the stages left without one."""
    data = planou.project_states(now=now)
    if not data or not data.get('customized'): return []
    by_key = {planou.state_key(x.get('name')): x for x in data.get('states') or [] if x.get('name')}
    smap = pc.get('stage_map') if isinstance(pc.get('stage_map'), dict) else {}
    missing = []
    for code, it in its.items():
        stage = it.get('planou_stage')
        if not stage: continue
        name = smap.get(stage)
        st = by_key.get(planou.state_key(name)) if name else None
        if not st:
            if (name or stage not in OPTIONAL_STAGES) and stage not in missing: missing.append(stage)
            continue
        closed = planou.STATE_OF.get(it.get('status'), ('todo',))[0] == 'closed'
        if st.get('category') == 'backlog' or (st.get('category') == 'closed') != closed: continue
        it['project_state'] = st['name']
    s['planou_stages'] = {**(s.get('planou_stages') or {}), 'last': data}
    return [ln for stage in missing for ln in _stage_warning(s, stage, data, now)]


def stage_warnings(s, its, now):
    """After the sync: a name Planou did not find (the project changed within the cache hour) -> the day's warning for
    that stage (planou.sync already dropped the cached states, so the next tick asks again)."""
    out = []
    for w in planou.sync_warnings():
        code, _, text = w.partition(': ')
        stage = (its.get(code) or {}).get('planou_stage')
        if 'project_state' in text and stage:
            out += _stage_warning(s, stage, (s.get('planou_stages') or {}).get('last'), now)
    return out


def apply_stage(s, code, name, smap, now=None):
    """The person moved a task to another state of the project: when the map names that state for a funnel stage, the
    job takes that status (as `--pending-set <id> status=...` would). Returns True when the state changed."""
    now = now or datetime.now(timezone.utc)
    if code[:1] not in ('a', 'w', 'i'): return False
    data = planou.project_states(now=now)
    if not data or not data.get('customized'): return False       # default states: nothing is mapped to them
    if not isinstance(smap, dict): return False
    statuses = {STAGE_STATUS.get(k, k) for k, v in smap.items() if v and planou.state_key(v) == planou.state_key(name)}
    statuses = [x for x in FUNNEL if x in statuses]
    v = ((s.get('jobs') or {}).get('tracked') or {}).get(code[1:])
    if not v or not statuses or v.get('status') in statuses: return False
    old, new = v.get('status'), statuses[0]
    v['status'] = new
    v.setdefault('history', []).append({'when': now.isoformat(), 'status': new, 'note': f'movida no Planou para "{name}"'})
    # a move back is a restart (items() closes the abandoned task from the history): what was ticked for the stages it
    # goes back to no longer counts, so their tasks open again
    if old in ('interview', 'offer', 'hired') and new in ('interested', 'applied', 'replied', 'interview'):
        (s.get('daily_tasks') or {}).get('done', {}).pop(f'i{code[1:]}', None)
    if new == 'interested' and old in FUNNEL[1:]: _back_to_apply(s, code[1:], now)
    return True


def planou_tick(s, shortlist_rank, now=None, plugin_version=None, logged_in=False):
    """Events from Planou first (the person's changes land in the state), then the full list, the files of the interview
    and application tasks, the tools list and the sign of life. Returns lines for the tick output. Does nothing without
    config or key."""
    pc = planou_settings()
    if not pc: return []
    now = now or datetime.now(timezone.utc)
    planou.configure(paths.NAME, paths.ROOT, project=pc.get('project'), confidentiality=pc.get('confidentiality') or 'title',
                     publish=pc.get('publish', True), live=pc.get('live', True), plugin_version=plugin_version)
    if not planou.active(): return []
    lines = planou.apply_events(planou.poll(), lambda c, st: apply_box(s, c, st, now), lambda ev: apply_answer(s, ev, now),
                                on_state=lambda c, name, sent: apply_stage(s, c, name, pc.get('stage_map'), now))
    cfg = settings() or {'code': 'LKD'}
    configure(cfg, s, shortlist_rank)
    v = view(s)
    _due_today(v, s, now)
    its = tc.itens(v, now)
    lines += project_states(s, its, pc, now)
    lines += planou.sync(its, now)
    lines += stage_warnings(s, its, now)
    try:
        import attachments
        lines += attachments.send(s, its, planou)
    except Exception as e:                        # a file never costs the sync or the sign of life
        lines.append(f'AVISO (planou): anexos: {type(e).__name__}: {str(e)[:120]}')
    try:
        planou.set_tools(tools(s, logged_in), now)
    except Exception as e:
        lines.append(f'AVISO (planou): ferramentas: {type(e).__name__}: {str(e)[:120]}')
    lines += planou.heartbeat('tick', now=now)
    return lines


def show(s, shortlist_rank, now=None):
    """--daily-tasks: the section as it would be published (nothing is written)."""
    cfg = settings() or {'code': 'LKD'}
    now = now or datetime.now(timezone.utc)
    configure(cfg, s, shortlist_rank)
    v = json.loads(json.dumps(view(s)))          # a copy: showing never writes
    _due_today(v, s, now)
    tc.ids(v)
    return tc.render_md(v, _today(now))
