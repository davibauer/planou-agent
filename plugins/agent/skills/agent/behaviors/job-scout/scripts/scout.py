#!/usr/bin/env python3
"""job-scout: one tick looks at MESSAGES (LinkedIn inbox via the internal Voyager API, li_at cookie), RECOMMENDED
(logged-in "top job picks" feed on the jobs home, with Premium's TOP APPLICANT signal; linkedin_recommended.py) and JOBS
(public jobs-guest search, no login, filtered by the CRITERIA in linkedin_jobs.py) since the last run and prints ONLY
what is new. Nothing new: no output, exit 0. A failure in one source does not hide the other: it comes out as
"FONTE QUEBRADA (source): ..." (up to 0.18: "VIGIA QUEBRADO"; runner.sh accepts both) and exit 2 at the end.

job-scout: own state in ~/.config/job-scout/data/state.json,
pending-items code copied on purpose (a bug in one skill does not take down the other, which may be armed at the same time).
1 h cadence, not 15 min: the LinkedIn account is personal and the site detects automation; one inbox call per tick.

Usage:
  scout.py                        # full tick (messages + jobs), saves cursors
  scout.py --dry                  # tick without saving
  scout.py --only messages|recommended|jobs|boards
  scout.py --since 4h             # ignores the cursors and looks at the last 4h (both sources); does not save
  scout.py --json
  scout.py --pending            # pending queue (message from someone else without your reply)
  scout.py --resolve pN          # removes an item from the queue (or --pending-set pN status=ignorar / note="...")
  scout.py --job <id> k=v [+ <id2> k=v ...]   # annotates jobs (status=interested|ignored|applied|replied|interview|offer|hired|lost, note="..."; the Portuguese keys and status names are also accepted); several in one command
  scout.py --jobs                # annotated jobs (all)
  scout.py --pipeline                # the pipeline: count per stage, stalled items (no movement for N days), response rate
  scout.py --week               # summary of the last 7 days (decisions, pipeline moves) — material for the weekly summary
  scout.py --gmail [cursor|mark <epoch>|triage "subject"...|body < text]   # support for the Gmail source (the model calls the connector; 'body' reads the alert's PLAIN_TEXT on stdin)
  scout.py --reengage            # recruiters whose conversation stopped 90+ days ago (the tick does this 1x/week, on Tuesday)
  scout.py --contact <thread> status=nao note="golpe"   # never suggest re-engaging this contact
"""
import os, sys, json, re, hashlib, argparse, time, types
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import schema
STATE = paths.STATE
import criteria
BRT = criteria.tz()   # historical name: it is the user's time zone (tz_hours in the config, default -3)
MAX_SEEN = 3000

sys.path.insert(0, HERE)
import linkedin_msgs as lm
import linkedin_jobs as lj
import linkedin_recommended as lr
import job_boards as jb
import companies as co
import notion_jobs as nj
import daily_tasks
from watch_core import lessons as _lessons      # shared with the work-watch behavior; this plugin's watch_core
from watch_core import deadlines                 # the Prazo in Planou (apply_until, interview_at)
from watch_core import fileio
# with no job decision yet (install day) there is nothing to learn, so the closing does not ask
lessons = types.SimpleNamespace(cli=_lessons.cli, tick=_lessons.tick, proposals=_lessons.propostas,
                                is_due=lambda s: bool((s.get('jobs') or {}).get('tracked')) and _lessons.devida(s))
AGENT_NAME = paths.NAME          # canonical agent name in the shared history (lessons): job-scout ('linkedin' up to 0.18, still read)
BROKEN_TOKEN = 'FONTE QUEBRADA'   # protocol line of a failed source; 'VIGIA QUEBRADO' up to 0.18 (runner.sh reads both)

def load_state():
    paths.lock_state()
    try: return json.load(open(STATE))
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fileio.write_json(STATE, s, indent=1, ensure_ascii=False)

def dt(iso):
    if not iso: return None
    d = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def fmt_time(iso):
    d = dt(iso); return d.astimezone(BRT).strftime('%d/%m %H:%M') if d else '??'

# ---------------- Pending items (copy from the other agents) ----------------
BUSINESS_HOURS = (8, 19)                  # BRT, Mon-Fri
REMIND_AFTER = {'mensagens': 11.0}        # 1 business day (08-19) until the 1st reminder: a recruiter DM is not urgent
MAX_REMINDERS = 2

def is_business(d):
    d = d.astimezone(BRT); return d.weekday() < 5 and BUSINESS_HOURS[0] <= d.hour < BUSINESS_HOURS[1]

def business_hours(a, b):
    if not a or not b or b <= a: return 0.0
    t, n, step = a, 0, timedelta(minutes=15)
    while t < b:
        if is_business(t): n += 1
        t += step
    return n / 4

def pending_items(s):
    return s.setdefault('pending', [])

def open_pending(s, source=None):
    return [p for p in pending_items(s) if p.get('status') == 'aberta' and (source is None or p['source'] == source)]

def enqueue(s, source, ref, who, subject, since, now):
    for p in pending_items(s):
        if p['source'] == source and p['ref'] == ref:
            if p.get('status') == 'aberta': p['who'], p['subject'] = who, subject; return p
            p.update(status='aberta', who=who, subject=subject, since=since, reminders=0, last_reminder=None, created=now.isoformat())
            return p
    s['pending_seq'] = s.get('pending_seq', 0) + 1
    p = {'key': f'p{s["pending_seq"]}', 'source': source, 'ref': ref, 'who': who, 'subject': subject, 'since': since,
         'status': 'aberta', 'reminders': 0, 'last_reminder': None, 'created': now.isoformat()}
    pending_items(s).append(p); return p

def resolve(p, now, how):
    p['status'] = 'resolvida'; p['resolved_at'] = now.isoformat(); p['how'] = how

def due_reminders(s, now, sources_ok):
    if not is_business(now): return []
    out = []
    for p in open_pending(s):
        if p['source'] not in sources_ok or p['reminders'] >= MAX_REMINDERS: continue
        base = dt(p['last_reminder'] or p['created'])
        if business_hours(base, now) >= REMIND_AFTER[p['source']]: out.append(p)
    return out

def print_pending(s, remind, resolved):
    if not (remind or resolved): return False
    now = datetime.now(timezone.utc)
    print('== PENDENTES')
    for p in remind:
        age = business_hours(dt(p['since']), now)
        print(f'-- {p["key"]} [{p["source"]}] {p["who"]} — {p["subject"]} · desde {fmt_time(p["since"])} ({age:.0f} h uteis) · lembrete {p["reminders"]}/{MAX_REMINDERS}')
    for p, how in resolved:
        print(f'   ✓ {p["key"]} [{p["source"]}] {p["who"]} — {p["subject"]} · resolvida ({how})')
    return True

def list_pending(s):
    opened = open_pending(s)
    if not opened: print('sem pendencias abertas'); return
    now = datetime.now(timezone.utc)
    print(f'{len(opened)} pendencias abertas:')
    for p in sorted(opened, key=lambda p: p['since']):
        silent = ' · silenciosa (lembretes esgotados)' if p['reminders'] >= MAX_REMINDERS else ''
        note = f' · nota: {p["note"]}' if p.get('note') else ''
        print(f'-- {p["key"]} [{p["source"]}] {p["who"]} — {p["subject"]} · desde {fmt_time(p["since"])} ({business_hours(dt(p["since"]), now):.0f} h uteis) · lembretes {p["reminders"]}{silent}{note}')
        print(f'   ref: {p["ref"]}')

# ---------------- Messages ----------------
def tick_messages(s, since, dry):
    st = s.setdefault('messages', {'cursor': None, 'seen': {}})
    now = datetime.now(timezone.utc)
    me = lm.me()
    if since is None: since = dt(st['cursor']) if st.get('cursor') else None
    if since is None:                      # first run: plants the cursor
        if not dry: st['cursor'] = now.isoformat(); st['me'] = me
        return {'baseline': True, 'me': me, 'chats': []}
    cache = lm._names()
    try:
        cs = lm.conversations(me['urn'])   # ONE call; +1..2 per conversation with new activity
        seen = st.setdefault('seen', {})   # thread -> 'when' of the last activity already reported
        active = [c for c in cs if c['when'] and dt(c['when']) > since and seen.get(c['id']) != c['when']]
        pend = {p['ref']: p for p in open_pending(s, 'mensagens')}
        new, resolved, left = [], [], 0
        if len(active) > lm.MAX_DETAILS: left = len(active) - lm.MAX_DETAILS; active = active[:lm.MAX_DETAILS]
        for c in active:
            lm.detail(c, me['urn'], cache, since)   # the pause between calls lives in lm.api
            if c['my_last']:
                p = pend.get(c['id'])
                if p and not dry and dt(c['when']) > dt(p['since']):
                    resolve(p, now, 'voce respondeu'); resolved.append((p, 'voce respondeu'))
            else:
                new.append(c)
            if not dry: seen[c['id']] = c['when']
        if not dry:
            for c in new:
                p = enqueue(s, 'mensagens', c['id'], c['with'], c['text'][:80], c['when'], now)
                p['msg_at'] = c['when']         # when the text in `subject` was written (`since` keeps the first one)
            # if an active conversation was left over, the cursor does NOT advance: the next run takes the rest (seen avoids repeating the ones already shown)
            if not left: st['cursor'] = now.isoformat()
            if len(seen) > 500: st['seen'] = dict(sorted(seen.items(), key=lambda kv: kv[1] or '')[-500:])
    finally:
        lm._save_names(cache)
    return {'baseline': False, 'me': me, 'chats': new, 'resolved': resolved, 'leftover': left}

def print_messages(r):
    if r['baseline']:
        print(f'== MENSAGENS: cursor plantado agora ({r["me"]["name"]}); a proxima rodada mostra o que chegar depois.'); return True
    ok = lm.print_block(r['chats'], r['me']['urn'])
    if r.get('leftover'): print(f'   (… +{r["leftover"]} conversas ativas ficam para a proxima rodada)'); ok = True
    return ok

# ---------------- Jobs ----------------
def tick_jobs(s, since, dry):
    st = s.setdefault('jobs', {'cursor': None, 'seen': [], 'tracked': {}})
    now = datetime.now(timezone.utc)
    if since is None: since = dt(st['cursor']) if st.get('cursor') else None
    if since is None:                      # first run: plants ALL jobs from the last 24h (unfiltered) as already seen
        jobs, errors = lj.collect(86400, filter_titles=False, max_details=0, pages=4)
        if not dry:
            st['cursor'] = now.isoformat(); st['seen'] = [v['id'] for v in jobs]
        return {'baseline': True, 'n': len(jobs), 'errors': errors}
    seen = set(st.get('seen') or [])
    # f_TPR is in seconds since posting; 1h slack because the card date is only the day and the index lags
    secs = max(3600, int((now - since).total_seconds()) + 3600)
    jobs, errors = lj.collect(secs, filter_titles=True, ignore=seen)
    alarm = jobs_alarm(st, lj.LAST_COLLECTION, dry)
    if not dry:
        st['cursor'] = now.isoformat()
        st['seen'] = (list(seen) + [v['id'] for v in jobs])[-MAX_SEEN:]
    return {'baseline': False, 'jobs': jobs, 'errors': errors, 'alarm': alarm}

ALARM_TICKS_WITHOUT_CARDS = 8      # ~4 h of consecutive ticks without ANY card in the searches (before any filter) = broken parser

def jobs_alarm(st, c, dry):
    """A broken parser does not error, it returns zero. Flags when: (1) N consecutive ticks without any raw card in the searches; (2) the
    detail pages of this tick all came without a description (3+). Returns the alarm text or ''."""
    z = (st.get('ticks_without_cards', 0) + 1) if c.get('cards', 0) == 0 else 0
    if not dry: st['ticks_without_cards'] = z
    if z >= ALARM_TICKS_WITHOUT_CARDS:
        return f'{z} ticks seguidos sem nenhuma vaga na busca publica (antes dos filtros): o LinkedIn pode ter mudado o HTML; conferir `linkedin_jobs.py --since 24h --all` e os regex de `_cards()`'
    if c.get('details', 0) >= 3 and c.get('empty_details') == c.get('details'):
        return f'{c["details"]} paginas de detalhe sem descricao: o LinkedIn pode ter mudado o HTML; conferir `linkedin_jobs.py --show <id>` e os regex de `detalhe()`'
    return ''

def print_jobs(r):
    if r['baseline']:
        print(f'== VAGAS: baseline plantada agora ({r["n"]} vagas das ultimas 24h marcadas como vistas); a proxima rodada mostra so as novas.')
        for e in r['errors']: print(f'   (busca falhou: {e})')
        return True
    return lj.print_block(r['jobs'], r['errors'])

# ---------------- Recommended (logged-in LinkedIn) ----------------
MAX_RECO_DETAILS = 8      # public detail pages per tick for the new recommended jobs

def tick_recommended(s, since, dry):
    st = s.setdefault('recommended', {'cursor': None, 'seen': []})
    now = datetime.now(timezone.utc)
    cs = lr.recommended()                  # ONE logged-in call
    if since is None and not st.get('cursor'):   # first run: plants the current recommendations as seen
        if not dry: st['cursor'] = now.isoformat(); st['seen'] = [c['id'] for c in cs]
        return {'baseline': True, 'n': len(cs), 'top': [c for c in cs if c['top']]}
    seen = set(st.get('seen') or []) if since is None else set()
    # EXCLUDE also applies to recommended, except TOP APPLICANT, which always passes
    new = [c for c in cs if c['id'] not in seen and (c['top'] or not lj.EXCLUDE.search(c['title']))]
    for i, v in enumerate(new):
        v['points'], v['tags'] = lj.score(v['title'])
        if i >= MAX_RECO_DETAILS: continue
        try:
            d = lj.detail(v['id']); v.update(level=d['level'], snippet=d['description'][:400].replace('\n', ' '), work_format=lj.work_format(d['description']), tech_test=lj.tech_test(d['description']),
                                            posted=d['posted'], days=d['days'], n_applicants=d['n_applicants'])
            v['points'], v['tags'] = lj.score(v['title'] + '\n' + d['description'])
        except RuntimeError as e: v['error'] = str(e)
        import time; time.sleep(1)
    # saturated ones (old + >100 applicants) are dropped; TOP APPLICANT is spared unless saturation.top_applicant_exempt is false
    sat = [v for v in new if lj.saturated_out(v, v['top'])]
    new = [v for v in new if not lj.saturated_out(v, v['top'])]
    new.sort(key=lambda v: (not v['top'], -v['points'], v['title']))
    if not dry:
        st['cursor'] = now.isoformat()
        st['seen'] = (list(seen) + [c['id'] for c in cs])[-MAX_SEEN:]
    return {'baseline': False, 'new': new, 'saturated': sat}

def print_recommended(r):
    if r['baseline']:
        print(f'== RECOMENDADAS: baseline plantada agora ({r["n"]} recomendacoes do LinkedIn marcadas como vistas); a proxima rodada mostra so as novas.')
        for c in r['top']: print(f'   (ja e TOP APPLICANT hoje: {c["title"]} — {c["company"]} · {c["location"]} · {c["url"]})')
        return True
    ok = lr.print_block(r['new'])
    if r.get('saturated'):
        print(f'   (+{len(r["saturated"])} saturada(s) fora — anuncio antigo com >100 candidatos: ' + '; '.join(f'{v["title"][:40]} — {v["company"]}' for v in r['saturated'][:5]) + ')')
        ok = True
    return ok

# ---------------- Boards (sources outside LinkedIn) ----------------
BOARDS_EVERY_H = 6      # boards run every 6 h (4x/day): 7 external requests per run, and job boards do not post hourly

def tick_boards(s, since, dry):
    st = s.setdefault('boards', {'cursor': None, 'seen': []})
    now = datetime.now(timezone.utc)
    if since is None and st.get('cursor') and (now - dt(st['cursor'])).total_seconds() < BOARDS_EVERY_H * 3600:
        return {'baseline': False, 'jobs': [], 'errors': {}, 'skipped': True}
    if since is None: since = dt(st['cursor']) if st.get('cursor') else None
    if since is None:                      # first run: plants the ids from the last 48h as seen
        jobs, errors = jb.collect(now - timedelta(hours=48), filter_titles=False)
        if not dry: st['cursor'] = now.isoformat(); st['seen'] = [v['id'] for v in jobs]
        return {'baseline': True, 'n': len(jobs), 'errors': errors}
    seen = set(st.get('seen') or []) if st.get('cursor') else set()
    jobs, errors = jb.collect(since - timedelta(hours=1), filter_titles=True, ignore=seen)
    if not dry:
        st['cursor'] = now.isoformat()
        st['seen'] = (list(seen) + [v['id'] for v in jobs])[-MAX_SEEN:]
    return {'baseline': False, 'jobs': jobs, 'errors': errors}

def print_boards(r):
    if r['baseline']:
        print(f'== MERCADO: baseline plantada agora ({r["n"]} vagas das ultimas 48h nas fontes de mercado marcadas como vistas).')
        for f, e in r['errors'].items(): print(f'   (fonte {f} falhou: {e})')
        return True
    return jb.print_block(r['jobs'], r['errors'])

# ---------------- Gmail (LinkedIn job alerts in personal Gmail; the call is the model's, via connector) ----------------
GMAIL_SENDERS = 'from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)'
GMAIL_HUMAN = ('-from:linkedin.com -from:glassdoor.com -from:indeed.com -category:promotions -category:social -category:updates '
               'subject:(recruiter OR "talent acquisition" OR opportunity OR oportunidade OR vaga OR interview OR entrevista OR "contract role")')

APPLIED_LIST_URL = 'https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED'   # no job link in the e-mail: the list of applications
GMAIL_CONFIRM = ('from:(jobs-noreply@linkedin.com OR jobs-listings@linkedin.com) '
                 'subject:("your application was sent to" OR "sua candidatura foi enviada para")')
CONFIRM_SUBJECT = re.compile(r'(?:your application was sent to|sua candidatura foi enviada para)\s+(.+?)\s*$', re.I)
CONFIRM_BOILER = ('view job', 'ver vaga', 'your application', 'sua candidatura', 'applied on', 'candidatou', 'next steps', 'we wish', 'good luck',
                  'boa sorte', 'unsubscribe', 'this email', 'este e-mail', 'linkedin corporation', 'you are receiving')

def gmail_confirmation(text):
    """Parses a LinkedIn application confirmation (headers From/Subject/Date, blank line, plain-text body). Returns
    {'company', 'title', 'job_id', 'when'} or None when it is not one (sender must be linkedin.com; subject in EN or PT)."""
    head, _, body = text.partition('\n\n')
    hd = {}
    for l in head.split('\n'):
        k, _, val = l.partition(':')
        if val and k.strip().lower() in ('from', 'subject', 'date'): hd[k.strip().lower()] = val.strip()
    if not hd.get('subject'):                        # no headers block: the whole text is the body
        body = text
    sender = re.search(r'@([\w.-]+)', hd.get('from', ''))
    if not sender or not (sender.group(1).lower() == 'linkedin.com' or sender.group(1).lower().endswith('.linkedin.com')): return None
    m = CONFIRM_SUBJECT.search(hd.get('subject', ''))
    if not m: return None
    company = m.group(1).strip().rstrip('.!')
    title = ''
    lines = [l.strip() for l in body.split('\n') if l.strip()]
    for i, l in enumerate(lines):
        if CONFIRM_SUBJECT.search(l):
            for c in lines[i + 1:i + 4]:
                if c.lower().startswith(CONFIRM_BOILER) or _norm(c) == _norm(company) or c.startswith('http'): continue
                title = c.split(' · ')[0].strip(); break
            break
    jid = re.search(r'jobs/view/(\d+)', body)
    when = None
    raw = hd.get('date')
    if raw:
        try:
            from email.utils import parsedate_to_datetime
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try: when = dt(raw)
            except ValueError: when = None
        if when is not None and when.tzinfo is None: when = when.replace(tzinfo=timezone.utc)
    mid = re.search(r'^message-id:\s*(.+)$', head, re.I | re.M)
    return {'message_id': mid.group(1).strip() if mid else '', 'subject': hd['subject'], 'company': company, 'title': title, 'job_id': jid.group(1) if jid else '', 'when': when.astimezone(timezone.utc).isoformat(timespec='seconds') if when else None}

def gmail_applied(s, text, now=None):
    """The LinkedIn confirmation e-mail marks the job as applied, with the e-mail's date and time. Match: job id in the body,
    else company (+ title when the body brings it). Ambiguous (same company, several jobs, no title) changes nothing and is
    listed. No job in the funnel: new entry already 'applied' (source gmail). Idempotent: the same e-mail changes nothing."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    c = gmail_confirmation(text)
    if not c: print('gmail: nao e confirmacao de candidatura do LinkedIn (remetente linkedin.com + assunto "your application was sent to" / "sua candidatura foi enviada para")'); return
    st = s.setdefault('gmail', {'cursor': None, 'seen': []})
    an = s.setdefault('jobs', {}).setdefault('tracked', {})
    when = c['when'] or now_iso
    label = f'{c["title"] or "(titulo nao informado)"} — {c["company"]}'
    ce, ct = _norm(c['company']), _norm(c['title'])
    ek = f'applied|{ce}|{ct}|' + (c['when'] or c['message_id'] or _norm(c['subject']))   # no Date: stable key (Message-ID, else subject)
    done = st.setdefault('applied_seen', [])
    if ek in done: print(f'-- ja registrada: {label}'); return
    closed = IN_PROGRESS + ('hired', 'lost')
    same_co = [(k, v) for k, v in an.items() if ce and _norm(v.get('company')) == ce]
    if c['job_id'] and c['job_id'] in an: hit = [(c['job_id'], an[c['job_id']])]
    elif ct:
        hit = [(k, v) for k, v in same_co if _norm(v.get('title')) == ct] or [(k, v) for k, v in same_co if _norm(v.get('title')) and (ct in _norm(v['title']) or _norm(v['title']) in ct)]
    else:
        hit = [(k, v) for k, v in same_co if v.get('status') not in closed] or same_co
    if len(hit) > 1:
        open_ = [(k, v) for k, v in hit if v.get('status') not in closed]
        if len(open_) != 1:
            print(f'?? ambigua: {label}: {len(hit)} vagas dessa empresa no funil ({", ".join(k for k, _ in hit)}); nada mudou, listar no relatorio'); return
        hit = open_
    day = dt(when).astimezone(BRT).strftime('%d/%m %H:%M')
    def remember(): done.append(ek); st['applied_seen'] = done[-500:]
    if hit:
        k, v = hit[0]
        remember()
        if v.get('status') in closed:
            print(f'-- ja estava em {schema.label(v["status"])}: {v.get("title", "")} — {v.get("company", "")}'); return
        v['status'] = 'applied'; v['when'] = when
        v.setdefault('history', []).append({'when': when, 'status': 'applied'})
        v['note'] = (v.get('note') or '') + f' | e-mail de confirmacao do LinkedIn em {day}'
        print(f'== APLICADAS (e-mail de confirmacao do LinkedIn; registrei no funil)\n-- {v.get("title", "")} — {v.get("company", "")} · aplicou em {day} · movida para aplicado'); return
    jid = c['job_id'] or 'gmail-' + hashlib.sha1(f'{ce}|{ct}|{c["when"] or c["message_id"] or _norm(c["subject"])}'.encode()).hexdigest()[:10]
    an[jid] = {'status': 'applied', 'title': c['title'], 'company': c['company'], 'source': 'gmail', 'url': (f'https://www.linkedin.com/jobs/view/{c["job_id"]}' if c['job_id'] else APPLIED_LIST_URL), 'when': when, 'history': [{'when': when, 'status': 'applied'}],
               'note': f'candidatura fora do funil, vinda do e-mail de confirmacao do LinkedIn em {day}'}
    remember()
    print(f'== APLICADAS (e-mail de confirmacao do LinkedIn; vaga nao estava no funil, registrei como nova)\n-- {label} · aplicou em {day}' + ('' if c['title'] else ' · sem titulo no e-mail: conferir com o usuario'))

def gmail_cursor(s, mark=None):
    """Prints the cursor (epoch) and the two queries ready for the connector; --gmail mark <epoch> advances the cursor."""
    st = s.setdefault('gmail', {'cursor': None, 'seen': []})
    now = datetime.now(timezone.utc)
    if mark:
        st['cursor'] = int(mark); print(f'gmail: cursor = {st["cursor"]}'); return
    cur = st.get('cursor') or int((now - timedelta(hours=24)).timestamp())
    print(f'cursor={cur}')
    print(f'alertas: {GMAIL_SENDERS} -subject:"your application was sent to" -subject:"sua candidatura foi enviada para" after:{cur}')
    print(f'humano:  newer_than:2d after:{cur} {GMAIL_HUMAN}')
    print(f'confirmacoes: {GMAIL_CONFIRM} after:{cur}   # e-mail "your application was sent to X": get_message em texto puro (com From, Subject, Date) e python3 scout.py --gmail applied < arquivo')
    an = (s.get('jobs') or {}).get('tracked') or {}
    emps = sorted({re.sub(r'[,.]? (inc|ltda|s\.?a|llc|ltd|corp|co)\.?$', '', v.get('company', ''), flags=re.I).strip()
                   for v in an.values() if v.get('status') in IN_PROGRESS and v.get('company')})
    if emps:
        names = ' OR '.join(f'"{e}"' for e in emps[:15])
        print(f'respostas: after:{cur} -from:linkedin.com -category:promotions ({names})   '
              '# resposta de empresa em que voce aplicou: convite de entrevista -> status=entrevista; retorno -> respondeu; recusa -> perdido')
    print(f'marcar depois: python3 scout.py --gmail mark {int(now.timestamp())}')

def gmail_triage(s, subjects):
    """For each subject 'X is hiring a Y' / 'X is hiring for a Y role': gatekeeper + already seen (company+title). Marks them seen."""
    st = s.setdefault('gmail', {'cursor': None, 'seen': []})
    seen = set(st.get('seen') or [])
    an = (s.get('jobs') or {}).get('tracked') or {}
    known = {(v.get('company', '').lower(), v.get('title', '').lower()) for v in an.values()}
    for a in subjects:
        m = re.match(r'(.+?) is hiring (?:a |an |for a )?(.+?)(?: role)?$', a.strip(), re.I)
        if not m: print(f'?? {a}'); continue
        emp, tit = m.group(1).strip(), m.group(2).strip()
        key = f'{emp.lower()}|{tit.lower()}'
        if key in seen or (emp.lower(), tit.lower()) in known: print(f'-- ja visto: {tit} — {emp}'); continue
        ok = lj.title_ok(tit)
        seen.add(key)
        print(f'{"OK" if ok else "fora"}: {tit} — {emp}' + ('' if ok else ' (porteiro)'))
    st['seen'] = list(seen)[-2000:]

def gmail_body(s, text):
    """Parses the body (PLAIN_TEXT) of a LinkedIn alert: blocks 'Title\nCompany\nLocation\n...View job: .../jobs/view/<id>/'.
    Dedup against jobs.seen + gmail.seen + jobs.tracked; gatekeeper; public detail (format/level/score) for the ones that pass."""
    st = s.setdefault('gmail', {'cursor': None, 'seen': []})
    seen_g = set(st.get('seen') or []); seen_j = set((s.get('jobs') or {}).get('seen') or [])
    an = (s.get('jobs') or {}).get('tracked') or {}
    blocks = re.split(r'\n-{10,}\n', text)
    jobs, out, repeated = [], [], 0
    for b in blocks:
        m = re.search(r'jobs/view/(\d+)', b)
        if not m: continue
        jid = m.group(1)
        lines = [l.strip() for l in b.strip().split('\n') if l.strip() and not l.lower().startswith(('view job', 'your job alert', 'this company', 'fast growing', 'actively'))]
        if len(lines) < 2: continue
        title, company, location = lines[0], lines[1], (lines[2] if len(lines) > 2 else '')
        if jid in seen_g or jid in seen_j or jid in an: repeated += 1; continue
        seen_g.add(jid); seen_j.add(jid)
        v = {'id': jid, 'title': title, 'company': company, 'location': location, 'url': f'https://www.linkedin.com/jobs/view/{jid}', 'search': 'gmail'}
        if not lj.title_ok(title): out.append(v); continue
        jobs.append(v)
    import time
    for i, v in enumerate(jobs):
        v['points'], v['tags'] = lj.score(v['title'])
        if i >= 8: continue
        try:
            d = lj.detail(v['id']); v.update(level=d['level'], snippet=d['description'][:400].replace('\n', ' '), work_format=lj.work_format(d['description']), tech_test=lj.tech_test(d['description']),
                                            posted=d['posted'], days=d['days'], n_applicants=d['n_applicants'])
            v['points'], v['tags'] = lj.score(v['title'] + '\n' + d['description']); v['level_excluded'] = d['level'] in lj.EXCLUDED_LEVELS
        except RuntimeError as e: v['error'] = str(e)
        time.sleep(1)
    saturated = [v for v in jobs if lj.is_saturated(v)]
    jobs = [v for v in jobs if not lj.is_saturated(v)]
    jobs, rep_sources = register_and_dedupe(s, jobs, 'gmail', False); repeated += rep_sources
    jobs.sort(key=lambda v: (-v['points'], v['title']))
    st['seen'] = list(seen_g)[-2000:]
    s.setdefault('jobs', {})['seen'] = list(seen_j)[-MAX_SEEN:]
    if jobs:
        print(f'== GMAIL (alertas do LinkedIn: {len(jobs)} novas que passam no porteiro; +{len(out)} fora, {repeated} repetidas)')
        for v in jobs:
            level = f' · {v["level"]}' if v.get('level') else ''
            fmt = (f' · formato: {"/".join(v["work_format"])}' if v.get('work_format') else '') + lj.test_tag(v)
            print(f'-- [{v["points"]}] {v["title"]} — {v["company"]} · {v["location"]}{level}{fmt}{lj.age(v)} · {", ".join(v["tags"]) or "-"}')
            if v.get('snippet'): print(f'   {v["snippet"][:220]}…')
            print(f'   {v["url"]}')
        if saturated: print(f'   (+{len(saturated)} saturada(s) fora — anuncio antigo com >100 candidatos)')
    else:
        print(f'gmail: nada novo que passe no porteiro (+{len(out)} fora, {len(saturated)} saturadas, {repeated} repetidas)')

# ---------------- Pipeline ----------------
# Stages in order; each --job with a new status pushes onto 'history' [{when, status}] so the pipeline has dates per stage.
# Stored in English (schema.STATUS); shown with schema.label() ('interessa', 'aplicado', ...).
STAGES = ['interested', 'applied', 'replied', 'interview', 'offer', 'hired', 'lost', 'ignored']
STALLED_DAYS = {'interested': 3, 'applied': 7, 'replied': 3, 'interview': 7, 'offer': 5}   # from here on the pipeline nags

def pipeline(s):
    an = (s.get('jobs') or {}).get('tracked') or {}
    now = datetime.now(timezone.utc)
    by = {e: [] for e in STAGES}
    for k, v in an.items():
        by.setdefault(v.get('status', '?'), []).append((k, v))
    active = {e: by[e] for e in STAGES if e not in ('ignored',) and by[e]}
    print('== FUNIL')
    print('   ' + ' · '.join(f'{schema.label(e)} {len(by[e])}' for e in STAGES if e != 'ignored') + f' · ignoradas {len(by["ignored"])}')
    applied = len(by['applied']) + len(by['replied']) + len(by['interview']) + len(by['offer']) + len(by['hired']) + len(by['lost'])
    resp = applied - len(by['applied'])
    if applied: print(f'   taxa de resposta: {resp}/{applied} ({100 * resp // applied}%) · fechadas {len(by["hired"])} · perdidas {len(by["lost"])}')
    for e in ('offer', 'interview', 'replied', 'applied', 'interested'):
        for k, v in sorted(by[e], key=lambda kv: kv[1].get('when') or ''):
            age = (now - dt(v.get('when'))).days if v.get('when') else 0
            stalled = f' · PARADA ha {age} d' if age >= STALLED_DAYS.get(e, 999) else f' · {age} d'
            print(f'-- {schema.label(e):10s} {v.get("title", "")} — {v.get("company", "")}{stalled} · {v.get("note", "")[:90]}')
            print(f'   {v.get("url") or f"https://www.linkedin.com/jobs/view/{k}"}')
    return any(active.values())

def week(s):
    an = (s.get('jobs') or {}).get('tracked') or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    print('== SEMANA (ultimos 7 dias)')
    moves = []
    for k, v in an.items():
        for h in v.get('history') or [{'when': v.get('when'), 'status': v.get('status')}]:
            if (h.get('when') or '') >= cutoff: moves.append((h['when'], h['status'], k, v))
    moves.sort()
    count = {}
    for q, st, k, v in moves: count[st] = count.get(st, 0) + 1
    print('   movimentos: ' + (' · '.join(f'{schema.label(e)} {n}' for e, n in sorted(count.items(), key=lambda x: STAGES.index(x[0]) if x[0] in STAGES else 99)) or 'nenhum'))
    for q, st, k, v in moves:
        if st == 'ignored': continue
        print(f'-- {q[:10]} {schema.label(st):10s} {v.get("title", "")} — {v.get("company", "")} · {v.get("note", "")[:80]}')
    ign = [v for q, st, k, v in moves if st == 'ignored']
    if ign:
        reasons = {}
        for v in ign:
            m = (v.get('note') or '?').split(':')[0].strip().lower(); reasons[m] = reasons.get(m, 0) + 1
        print('   ignoradas por motivo: ' + ' · '.join(f'{m} {n}' for m, n in sorted(reasons.items(), key=lambda x: -x[1])))
    print(); metrics(s, 30)
    return True

# ---------------- Origin, repeats across sources, funil.md and metrics ----------------
MAX_ORIGIN = 5000

def _norm(t):
    return re.sub(r'[^a-z0-9]+', ' ', (t or '').lower()).strip()

def job_key(v):
    """normalized company|title: the same job on LinkedIn and RemoteOK has different ids, but the same key."""
    e, t = _norm(v.get('company')), _norm(v.get('title'))
    return f'{e}|{t}' if e and t else ''

def source_of(jid, s=None):
    o = ((s or {}).get('origin') or {}).get(str(jid))
    if o: return o
    j = str(jid)
    return 'remoteok' if j.startswith('rok-') else 'web3career' if j.startswith('w3-') else 'wwr' if j.startswith('wwr-') else 'linkedin'

REPOSTED_DAYS = 7      # same company+title with a new id after 7+ days = the company reposted (hard to hire)

def _keys(s):
    """{key: {'id', 'since'}}; migrates the old list (keys only) without losing anything."""
    c = s.get('dedup_keys')
    if isinstance(c, list): c = s['dedup_keys'] = {k: {} for k in c}
    return s.setdefault('dedup_keys', {})

def register_and_dedupe(s, jobs, source, dry, keep=lambda v: False):
    """Saves the origin of each job shown and drops the ones that already appeared via another source (same company+title).
    `keep` holds the ones that must not disappear (TOP APPLICANT). Same key with a new id after REPOSTED_DAYS is not dropped:
    it comes back marked as reposted. Returns (list_without_repeats, n_repeats)."""
    ch = _keys(s)
    an = (s.get('jobs') or {}).get('tracked') or {}
    known = {}
    for k, v in an.items():
        kk = job_key(v)
        if kk:
            h = v.get('history') or []
            known.setdefault(kk, {'id': k, 'since': (h[0].get('when') if h else v.get('when'))})
    for k, e in ch.items(): known.setdefault(k, e)
    origin = s.setdefault('origin', {})
    now = datetime.now(timezone.utc)
    out, rep = [], 0
    for v in jobs:
        k = job_key(v); e = known.get(k) if k else None
        if e is not None and not keep(v) and str(v['id']) not in origin:
            before = dt(e.get('since')) if e.get('since') else None
            if before and e.get('id') and str(e['id']) != str(v['id']) and (now - before).days >= REPOSTED_DAYS:
                v['reposted'] = before.astimezone(BRT).strftime('%d/%m')
            else:
                rep += 1; continue
        out.append(v)
        if not dry:
            origin.setdefault(str(v['id']), v.get('source') or source)
            if v.get('search'): s.setdefault('origin_search', {}).setdefault(str(v['id']), v['search'])
            if k and k not in ch: ch[k] = {'id': str(v['id']), 'since': now.isoformat(timespec='seconds')}
            if k: known.setdefault(k, ch[k])
    if not dry:
        if len(ch) > MAX_ORIGIN: s['dedup_keys'] = dict(list(ch.items())[-MAX_ORIGIN:])
        if len(origin) > MAX_ORIGIN: s['origin'] = dict(list(origin.items())[-MAX_ORIGIN:])
        osr = s.get('origin_search') or {}
        if len(osr) > MAX_ORIGIN: s['origin_search'] = dict(list(osr.items())[-MAX_ORIGIN:])
    return out, rep

def fit(v):
    m = re.match(r'\s*(forte|m[eé]dio-forte|m[eé]dio-fraco|m[eé]dio|fraco|fora)\b', v.get('note') or '', re.I)
    return m.group(1).lower().replace('e', 'é', 1) if m and m.group(1).lower().startswith('me') else (m.group(1).lower() if m else '?')

def write_pipeline_md(s, path):
    """Pipeline in markdown, rewritten every tick: for whoever does not use Notion (or wants the pipeline in a file)."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    active = sorted(((k, v) for k, v in an.items() if v.get('status') in STAGES[:7]),
                    key=lambda kv: (STAGES.index(kv[1]['status']), kv[1].get('when') or ''))
    now = datetime.now(timezone.utc)
    count = {e: sum(1 for v in an.values() if v.get('status') == e) for e in STAGES}
    ln = [f'# Funil de vagas', '', f'Atualizado em {now.astimezone(BRT).strftime("%d/%m/%Y %H:%M")} pelo job-scout (não editar: é reescrito a cada tick).', '',
          ' · '.join(f'{schema.label(e)} {count[e]}' for e in STAGES[:7]) + f' · ignoradas {count["ignored"]}', '',
          '| Status | Vaga | Encaixe | Formato | Faixa | Match | Processo seletivo | Fonte | Há | Link | CV |', '|---|---|---|---|---|---|---|---|---|---|---|']
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    for k, v in active:
        age = (now - dt(v['when'])).days if v.get('when') else 0
        url = v.get('url') or (f'https://www.linkedin.com/jobs/view/{k}' if str(k).isdigit() else '')
        cel = lambda x: str(x or '-').replace('|', '/').replace('\n', ' ')
        ln.append(f'| {schema.label(v["status"])} | {cel(v.get("title"))} ({cel(v.get("company"))}) | {fit(v)} | {cel(v.get("work_format") and "/".join(v["work_format"]) if isinstance(v.get("work_format"), list) else v.get("work_format"))} '
                  f'| {cel(v.get("pay_range"))} | {cel((v.get("match") or "").split(" (")[0])} | {_process_cell(v, rep)} | {source_of(k, s)} | {age} d | {"[abrir](" + url + ")" if url else "-"} | {"[doc](" + v["cv_gdoc"] + ")" if v.get("cv_gdoc") else "-"} |')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fileio.write(path, '\n'.join(ln) + '\n')

def metrics(s, days=None):
    """Per source and per fit: how many the agent highlighted, how many became applications and how many got a reply.
    Shows which source is just noise and whether the agent's 'forte' is right."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ''
    APPLIED = {'applied', 'replied', 'interview', 'offer', 'hired', 'lost'}
    REPLIED = {'replied', 'interview', 'offer', 'hired'}
    def hist(v): return {h.get('status') for h in (v.get('history') or [])} | {v.get('status')}
    by_s, by_f = {}, {}
    for k, v in an.items():
        if cutoff and (v.get('when') or '') < cutoff: continue
        h = hist(v); hl = bool(h - {'ignored'}) or fit(v) in ('forte', 'médio-forte')
        for tab, key in ((by_s, source_of(k, s)), (by_f, fit(v))):
            t = tab.setdefault(key, [0, 0, 0, 0])
            t[0] += 1; t[1] += hl; t[2] += bool(h & APPLIED); t[3] += bool(h & REPLIED)
    print('== METRICAS' + (f' (ultimos {days} dias)' if days else ' (desde o inicio)'))
    for name, tab in (('por fonte', by_s), ('por encaixe', by_f)):
        print(f'   {name}: julgadas · destacadas · candidaturas · respostas')
        for c, (n, d, a, r) in sorted(tab.items(), key=lambda x: -x[1][1]):
            print(f'   -- {c:14s} {n:4d} · {d:3d} ({100 * d // n if n else 0}%) · {a:3d} · {r:3d}' + (f' ({100 * r // a}% das candidaturas)' if a else ''))
    return True

# ---------------- KPIs (the six the user chose on 24/09/2026) ----------------
EXPIRE_DAYS = 5                                   # medium-or-weaker job idle this long leaves "interested" by itself
EXPIRE_FITS = ('médio', 'médio-fraco', 'fraco', 'fora', '?')
STRONG = ('forte', 'médio-forte')

def _first(v, status=None):
    for h in v.get('history') or []:
        if status is None or h.get('status') == status: return dt(h.get('when'))
    return None if status else dt(v.get('when'))

def expire_backlog(s, now):
    """Medium-or-weaker jobs in 'interested' with no movement for EXPIRE_DAYS leave the pipeline (TOP APPLICANT stays).
    Keeps the backlog a list of actions, not an archive. Returns the ones that left."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    out = []
    for k, v in an.items():
        if v.get('status') != 'interested' or v.get('top') or fit(v) not in EXPIRE_FITS or not v.get('when'): continue
        idle = (now - dt(v['when'])).days
        if idle < EXPIRE_DAYS: continue
        v['status'] = 'ignored'; v.setdefault('history', []).append({'when': now.isoformat(), 'status': 'ignored'})
        v['note'] = f'expirada: {fit(v)} parada há {idle} dias em interessa (antes: {(v.get("note") or "")[:200]})'
        v['when'] = now.isoformat(); out.append((k, v))
    return out

CORRECTION_KINDS = [('cv', re.compile(r'^\s*(cv|summary|resumo|curr[ií]culo|evid[eê]ncia|captura|formul[aá]rio|mensagem|rascunho|pacote)\b', re.I)),
                    ('regra', re.compile(r'^\s*regra\b|prova t[eé]cnica|r[eé]gua|aceit[aá]vel|n[aã]o (faz|aceita|tem)|sempre|nunca|regulated', re.I))]

def correction_kind(what):
    """'julgamento' (the agent read a job wrong), 'cv' (CV and texts) or 'regra' (a new rule of the user's). The prefix
    'julgamento:', 'cv:' or 'regra:' in the correction wins; otherwise a guess from the words."""
    m = re.match(r'\s*(julgamento|cv|regra)\s*:', what or '', re.I)
    if m: return m.group(1).lower()
    return next((k for k, rx in CORRECTION_KINDS if rx.search(what or '')), 'julgamento')

def kpis(s, now, days=7):
    """Response rate, time to apply, accuracy of 'forte', user corrections, strong jobs per source/search, backlog, health."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    since = now - timedelta(days=days)
    APPLIED = {'applied', 'replied', 'interview', 'offer', 'hired', 'lost'}
    REPLIED = {'replied', 'interview', 'offer', 'hired'}
    hs = {k: {h.get('status') for h in (v.get('history') or [])} | {v.get('status')} for k, v in an.items()}
    ap = [k for k in an if hs[k] & APPLIED]; rp = [k for k in ap if hs[k] & REPLIED]
    ripe = [k for k in ap if (_first(an[k], 'applied') or now) <= now - timedelta(days=7)]   # 7+ days since applying
    rp_ripe = [k for k in ripe if k in rp]
    by_fit = {}
    for k in ripe: t = by_fit.setdefault(fit(an[k]), [0, 0]); t[0] += 1; t[1] += k in rp
    reply_days = sorted((min(filter(None, (_first(an[k], x) for x in REPLIED))) - _first(an[k], 'applied')).days for k in rp
                        if _first(an[k], 'applied') and any(_first(an[k], x) for x in REPLIED))
    week_ap = [k for k in ap if (_first(an[k], 'applied') or since) > since]
    week_iv = [k for k in an if (_first(an[k], 'interview') or since) > since]
    target = (criteria.load().get('kpi_targets') or {}).get('applications_week')
    waits = sorted((_first(an[k], 'applied') - _first(an[k])).total_seconds() / 3600 for k in ap
                   if fit(an[k]) in STRONG and _first(an[k], 'applied') and _first(an[k]))
    strong = [k for k, v in an.items() if fit(v) in STRONG]
    st_ap = sum(1 for k in strong if hs[k] & APPLIED); st_pend = sum(1 for k in strong if an[k].get('status') == 'interested')
    st_drop = len(strong) - st_ap - st_pend
    corrected = {str(c.get('job')) for c in s.get('corrections') or []}
    why_drop = {}
    for k in strong:
        v = an[k]
        if hs[k] & APPLIED or v.get('status') == 'interested': continue
        r = ('regra do perfil' if '(regra do perfil:' in (v.get('note') or '') else 'vaga fechou' if v.get('closure') else 'expirou parada' if (v.get('note') or '').lower().startswith('expirada')
             else 'você corrigiu' if str(k) in corrected else 'regra nova ou outro motivo')
        why_drop[r] = why_drop.get(r, 0) + 1
    corr = [c for c in s.get('corrections') or [] if dt(c['when']) >= since]
    corr_kind = {}
    for c in corr: corr_kind[correction_kind(c.get('what'))] = corr_kind.get(correction_kind(c.get('what')), 0) + 1
    new_strong = [k for k in strong if (_first(an[k]) or now) >= since]
    osr = s.get('origin_search') or {}
    by_src, by_search = {}, {}
    for k in new_strong:
        by_src[source_of(k, s)] = by_src.get(source_of(k, s), 0) + 1
        q = osr.get(str(k))
        if q: by_search[q] = by_search.get(q, 0) + 1
    backlog = [v for v in an.values() if v.get('status') == 'interested']
    bl_fit = {}
    for v in backlog: bl_fit[fit(v)] = bl_fit.get(fit(v), 0) + 1
    ticks, bad = 0, 0
    try:
        for ln in open(os.path.join(paths.RUNNER_DIR, 'metricas.tsv'), encoding='utf-8'):
            p = ln.rstrip('\n').split('\t')
            # heavy ticks only: a light wake ("planou") and a cycle exit ("ciclo", PLN0144) are not ticks
            if len(p) >= 2 and p[1].lstrip('-').isdigit() and p[0] >= since.astimezone(BRT).strftime('%Y-%m-%dT%H:%M'): ticks += 1; bad += p[1] != '0'
    except FileNotFoundError: pass
    med = f'{waits[len(waits) // 2]:.0f} h (mediana de {len(waits)})' if waits else 'sem dados'
    rows = [('1. Resposta às candidaturas com 7+ dias', f'{len(rp_ripe)} de {len(ripe)}' + (f' ({100 * len(rp_ripe) // len(ripe)}%)' if ripe else '')
             + ' · por encaixe: ' + (', '.join(f'{f} {r}/{n}' for f, (n, r) in sorted(by_fit.items())) or '-')
             + f' · ainda recentes: {len(ap) - len(ripe)}' + (f' · resposta em {reply_days[len(reply_days) // 2]} d (mediana de {len(reply_days)})' if reply_days else ''),
             'meta: forte responde mais que médio'),
            ('2. Tempo até aplicar (forte)', med, 'meta: < 24 h'),
            ('3. Acerto do forte (forte + médio-forte)', f'{len(strong)} vagas: {st_ap} aplicadas, {st_pend} esperando, {st_drop} descartadas'
             + (' (' + ', '.join(f'{r} {n}' for r, n in sorted(why_drop.items(), key=lambda x: -x[1])) + ')' if why_drop else ''),
             'calibrar só pelo "você corrigiu" ("regra do perfil" não é erro do juiz)'),
            (f'4. Correções suas ({days} d)', f'{len(corr)}' + (' · ' + ', '.join(f'{k} {n}' for k, n in sorted(corr_kind.items(), key=lambda x: -x[1])) if corr else '')
             + (f' · última: {corr[-1]["what"][:60]}' if corr else ''), 'meta: julgamento cair semana a semana'),
            (f'5. Fortes novas por fonte ({days} d)', ', '.join(f'{k} {n}' for k, n in sorted(by_src.items(), key=lambda x: -x[1])) or '0',
             'por busca: ' + (', '.join(f'"{q}" {n}' for q, n in sorted(by_search.items(), key=lambda x: -x[1])[:4]) or 'sem dados ainda')),
            ('6. Funil em interessa', f'{len(backlog)} · ' + ', '.join(f'{f} {n}' for f, n in sorted(bl_fit.items(), key=lambda x: -x[1])), 'meta: < 30'),
            (f'7. Candidaturas e entrevistas ({days} d)', f'{len(week_ap)} candidatura(s), {len(week_iv)} entrevista(s)',
             f'meta: {target} candidaturas por semana' if target else 'sem meta (config "kpi_targets": {"applications_week": N})'),
            (f'Saúde ({days} d)', f'{ticks} ticks, {bad} com erro', '')]
    print(f'== KPIs (últimos {days} dias)')
    print('| Indicador | Valor | Referência |'); print('|---|---|---|')
    for a, b, c in rows: print(f'| {a} | {_cell(b)} | {_cell(c)} |')
    return True

# ---------------- Recruiter re-engagement (weekly; the skill only drafts, the user sends) ----------------
REENGAGE_DAYS = 90
RECRUITER = re.compile(r'recruit|recrut|talent|hiring|head ?hunt|sourc|people|rh\b|human resources|staffing|acquisition', re.I)

def reengage(s, cache, me_urn, now):
    """Conversations with a recruiter whose last activity is >= REENGAGE_DAYS old and that were not suggested yet in this cycle."""
    st = s.setdefault('reengage', {})            # thread -> date of the last suggestion
    blocked = {k for k, v in (s.get('contacts') or {}).items() if v.get('status') == 'nao'}   # scam, spam, "do not contact"
    out = []
    for c in lm.conversations(me_urn):
        if not c['when'] or c['group'] or c['id'] in blocked: continue
        age = (now - dt(c['when'])).days
        if age < REENGAGE_DAYS: continue
        if set(c['others']) - set(cache): lm.participants(c['urn'], cache)
        hl = '; '.join((cache.get(u) or {}).get('headline', '') for u in c['others'])
        name = ', '.join(lm.name(u, cache) for u in c['others'])
        if not RECRUITER.search(hl) or name == 'LinkedIn Member' or name.startswith('1337'): continue
        last = st.get(c['id'])
        if last and (now - dt(last)).days < REENGAGE_DAYS: continue
        out.append({'id': c['id'], 'name': name, 'headline': hl[:100], 'days': age, 'url': c['url'], 'inmail': c['inmail']})
        st[c['id']] = now.isoformat()
    return out

def print_reengage(items):
    if not items: return False
    print(f'== REENGAJAR ({len(items)} recrutadores sem contato ha {REENGAGE_DAYS}+ dias; 1x por semana)')
    for r in items:
        print(f'-- {r["name"]} · {r["headline"]} · ultimo contato ha {r["days"]} d')
        print(f'   {r["url"]}')
    return True

# ---------------- Pipeline jobs that closed on LinkedIn (1x/day) ----------------
ACTIVE = ('interested', 'applied', 'replied', 'interview', 'offer')

def check_closed(s, now):
    """Checks on the public page (no cookie, 1 s between each) whether the pipeline jobs are still open.
    A closed/removed 'interested' becomes 'ignored' (leaves Notion). From the 'applied' stage on the status does NOT change
    (a closed job does not mean rejected): marks v['closure'] and Notion shows 'VAGA ENCERRADA NO LINKEDIN'.
    """
    import linkedin_jobs as lj
    an = (s.get('jobs') or {}).get('tracked') or {}
    now_iso = now.isoformat(timespec='seconds'); day = now.astimezone(BRT).strftime('%d/%m')
    lines, errors = [], 0
    for jid, v in list(an.items()):
        if v.get('status') not in ACTIVE or v.get('closure') or not str(jid).isdigit(): continue
        try:
            state = lj.job_state(jid)
        except Exception:
            errors += 1; continue
        finally:
            time.sleep(1)
        if state == 'aberta': continue
        label = f'{v.get("title", "")} — {v.get("company", "")}'
        if v['status'] == 'interested':
            v['status'] = 'ignored'; v.setdefault('history', []).append({'when': now_iso, 'status': 'ignored'})
            v['note'] = f'fora: vaga {state} no LinkedIn em {day} (antes: {v.get("note", "")[:150]})'; v['when'] = now_iso
            lines.append(f'-- {state}: {label} · saiu do funil e do Notion')
        else:
            v['closure'] = {'kind': state, 'when': day}
            lines.append(f'-- {state}: {label} · voce esta em "{schema.label(v["status"])}", status mantido; a vaga nao recebe mais candidaturas')
    if not lines and not errors: return ''
    txt = ['== ENCERRADAS (1x/dia: vagas do funil que fecharam ou sumiram no LinkedIn)'] + lines
    if errors: txt.append(f'   ({errors} nao consultada(s) por erro de rede/rate; tenta de novo amanha)')
    return '\n'.join(txt)

# ---------------- Applications you made directly on LinkedIn (1x/day, only with logged_in_sources) ----------------
MAX_CHECK_APPLIED = 30      # logged-in calls per run (2 s between each); the rest is left for the next day

def check_applied(s, now, limit=MAX_CHECK_APPLIED):
    """Jobs in 'interested' you already applied to via LinkedIn (applyingInfo.applied): move to 'applied' with the real
    date and the CV file you attached. Checks the oldest first and stores the date of the last check."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    queue = sorted(((k, v) for k, v in an.items() if v.get('status') == 'interested' and str(k).isdigit()),
                   key=lambda kv: kv[1].get('applied_checked') or '')[:limit]
    lines, today = [], now.isoformat(timespec='seconds')
    for jid, v in queue:
        c = lj.logged_in_application(jid); time.sleep(2)
        if c is None: return ''
        v['applied_checked'] = today
        if not c['has_applied']: continue
        v['status'] = 'applied'; v['when'] = c['when'] or today
        v.setdefault('history', []).append({'when': c['when'] or today, 'status': 'applied'})
        if c['cv']: v['cv_sent'] = c['cv']
        day = dt(c['when']).astimezone(BRT).strftime('%d/%m') if c['when'] else '?'
        v['note'] = (v.get('note') or '') + f' | aplicou pelo LinkedIn em {day}' + (f' com {c["cv"]}' if c['cv'] else '')
        lines.append(f'-- {v.get("title", "")} — {v.get("company", "")} · aplicou em {day}' + (f' · CV: {c["cv"]}' if c['cv'] else '') + ' · movida para aplicado')
    if not lines: return ''
    return '\n'.join(['== APLICADAS (voce aplicou direto no LinkedIn; registrei no funil)'] + lines)

# ---------------- Application progress (1x/day, with logged_in_sources) ----------------
IN_PROGRESS = ('applied', 'replied', 'interview', 'offer')
ACTIVITY = {'VIEW': 'candidatura visualizada', 'VIEWED': 'candidatura visualizada', 'RESUME_DOWNLOAD': 'CV baixado',
            'RESUME_DOWNLOADED': 'CV baixado', 'MESSAGE': 'mensagem do recrutador', 'REJECT': 'candidatura recusada'}

def track_applications(s, now, limit=MAX_CHECK_APPLIED):
    """What LinkedIn shows after you applied (viewed, CV downloaded, job closed): only what is new since the
    last check. Does not change the status by itself (viewing is not replying); the model proposes the next step."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    queue = [(k, v) for k, v in an.items() if v.get('status') in IN_PROGRESS and str(k).isdigit()][:limit]
    lines = []
    for jid, v in queue:
        c = lj.logged_in_application(jid); time.sleep(2)
        if c is None: return ''
        seen = set(v.get('activities_seen') or [])
        new = [a for a in c['activities'] if a['kind'] != 'APPLY' and f'{a["kind"]}|{a["text"]}' not in seen]
        label = f'{v.get("title", "")} — {v.get("company", "")}'
        for a in new:
            lines.append(f'-- {label}: {ACTIVITY.get(a["kind"], a["kind"].lower())} ({a["text"]})')
            seen.add(f'{a["kind"]}|{a["text"]}')
        if c['closed'] and not v.get('closure'):
            v['closure'] = {'kind': 'encerrada', 'when': now.astimezone(BRT).strftime('%d/%m')}
            lines.append(f'-- {label}: a vaga parou de receber candidaturas (voce segue em "{schema.label(v["status"])}")')
        v['activities_seen'] = sorted(seen)
    if not lines: return ''
    return '\n'.join(['== CANDIDATURAS (andamento no LinkedIn)'] + lines)

# ---------------- Daily shortlist: "apply to these today" ----------------
FIT_WEIGHT = {'forte': 4, 'médio-forte': 3, 'médio': 2, 'médio-fraco': 1}
SHORTLIST_N = 3
IDLE_DAYS = 5            # in 'interested' for longer than this = goes into the batch "apply or discard?" question

def _since(v):
    h = v.get('history') or []
    return dt(h[0].get('when') if h else v.get('when'))

def shortlist_score(v, now):
    """Fit (higher weight), match (matrix or quick), pay range vs expectation, TOP APPLICANT, reputation risk and age.
    Returns (score, reasons)."""
    n, m = FIT_WEIGHT.get(fit(v), 0) * 10, []
    txt = (v.get('note') or '').lower() + ' ' + (v.get('pay_range') or '').lower()
    mt = re.match(r'(\d+)%', v.get('match') or '')
    if mt: n += int(mt.group(1)) / 10; m.append(f'match {mt.group(1)}%')
    elif v.get('quick_match'): n += v['quick_match'][0] / 20; m.append(f'match~ {v["quick_match"][0]}%')
    if 'acima' in txt: n += 3; m.append('paga acima da sua pretensão')
    elif 'na faixa' in txt: n += 1.5
    elif 'abaixo' in txt: n -= 2; m.append('paga abaixo')
    if 'top applicant' in txt or v.get('top'): n += 4; m.append('TOP APPLICANT')
    if 'risco' in txt or 'regulador' in txt: n -= 15; m.append('risco na empresa')   # a risky company does not get past with a good average
    elif 'intermedi' in txt: n -= 1
    if v.get('closure'): n -= 50
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    if not _has_reputation(v.get('company'), rep): m.append('sem checagem de reputação')
    d = _since(v); age = (now - d).days if d else 0     # days in the pipeline (since the 1st decision), not the posting's age
    n -= max(0, age - 2) * 0.7
    posting = v.get('days')                               # posting age on LinkedIn when the agent read the page
    if posting is not None and d:
        posting += (now - d).days
        if posting <= 1: m.append('anúncio novo')
        elif posting >= 14: m.append(f'anúncio de {posting} dias')
    elif d and d.astimezone(BRT).date() == now.astimezone(BRT).date(): m.append('entrou no funil hoje')
    return n, m

FLAGS = ('TOP APPLICANT', 'anúncio novo', 'entrou no funil hoje')

def _cell(t):
    return str(t or '').replace('|', '/').replace('\n', ' ').strip()

def _pay_cell(v):
    """'est. US$ 60-110 mil/ano (~R$ 27-50 mil/mês), acima da sua referência' -> 'est. US$ 60-110 mil/ano (~R$ 27-50 mil/mês)'."""
    p = re.sub(r'\s*\((?:acima|bem acima|na faixa|abaixo|dos seus)[^)]*\)', '', v.get('pay_range') or '')
    p = re.split(r',\s*(?:acima|bem acima|na faixa|abaixo)', p)[0].strip()
    return p or '?'

def _match_cell(v):
    mt = re.match(r'(\d+)%', v.get('match') or '')
    if mt: return f'{mt.group(1)}% (matriz)'
    qm = v.get('quick_match')
    if not qm: return '?'
    return f'{qm[0]}%' + (f", falta {', '.join(qm[1][:3])}" if qm[1] else '')

def _rep_cell(v, rep):
    c = v.get('company') or ''
    if NO_NAME.search(c): return 'empresa não identificada'
    e = next((x for k, x in rep.items() if k.lower() in c.lower() or (c and c.lower() in k.lower())), None)
    if not e: return 'não checada'
    g = re.split(r'[;,]\s*(?=\D)', str(e.get('glassdoor') or ''), maxsplit=1)[0]
    g = re.sub(r'\s*\((?![\d.,\s]+(?:reviews?|avalia\w*)?\))[^)]*\)?', '', g)       # keeps "(2.110)", drops "(positiva: ...)"
    return _cell(g)[:40] or '?'

WORK = re.compile(r'\b(remoto|remote|PJ|contractor|contrato|CLT|h[íi]brido|presencial)\b', re.I)
WORK_LABEL = {'remote': 'remoto', 'contrato': 'contractor', 'hibrido': 'híbrido'}

def _rep_entry(v, rep):
    c = (v.get('company') or '').lower()
    return next((x for k, x in rep.items() if k.lower() in c or (c and c.lower() in k.lower())), None) if c else None

def _process_cell(v, rep):
    """Hiring process and technical test (26/09/2026, user: "whether there is a technical test and how the interview
    process goes matters for every job in the shortlist"): what the posting says (tech_test tag) and what the research
    saved in the reputation cache ('process', first sentence)."""
    parts = [f'anúncio: {"/".join(v["tech_test"])}'] if v.get('tech_test') else []
    e = _rep_entry(v, rep) or {}
    pr = str(e.get('process') or '').strip()
    if pr:
        head, _, rest = pr.partition('. ')
        parts.append(head.rstrip('.') + (f', {rest[:70].rstrip()}…' if rest else ''))
    return _cell('; '.join(parts)) or 'não pesquisado'

def _format_cell(v):
    """work_format read from the posting; when empty, the format words of the tags and of the note (the model's reading)."""
    seen = []
    for w in list(v.get('work_format') or []) + list(v.get('tags') or []) + WORK.findall(v.get('note') or ''):
        w = w.lower(); w = WORK_LABEL.get(w, w); w = 'PJ' if w == 'pj' else ('CLT' if w == 'clt' else w)
        if WORK.fullmatch(w) and w not in seen: seen.append(w)
    return '/'.join(seen) or '?'

def _why(v):
    """First clause of the note, without the fit word: the reason for the position, in one line."""
    n = re.sub(r'^\s*(forte|médio-forte|médio|médio-fraco|fraco|fora)\s*:\s*', '', v.get('note') or '', flags=re.I)
    n = re.split(r'(?<=[.;])\s', n.replace(' — ', ', ').replace('—', ','), maxsplit=1)[0].rstrip('.;')
    return n if len(n) <= 140 else n[:140].rsplit(' ', 1)[0] + '…'

def verify_open(s, now, ids):
    """Confirms on the public page that these jobs still take applications, at most once per job per day (26/09/2026:
    the shortlist recommended a job closed that morning). A closed one leaves `interested` like in check_closed.
    The same page gives the applicant count (01/10/2026): a job saturated by the config rule (linkedin_jobs.is_saturated)
    leaves `interested` too, unless it is TOP APPLICANT and saturation.top_applicant_exempt is on (default). Returns the ids that are still open (or could not be checked)."""
    import linkedin_jobs as lj
    an = (s.get('jobs') or {}).get('tracked') or {}
    today = now.astimezone(BRT).strftime('%Y-%m-%d'); day = now.astimezone(BRT).strftime('%d/%m')
    out = []
    for k in ids:
        v = an.get(k) or {}
        if not str(k).isdigit() or v.get('open_checked') == today: out.append(k); continue
        try: page = lj.job_page(k)
        except Exception: out.append(k); continue
        v['open_checked'] = today
        state, n = page.get('state'), page.get('n_applicants')
        if n is not None: v['n_applicants'] = n
        if state == 'aberta':
            days = page.get('days') if page.get('days') is not None else v.get('days')
            top = 'top applicant' in (v.get('note') or '').lower() or v.get('top')
            if n is not None and lj.saturated_out({'days': days, 'n_applicants': n}, top):
                _leave(v, now, f'fora: saturada, {n} candidatos em {day}')
                continue
            if lj.LOGGED_IN_SOURCES and v.get('remote_li') is None:   # the logged-in "remote allowed" flag, once per job
                try:
                    x = lj.logged_in_detail(k) or {}
                    if 'error' not in x and x.get('remote') is not None: v['remote_li'] = bool(x['remote'])
                except Exception: pass
            out.append(k); continue
        _leave(v, now, f'fora: vaga {state} no LinkedIn em {day}')
    return out

def _leave(v, now, why):
    """`interested` -> `ignored` with the reason in front of the previous note."""
    v['status'] = 'ignored'; v.setdefault('history', []).append({'when': now.isoformat(timespec='seconds'), 'status': 'ignored'})
    v['note'] = f'{why} (antes: {(v.get("note") or "")[:150]})'

def _crowded(v):
    """' ; N candidatos (concorrida)' when the job passed the saturation count (a TOP APPLICANT that stayed), else ''."""
    import linkedin_jobs as lj
    n = v.get('n_applicants')
    return f'; {n} candidatos (concorrida)' if isinstance(n, int) and n > lj.SATURATED_APPLICANTS else ''

def _skipped_by_process(v, rep, rx):
    """config "shortlist_skip_process" (regex on the hiring-process cell, e.g. "provável|\\(fora\\)"): jobs whose process
    matches stay in the pipeline but not in the shortlist, until the recruiter clarifies (26/09/2026, user's rule)."""
    return bool(rx) and bool(re.search(rx, _process_cell(v, rep), re.I))

def _shortlist_pool(s):
    an = (s.get('jobs') or {}).get('tracked') or {}
    rx = criteria.load().get('shortlist_skip_process') or ''
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    ok = {k: v for k, v in an.items() if v.get('status') == 'interested' and not v.get('closure')}
    # gate (26/09/2026): a job LinkedIn marks as not remote does not go to the shortlist until the model re-judges it
    skipped = [k for k, v in ok.items() if _skipped_by_process(v, rep, rx) or v.get('remote_li') is False]
    return {k: v for k, v in ok.items() if k not in skipped}, skipped

def shortlist_rank(s, now):
    """Ids of the jobs in `interested`, best first (the order of the "apply today" list)."""
    pool, _ = _shortlist_pool(s)
    cands = [(k, shortlist_score(v, now)[0]) for k, v in pool.items()]
    return [k for k, _ in sorted(cands, key=lambda x: -x[1])]

def shortlist(s, now, n=SHORTLIST_N):
    """The day's list as a table: job (with strong signals), format, pay, match, reputation, link; then one "why" line for
    the top 3. Ordered by shortlist_score (fit, pay, TOP APPLICANT, reputation risk, age), not by match."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    pool, skipped = _shortlist_pool(s)
    cands = [(k, v) + shortlist_score(v, now) for k, v in pool.items()]
    cands.sort(key=lambda x: -x[2])
    while True:                                        # the top n must still be open (checked once a day per job)
        ok = set(verify_open(s, now, [k for k, *_ in cands[:n]]))
        if all(k in ok for k, *_ in cands[:n]): break
        cands = [c for c in cands if c[0] in ok or c[0] not in {k for k, *_ in cands[:n]}]
    top = cands[:n]
    idle = [(k, v) for k, v, *_ in cands[n:] if _since(v) and (now - _since(v)).days > IDLE_DAYS]
    if not top: return ''
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    out = [f'== APLIQUE HOJE (lista curta: as {len(top)} melhores de {len(cands)} em interessa)', '',
           '| # | Vaga | Formato | Faixa | Match | Reputação | Processo seletivo | Link |', '|---|---|---|---|---|---|---|---|']
    for i, (k, v, score, why) in enumerate(top, 1):
        flags = [w for w in why if w in FLAGS] + ([f'REPUBLICADA {v["reposted"]}'] if v.get('reposted') else [])
        job = f'{_cell(v.get("company"))}, {_cell(v.get("title"))} ({fit(v)})' + (' · ' + ' · '.join(flags) if flags else '')
        fmt = _format_cell(v)
        url = v.get('url') or (f'https://www.linkedin.com/jobs/view/{k}' if str(k).isdigit() else '-')
        out.append(f'| {i} | {job} | {fmt} | {_cell(_pay_cell(v))} | {_cell(_match_cell(v))} | {_rep_cell(v, rep)} | {_process_cell(v, rep)} | {url} |')
    out.append('')
    for i, (k, v, score, why) in enumerate(top[:3], 1):
        out.append(f'{i}. {_cell(v.get("company"))}: {_why(v)}{_crowded(v)} (candidatura: `application.py {k}`)')
    if skipped:
        out.append(f'   fora da lista (processo seletivo a esclarecer, ou marcada como não remota no LinkedIn): '
                   + '; '.join(f'{an[k].get("company", "")} ({an[k].get("title", "")[:40]})' + (' [não remota]' if an[k].get('remote_li') is False else '')
                               for k in skipped[:8]) + ('…' if len(skipped) > 8 else ''))
    if idle:
        out.append(f'   {len(idle)} parada(s) ha mais de {IDLE_DAYS} dias em interessa; responder em lote "aplica X, descarta o resto": '
                   + '; '.join(f'{v.get("company", "")} ({fit(v)})' for k, v in idle[:12]) + ('…' if len(idle) > 12 else ''))
    return '\n'.join(out)

def board(s, now, n=None):
    """Everything in one table (26/09/2026, user's request): applications in progress first (furthest stage first), then
    the day's shortlist, then the rest of `interested` best first. Same cells as the shortlist, plus the status."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    moving = sorted(((k, v) for k, v in an.items() if v.get('status') in ACTIVE[1:]),
                    key=lambda kv: (-STAGES.index(kv[1]['status']), -(_since(kv[1]) or now).timestamp()))
    rank = shortlist_rank(s, now)
    rows = [(k, an[k]['status'], an[k]) for k, _ in moving]
    rows += [(k, 'lista curta' if i < SHORTLIST_N else 'interessa', an[k]) for i, k in enumerate(rank)]
    total = len(rows)
    if n: rows = rows[:n]
    if not rows: return ''
    out = [f'== PAINEL ({len(moving)} em andamento, {len(rank)} em interessa' + (f'; mostrando {len(rows)} de {total}' if n and total > n else '') + ')', '',
           '| # | Status | Vaga | Formato | Faixa | Match | Reputação | Processo seletivo | Link |', '|---|---|---|---|---|---|---|---|---|']
    for i, (k, st, v) in enumerate(rows, 1):
        label = st if st in ('lista curta', 'interessa') else schema.label(st)
        since = _when_status(v, st) if st not in ('lista curta', 'interessa') else None
        label += f' ({since})' if since else ''
        url = v.get('url') or (f'https://www.linkedin.com/jobs/view/{k}' if str(k).isdigit() else '-')
        name = ', '.join(x for x in (_cell(v.get('company')), _cell(v.get('title'))) if x) or f'(sem nome, id {k})'
        out.append(f'| {i} | {label} | {name} ({fit(v)}) | {_format_cell(v)} | {_cell(_pay_cell(v))} '
                   f'| {_cell(_match_cell(v))} | {_rep_cell(v, rep)} | {_process_cell(v, rep)} | {url} |')
    return '\n'.join(out)

def _when_status(v, st):
    """'24/09' = when the job entered its current status (history)."""
    w = next((h.get('when') for h in reversed(v.get('history') or []) if h.get('status') == st), None)
    return dt(w).astimezone(BRT).strftime('%d/%m') if w else ''

# ---------------- Reputation missing from the highlights ----------------
REPUTATION = paths.REPUTATION
NO_NAME = re.compile(r'confidencial|confidential|stealth|sigilo', re.I)

def _has_reputation(company, rep):
    e = (company or '').lower()
    return any(k.lower() in e or (e and e in k.lower()) for k in rep)

def pending_reputation(s):
    """Forte/medio-forte job in the pipeline whose company is not in the reputation cache: nags ONCE per job, on the tick
    after the decision, so the model does not skip the check (research and save to reputation.json)."""
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    an = (s.get('jobs') or {}).get('tracked') or {}
    missing = [(k, v) for k, v in an.items() if v.get('status') in ACTIVE and fit(v) in ('forte', 'médio-forte')
               and not v.get('reputation_asked') and not NO_NAME.search(v.get('company') or '') and not _has_reputation(v.get('company'), rep)]
    if not missing: return ''
    for k, v in missing: v['reputation_asked'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return '\n'.join(['== REPUTACAO (destaques sem checagem: pesquisar Glassdoor e noticias, gravar em reputacao.json e acrescentar na nota)'] +
                     [f'-- {v.get("company", "")}: {v.get("title", "")} ({fit(v)}) · id {k}' for k, v in missing])

def pending_process(s, now, n=SHORTLIST_N + 3):
    """Jobs at the top of the shortlist, and applications in progress, whose hiring process was not researched (no 'process' in the reputation cache):
    nags ONCE per job, so the model looks it up (Glassdoor Interviews, reports) before the user decides to apply."""
    try: rep = json.load(open(REPUTATION, encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError): rep = {}
    an = (s.get('jobs') or {}).get('tracked') or {}
    ids = shortlist_rank(s, now)[:n] + [k for k, v in an.items() if v.get('status') in ACTIVE[1:]]   # + applications in progress
    missing = [(k, an[k]) for k in ids
               if not an[k].get('process_asked') and not NO_NAME.search(an[k].get('company') or '')
               and not (_rep_entry(an[k], rep) or {}).get('process')]
    if not missing: return ''
    for k, v in missing: v['process_asked'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return '\n'.join(['== PROCESSO SELETIVO (lista curta sem processo pesquisado: buscar etapas e se ha prova tecnica, gravar "process" em reputation.json)'] +
                     [f'-- {v.get("company", "")}: {v.get("title", "")} ({fit(v)}) · id {k}' + (f' · anuncio: {"/".join(v["tech_test"])}' if v.get('tech_test') else '') for k, v in missing])

def pending_rejections(s):
    """Applications that became `lost` without a post-mortem (26/09/2026, user's request: after every rejection, an analysis of
    what may have caused it, so the next application improves). Nags once per job; the model writes <Company>-recusa.md
    next to the package and turns what is general into a note in the profile."""
    an = (s.get('jobs') or {}).get('tracked') or {}
    missing = [(k, v) for k, v in an.items() if v.get('status') == 'lost' and not v.get('loss_asked')
               and any(h.get('status') == 'applied' for h in v.get('history') or [])]
    if not missing: return ''
    for k, v in missing: v['loss_asked'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    lines = ['== RECUSA (analisar: anúncio x CV enviado x respostas; o que pode ter motivado; o que muda nas próximas)']
    for k, v in missing:
        lines.append(f'-- {v.get("company", "")}: {v.get("title", "")} · id {k} · CV: {os.path.basename(v.get("cv_pdf") or v.get("cv_docx") or "?")}'
                     + (f' · o anúncio citava e o CV não: {", ".join(v["cv_gaps"])}' if v.get('cv_gaps') else ''))
    return '\n'.join(lines)

# ---------------- User hooks (config "tick_hooks") ----------------
def tick_hooks():
    """Commands of the user's own that ride on the tick: config "tick_hooks": [{"name": "...", "command": "...",
    "alert_when": "<regex on the output>", "timeout": 60}]. A hook whose output matches alert_when becomes a block
    (and so wakes the session); otherwise it stays silent. Examples: a publish reminder, a backup check."""
    import subprocess
    out = []
    for h in criteria.load().get('tick_hooks') or []:
        name = (h.get('name') or 'hook').upper()
        try:
            r = subprocess.run(h['command'], shell=True, capture_output=True, text=True, timeout=h.get('timeout', 60))
            txt = ((r.stdout or '') + (r.stderr or '')).strip()
            if re.search(h.get('alert_when') or r'\S', txt): out.append(f'== {name}\n{txt[:800]}')
        except Exception as e:
            out.append(f'== {name}: falhou ({type(e).__name__}: {str(e)[:120]})')
    return out

# ---------------- Interview: preparation ----------------
def interviews_without_prep(s):
    an = (s.get('jobs') or {}).get('tracked') or {}
    pend = [(k, v) for k, v in an.items() if v.get('status') == 'interview' and not v.get('prep')]
    if not pend: return ''
    return '\n'.join(['== ENTREVISTA (sem preparo ainda: `interview.py <id>` monta o roteiro)'] +
                     [f'-- {v.get("title", "")} — {v.get("company", "")} · id {k}' for k, v in pend])

# ---------------- Slow routines (daily / weekly), gated by date in the state ----------------
def routines(s, now):
    """Blocks that are not hourly: pipeline with stalled items (1x per business day, from 09h BRT) and weekly summary
    (1x per week, on the first Monday tick from 09h). Returns a list of ready texts."""
    import io, contextlib
    out = []
    d = now.astimezone(BRT)
    today = d.strftime('%Y-%m-%d')
    # closed jobs: every day, weekends included (26/09/2026: a shortlisted job closed on a Saturday morning and the list
    # kept recommending it; the check is public and cheap, one page per job in the pipeline)
    if d.hour >= 9 and s.get('routine_closed') != today:
        s['routine_closed'] = today
        txt = check_closed(s, now)
        if txt: out.append(txt)
    if d.weekday() < 5 and d.hour >= 9:
        if s.get('routine_pipeline') != today:
            an = (s.get('jobs') or {}).get('tracked') or {}
            stalled = [(k, v) for k, v in an.items() if v.get('status') in STALLED_DAYS and v.get('when')
                       and (now - dt(v['when'])).days >= STALLED_DAYS[v['status']]]
            s['routine_pipeline'] = today
            gone = expire_backlog(s, now)
            if gone:
                out.append(f'== FUNIL LIMPO: {len(gone)} vaga(s) média(s) ou abaixo parada(s) há {EXPIRE_DAYS}+ dias saíram de interessa (TOP APPLICANT fica)\n'
                           + '; '.join(f'{v.get("company", "")} ({v.get("title", "")[:40]})' for k, v in gone[:15]) + ('…' if len(gone) > 15 else ''))
            if stalled:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    print('== FUNIL: itens parados (1x/dia)')
                    for k, v in stalled:
                        print(f'-- {schema.label(v["status"]):10s} {v.get("title", "")} — {v.get("company", "")} · ha {(now - dt(v["when"])).days} d · {v.get("note", "")[:80]}')
                        print(f'   {v.get("url") or f"https://www.linkedin.com/jobs/view/{k}"}')
                out.append(buf.getvalue().rstrip('\n'))
        if lj.LOGGED_IN_SOURCES and s.get('routine_applied') != today and (s.get('messages') or {}).get('me'):
            s['routine_applied'] = today
            try:
                for fn in (check_applied, track_applications):
                    txt = fn(s, now)
                    if txt: out.append(txt)
            except SystemExit as e:
                out.append(f'APLICADAS: nao consegui ({e.code})')
        if s.get('routine_shortlist') != today:
            s['routine_shortlist'] = today
            txt = shortlist(s, now)
            if txt: out.append(txt)
        txt = pending_reputation(s)
        if txt: out.append(txt)
        txt = pending_process(s, now)
        if txt: out.append(txt)
        txt = pending_rejections(s)
        if txt: out.append(txt)
        txt = interviews_without_prep(s)
        if txt and s.get('routine_interview') != today: s['routine_interview'] = today; out.append(txt)
        iso_week = d.strftime('%G-W%V')
        if d.weekday() == 0 and s.get('routine_week') != iso_week:
            s['routine_week'] = iso_week
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                week(s); print(); pipeline(s); print(); kpis(s, now)
            out.append(buf.getvalue().rstrip('\n'))
        # companies (former clients, current ones, consultancies, ecosystem): 1x per business day, 1-2 public calls
        if s.get('routine_companies') != today:
            s['routine_companies'] = today
            st = s.setdefault('companies', {'ids': {}, 'seen': []})
            try:
                co.ids(st['ids'])
                cards = co.fetch(st['ids'], co.WINDOW_H, ignore=set(st.get('seen') or []))
                all_ids = [c['id'] for c in cards] + [i for c in cards for i in c.get('extra_ids', [])]
                st['seen'] = (list(st.get('seen') or []) + all_ids)[-MAX_SEEN:]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    if co.print_block(cards): out.append(buf.getvalue().rstrip('\n'))
            except Exception as e:
                out.append(f'EMPRESAS: nao consegui ({type(e).__name__}: {str(e)[:100]})')
        # re-engagement: Tuesday (so it does not stack with Monday's summary); makes its OWN inbox list call (+1 Voyager/week)
        if d.weekday() == 1 and s.get('routine_reengage') != iso_week and (s.get('messages') or {}).get('me'):
            s['routine_reengage'] = iso_week
            try:
                cache = lm._names()
                items = reengage(s, cache, s['messages']['me']['urn'], now); lm._save_names(cache)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    if print_reengage(items): out.append(buf.getvalue().rstrip('\n'))
            except SystemExit as e:
                out.append(f'REENGAJAR: nao consegui ({e.code})')
            except Exception as e:
                out.append(f'REENGAJAR: nao consegui ({type(e).__name__}: {str(e)[:100]})')
    return out

# ---------------- main ----------------
# English names on the command line; the Portuguese values are still accepted (0.6.x and earlier)
SOURCE_ALIASES = {'messages': 'mensagens', 'jobs': 'vagas', 'recommended': 'recomendadas', 'boards': 'mercado'}
LESSONS_ALIASES = {'proposals': 'propostas', 'all': 'tudo', 'rules': 'regras'}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true'); ap.add_argument('--json', action='store_true')
    ap.add_argument('--only', '--so', dest='so', choices=list(SOURCE_ALIASES) + list(SOURCE_ALIASES.values()))
    ap.add_argument('--since', help='fixed window (30m, 4h, 2d) instead of the cursors, on both sources; implies --dry')
    ap.add_argument('--pending', '--pendentes', dest='pendentes', action='store_true'); ap.add_argument('--resolve', '--resolver', dest='resolver', metavar='pN')
    ap.add_argument('--pending-set', '--pendente', dest='pendente', nargs='+', metavar=('pN', 'k=v'))
    ap.add_argument('--job', '--vaga', dest='vaga', nargs='+', metavar=('ID', 'k=v'), help='annotates a job: status=interested|ignored|applied|..., note="..." (Portuguese keys and status names also accepted)')
    ap.add_argument('--jobs', '--vagas', dest='vagas', action='store_true', help='lists the annotated jobs')
    ap.add_argument('--pipeline', '--funil', dest='funil', action='store_true'); ap.add_argument('--week', '--semana', dest='semana', action='store_true')
    ap.add_argument('--metrics', '--metricas', dest='metricas', nargs='?', const=0, type=int, metavar='DIAS', help='highlighted/applications/replies per source and per fit')
    ap.add_argument('--gmail', nargs='*', metavar='ARG', help='cursor | mark <epoch> | triage "subject" ... | body < text | applied < email')
    ap.add_argument('--reengage', '--reengajar', dest='reengajar', action='store_true', help='lists recruiters with no contact for 90+ days (query only; does not mark the cycle)')
    ap.add_argument('--lessons', '--licoes', dest='licoes', nargs='*', metavar='ARG', help='--lessons: latest lessons; set "text": saves today\'s (state + shared LICOES.md); rules; proposals; all')
    ap.add_argument('--applied', '--aplicadas', dest='aplicadas', nargs='?', const=MAX_CHECK_APPLIED, type=int, metavar='N', help='checks now (logged-in API) whether you already applied to the jobs in interested')
    ap.add_argument('--shortlist', nargs='?', const=SHORTLIST_N, type=int, metavar='N', help='the N best jobs in interested to apply to today')
    ap.add_argument('--daily-tasks', dest='daily_tasks', action='store_true', help='the job hunt section of the daily Notion page as it would be published (config daily_tasks; nothing is written)')
    ap.add_argument('--board', '--painel', dest='board', nargs='?', const=0, type=int, metavar='N', help='every active job in one table: applications, shortlist, then the rest of interested (N = only the first N rows)')
    ap.add_argument('--track', '--acompanhar', dest='acompanhar', action='store_true', help='progress of your applications on LinkedIn (viewed, CV downloaded)')
    ap.add_argument('--observed-pay', '--faixa-real', dest='faixa_real', nargs=4, metavar=('EMPRESA', 'CARGO', 'VALOR', 'FONTE'),
                    help='real amount a recruiter/offer gave (e.g. "Acme" "SRE Sr" "US$ 50/h" "recruiter 23/09"); goes into pay_ranges.observed of config.json')
    ap.add_argument('--contact', '--contato', dest='contato', nargs='+', metavar=('THREAD', 'k=v'), help='annotates a contact: status=nao|ok, note="golpe"')
    a = ap.parse_args()
    a.so = SOURCE_ALIASES.get(a.so, a.so)
    if a.licoes: a.licoes[0] = LESSONS_ALIASES.get(a.licoes[0], a.licoes[0])   # the shared lessons lib (watch_core) only knows the Portuguese names
    if a.funil or a.semana:
        s = load_state(); (pipeline if a.funil else week)(s); return
    if a.metricas is not None:
        st = load_state(); metrics(st, a.metricas or None); print(); kpis(st, datetime.now(timezone.utc), a.metricas or 7); return
    if a.gmail is not None:
        s = load_state(); mode = a.gmail[0] if a.gmail else 'cursor'
        if mode in ('mark', 'marca'): gmail_cursor(s, mark=a.gmail[1])
        elif mode in ('triage', 'triagem'): gmail_triage(s, a.gmail[1:])
        elif mode in ('body', 'corpo'): gmail_body(s, sys.stdin.read())
        elif mode in ('applied', 'aplicada'): gmail_applied(s, sys.stdin.read())
        else: gmail_cursor(s)
        save_state(s); return
    if a.contato:
        s = load_state(); c = s.setdefault('contacts', {}).setdefault(a.contato[0], {})
        for kv in a.contato[1:]:
            k, _, v = kv.partition('='); c[schema.CONTACT.get(k, k)] = v
        c['when'] = datetime.now(timezone.utc).isoformat(); save_state(s); print(f'{a.contato[0]}: {c}'); return
    if a.board is not None:
        print(board(load_state(), datetime.now(timezone.utc), a.board) or 'nenhuma vaga ativa'); return
    if a.shortlist is not None:
        s = load_state(); txt = shortlist(s, datetime.now(timezone.utc), a.shortlist); save_state(s)   # closed ones leave
        print(txt or 'nenhuma vaga em interessa'); return
    if a.daily_tasks:
        print(daily_tasks.show(load_state(), shortlist_rank)); return
    if a.acompanhar:
        s = load_state(); txt = track_applications(s, datetime.now(timezone.utc)); save_state(s)
        print(txt or 'nada novo nas suas candidaturas'); return
    if a.faixa_real:
        c = json.load(open(paths.CONFIG, encoding='utf-8')) if os.path.exists(paths.CONFIG) else {}
        emp, role, amount, source = a.faixa_real
        c.setdefault('pay_ranges', {}).setdefault('observed', []).append({'company': emp, 'role': role, 'value': amount, 'source': source,
                                                                         'date': datetime.now(BRT).strftime('%d/%m/%Y')})
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        fileio.write_json(paths.CONFIG, c, indent=1, ensure_ascii=False)
        os.makedirs(paths.HISTORY_DIR, exist_ok=True)
        with open(paths.CRITERIA_LOG, 'a', encoding='utf-8') as f: f.write(f'{datetime.now().strftime("%Y%m%d-%H%M%S")} faixa observada: {emp} · {role} · {amount} ({source})\n')
        print(f'faixa observada gravada no config.json: {emp} · {role} · {amount} ({source}); {len(c["pay_ranges"]["observed"])} no total'); return
    if a.aplicadas is not None:
        s = load_state(); txt = check_applied(s, datetime.now(timezone.utc), a.aplicadas); save_state(s)
        print(txt or 'nenhuma vaga em interessa com candidatura sua no LinkedIn'); return
    if a.reengajar:
        s = load_state(); cache = lm._names(); me = lm.me()
        items = reengage({'reengage': {}, 'contacts': s.get('contacts') or {}}, cache, me['urn'], datetime.now(timezone.utc)); lm._save_names(cache)
        if not print_reengage(items): print('nenhum recrutador com 90+ dias sem contato')
        return
    if a.vagas or a.vaga:
        s = load_state(); an = s.setdefault('jobs', {}).setdefault('tracked', {})
        if a.vagas:
            if not an: print('nenhuma vaga anotada'); return
            for k, v in sorted(an.items(), key=lambda kv: (STAGES.index(kv[1].get('status')) if kv[1].get('status') in STAGES else 99, kv[1].get('when') or '')):
                print(f'-- {k} · {schema.label(v.get("status", "?")):10s} · {v.get("title", "")} — {v.get("company", "")} · {v.get("note", "")[:80]} · {v.get("url") or f"https://www.linkedin.com/jobs/view/{k}"}')
            return
        # batch: --job ID k=v k=v + ID2 k=v ... ('+' separator; one subprocess per tick, not one per job)
        groups, current = [], []
        for tok in a.vaga:
            if tok == '+': groups.append(current); current = []
            else: current.append(tok)
        groups.append(current)
        now_iso = datetime.now(timezone.utc).isoformat()
        for g in groups:
            if not g: continue
            v = an.setdefault(g[0], {})
            v.setdefault('source', source_of(g[0], s))
            kv = {}
            for tok in g[1:]:
                k, _, val = tok.partition('='); kv[k] = val
            kv = schema.job(kv)            # Portuguese keys (nota=, faixa=, ...) and status names (interessa, ...) still accepted
            corr = kv.pop('correction', None) or kv.pop('correcao', None)
            if corr: s.setdefault('corrections', []).append({'when': now_iso, 'job': g[0], 'what': corr})
            for k in ('apply_until', 'interview_at'):      # the Prazo in Planou: "02/10", "sexta", "2026-10-02" -> the day
                if k in kv and kv[k] not in ('', '-'):
                    d = deadlines.valid(kv[k][:10]) or deadlines.resolve(kv[k], datetime.now(timezone.utc), marker=False)
                    if not d: sys.exit(f'{k}: data (AAAA-MM-DD, dd/mm ou dia da semana), ou - para apagar')
                    kv[k] = d
                elif k in kv:
                    kv[k] = None
            for k, val in kv.items():
                if k == 'status':
                    if val not in STAGES: sys.exit(f'status: {"|".join(schema.label(e) for e in STAGES)}')
                    if val != v.get('status'): v.setdefault('history', []).append({'when': now_iso, 'status': val})
                v[k] = val
            if (not v.get('title') or not v.get('company') or 'work_format' not in v) and v.get('status') != 'ignored':   # detail only for what is worth it (1 request)
                try:
                    d = lj.detail(g[0]); v['title'] = v.get('title') or d['title']; v['company'] = d['company']
                    v['work_format'] = lj.work_format(d['description']); v['tech_test'] = lj.tech_test(d['description']); v['location'] = v.get('location') or d.get('location', ''); v['level'] = d['level']
                    if d.get('recruiter'): v['recruiter'] = d['recruiter']
                    if d.get('days') is not None: v['days'], v['posted'] = d['days'], d.get('posted', '')
                    v['quick_match'] = lj.quick_match(v['title'] + '\n' + d['description'])
                    au = deadlines.resolve(d['description'], datetime.now(timezone.utc), markers=deadlines.APPLY_MARKERS)
                    if au and not v.get('apply_until'): v['apply_until'] = au          # "applications close on ..." only
                except RuntimeError: pass
            v['when'] = now_iso
            print(f'{g[0]}: {schema.label(v.get("status"))} · {v.get("title", "")} — {v.get("company", "")} · {v.get("note", "")[:60]}')
        save_state(s); return
    if a.pendentes or a.resolver or a.pendente:
        s = load_state(); now = datetime.now(timezone.utc)
        if a.pendentes: list_pending(s); return
        key = a.resolver or a.pendente[0]
        p = next((p for p in pending_items(s) if p['key'] == key), None)
        if not p: sys.exit(f'pendencia {key} nao existe (ver --pending)')
        if a.resolver: resolve(p, now, 'manual')
        else:
            for kv in a.pendente[1:]:
                k, _, v = kv.partition('='); k = schema.PENDING.get(k, k)
                if k == 'status' and v not in ('ignorar', 'aberta'): sys.exit('status: ignorar|aberta')   # pending status, not a job status
                p[k] = v
        save_state(s); print(f'{p["key"]} [{p["source"]}] {p["who"]} — {p["subject"]}: {p["status"]}'); return
    if a.licoes is not None:
        s = load_state(); lessons.cli(a.licoes, s, save_state, AGENT_NAME); return
    dry = a.dry or bool(a.since)
    since = lm.parse_since(a.since) if a.since else None
    s = load_state()
    sources = [a.so] if a.so else (['mensagens', 'recomendadas'] if lj.LOGGED_IN_SOURCES else []) + ['vagas', 'mercado']
    res, broken = {}, []
    for f in sources:
        try:
            res[f] = {'mensagens': tick_messages, 'vagas': tick_jobs, 'recomendadas': tick_recommended, 'mercado': tick_boards}[f](s, since, dry)
        except SystemExit as e: broken.append((f, str(e.code)))
        except Exception as e: broken.append((f, f'{type(e).__name__}: {e}'))
        if (res.get(f) or {}).get('alarm'): broken.append((f, res[f]['alarm']))
    if not dry:                    # a broken source stays recorded until it runs clean (Planou: the Ferramentas tab)
        bk, now_b, db = s.setdefault('broken', {}), datetime.now(timezone.utc).isoformat(), dict(broken)
        for f in sources:
            if f in db: bk.setdefault(f, {'since': now_b})['error'] = db[f]
            else: bk.pop(f, None)
    # the same job via another source (company+title) is dropped; saves the origin of each one (metrics per source)
    if 'recomendadas' in res and not res['recomendadas'].get('baseline'):
        res['recomendadas']['new'], res['recomendadas']['repeated'] = register_and_dedupe(s, res['recomendadas']['new'], 'recomendadas', dry, keep=lambda v: v.get('top'))
    if 'vagas' in res and not res['vagas'].get('baseline'):
        res['vagas']['jobs'], res['vagas']['repeated'] = register_and_dedupe(s, res['vagas']['jobs'], 'linkedin', dry)
    if 'mercado' in res and not res['mercado'].get('baseline'):
        res['mercado']['jobs'], res['mercado']['repeated'] = register_and_dedupe(s, res['mercado']['jobs'], 'mercado', dry)
    remind, resolved, routine_blocks = [], [], []
    if not dry:
        now = datetime.now(timezone.utc)
        remind = due_reminders(s, now, [f for f in ('mensagens',) if f in res])
        for p in remind: p['reminders'] += 1; p['last_reminder'] = now.isoformat()
        resolved = res.get('mensagens', {}).get('resolved', [])
        try: routine_blocks = routines(s, now)
        except Exception as e: routine_blocks = [f'ROTINAS: falharam ({type(e).__name__}: {str(e)[:120]})']
        routine_blocks += tick_hooks()
        # the job hunt as a task section on the daily Notion page (config "daily_tasks"; watch_core, shared with work-watch)
        if daily_tasks.settings():
            try:
                lines = daily_tasks.tick(s, shortlist_rank)
                if lines: routine_blocks.append('== ' + daily_tasks.settings()['code'] + ' (pagina do dia)\n' + '\n'.join(lines))
            except Exception as e:
                routine_blocks.append(f'PAGINA DO DIA: nao consegui ({type(e).__name__}: {str(e)[:120]})')
        # Planou (config "planou"; watch_core.planou): same list, plus what the person did there
        if daily_tasks.planou_settings():
            try:
                lines = daily_tasks.planou_tick(s, shortlist_rank, now, logged_in=lj.LOGGED_IN_SOURCES)
                # warnings and a lasting breakage go bare (the runner filters AVISO (planou) and dedupes FONTE QUEBRADA);
                # what the person did goes under == PLANOU
                bare = [ln for ln in lines if ln.startswith(('AVISO (planou)', 'FONTE QUEBRADA (planou)'))]
                news = [ln for ln in lines if ln not in bare]
                if news: routine_blocks.append('== PLANOU\n' + '\n'.join(news))
                routine_blocks += bare
            except Exception as e:
                routine_blocks.append(f'AVISO (planou): {type(e).__name__}: {str(e)[:120]}')
        # Notion: pushes the annotated jobs that changed and reads the Pedido column (the user's return channel)
        if (s.get('notion_jobs') or {}).get('db'):
            try:
                tok = nj.token(); done = nj.sync(s, tok)
                reqs = nj.requests(s, tok)
                (s.get('broken') or {}).pop('notion', None)
                if reqs:
                    routine_blocks.append('== NOTION PEDIDOS (' + str(len(reqs)) + ')\n' + '\n'.join(f'-- {p["request"]}: {p["job"]} · id {p["id"]} · page {p["page"]}' for p in reqs))
                if done: routine_blocks.append(f'(notion: {len(done)} linhas sincronizadas)')
            except Exception as e:
                routine_blocks.append(f'NOTION: nao consegui ({type(e).__name__}: {str(e)[:120]})')
                s.setdefault('broken', {}).setdefault('notion', {'since': now.isoformat()})['error'] = f'{type(e).__name__}: {str(e)[:200]}'
        fmd = criteria.pipeline_md()
        if fmd:
            try: write_pipeline_md(s, fmd)
            except OSError as e: routine_blocks.append(f'FUNIL.MD: nao consegui gravar ({e})')
        s['last_run'] = now.isoformat(); save_state(s)
    if a.json:
        print(json.dumps({'res': res, 'broken': broken, 'remind': remind}, ensure_ascii=False, indent=1, default=str))
    else:
        import io, contextlib
        blocks = []
        for f, prt in (('mensagens', print_messages), ('recomendadas', print_recommended), ('vagas', print_jobs), ('mercado', print_boards)):
            if f not in res: continue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                if prt(res[f]):
                    if res[f].get('repeated'): print(f'   (+{res[f]["repeated"]} ja vista(s) por outra fonte, fora)')
                    blocks.append(buf.getvalue().rstrip('\n'))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if print_pending(s, remind, resolved): blocks.append(buf.getvalue().rstrip('\n'))
        blocks += routine_blocks
        if not dry and lessons.is_due(s):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                if lessons.tick(s, AGENT_NAME, dry): blocks.append(buf.getvalue().rstrip('\n'))
        if blocks: print('\n\n'.join(blocks))
        for f, err in broken: print(f'{BROKEN_TOKEN} ({f}): {err}')
    sys.exit(2 if broken else 0)

if __name__ == '__main__':
    main()
