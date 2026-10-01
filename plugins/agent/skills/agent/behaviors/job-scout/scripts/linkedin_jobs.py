#!/usr/bin/env python3
"""LinkedIn jobs through the public search (jobs-guest), WITHOUT login and without a cookie.

Library for scout.py (search / detail / score) and query CLI:
  linkedin_jobs.py --since 24h            # jobs posted in the last 24h that pass the CRITERIA (1h, 24h, 3d)
  linkedin_jobs.py --since 24h --all     # same, without the title filter (see what the search returns)
  linkedin_jobs.py --show 4467603178      # detail: full description, level, type, industry
  linkedin_jobs.py --json ...

The endpoint is the same one the jobs page uses for logged-out visitors: card HTML, parsed here with regex.
If LinkedIn changes the markup, the regexes in `_cards()` / `detail()` are the only place to touch.
"""
import os, re, sys, json, html, time, argparse, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

BRT = timezone(timedelta(hours=-3))   # replaced by the config timezone right below (criteria.tz)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36'
BASE = 'https://www.linkedin.com/jobs-guest/jobs/api'

# ---------------- CRITERIA ----------------
# No criterion lives in the code: searches, gatekeeper (include/exclude in the title), points, min_score, boards and companies come
# from criteria.load() = ~/.config/job-scout/config.json on top of config-example/config.json. The user adjusts it
# THROUGH Claude (criteria.py --apply, with a diff and their ok); updating the plugin never touches config.json.
# The gatekeeper only cuts noise for cost; the JUDGMENT of "makes sense" is the model's, against profile.md.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import criteria
_CFG = criteria.load()
BRT = criteria.tz()                                     # historical name: it is the user's timezone (tz_hours in the config)
SEARCHES = _CFG.get('searches') or []                 # each search = 1 call per tick; remote=True -> f_WT=2
INCLUDE = re.compile(_CFG.get('include') or '.', re.I)
EXCLUDE = re.compile(_CFG.get('exclude') or r'(?!x)x', re.I)
POINTS = [tuple(p) for p in _CFG.get('points') or []]  # [(regex, label)]: one point per matching group; only sorts
MIN_SCORE = int(_CFG.get('min_score') or 0)           # below this the job does not even show in the tick (shows in --all)
# inbox and recommended use LinkedIn's internal API with the user's cookie (outside LinkedIn's terms of use):
# off by default, the user turns them on knowingly
LOGGED_IN_SOURCES = bool(_CFG.get('logged_in_sources', False))
EXCLUDED_LEVELS = {'Entry level', 'Internship', 'Associate'}   # "Seniority level" of the detail page
MAX_DETAILS_PER_TICK = 12                                   # detail pages per tick (1 req each; polite with the rate limit)
# Saturated: old posting AND more than 100 applicants = out. Both conditions together;
# a freshly (re)posted job with 200+ applicants still shows, marked as "concorrida" — the model decides.
# config "saturation": {"days": N, "applicants": M} changes both (days 0 = applicants alone decide).
SATURATED_DAYS = int((_CFG.get('saturation') or {}).get('days', 14))
SATURATED_APPLICANTS = int((_CFG.get('saturation') or {}).get('applicants', 100))
# "top_applicant_exempt" (default true): a TOP APPLICANT job (logged-in recommended feed) is kept even when saturated;
# false = it is cut like any other, at the entry and in the shortlist (01/10/2026, user's decision).
TOP_APPLICANT_EXEMPT = bool((_CFG.get('saturation') or {}).get('top_applicant_exempt', True))
# ---------------------------------------------------------------------------

def _get(url, attempts=3):
    for i in range(attempts):
        try:
            r = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9,pt-BR;q=0.8'})
            with urllib.request.urlopen(r, timeout=30) as resp: return resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and i < attempts - 1: time.sleep(5 * (i + 1)); continue
            raise RuntimeError(f'HTTP {e.code} em {url[:90]}')
        except (urllib.error.URLError, TimeoutError) as e:
            if i < attempts - 1: time.sleep(3); continue
            raise RuntimeError(f'rede: {e}')

def _txt(m, g=1):
    return html.unescape(re.sub(r'\s+', ' ', m.group(g))).strip() if m else ''

def _cards(h):
    out = []
    for c in re.split(r'<li\b', h)[1:]:
        jid = re.search(r'urn:li:jobPosting:(\d+)', c)
        if not jid: continue
        dt = re.search(r'datetime="([^"]+)"', c)
        out.append({'id': jid.group(1),
                    'title': _txt(re.search(r'base-search-card__title">\s*([^<]+?)\s*<', c)),
                    'company': _txt(re.search(r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*([^<]+?)\s*<', c)),
                    'location': _txt(re.search(r'job-search-card__location">\s*([^<]+?)\s*<', c)),
                    'when': (dt.group(1) + 'T00:00:00+00:00') if dt else None,
                    'url': f'https://www.linkedin.com/jobs/view/{jid.group(1)}'})
    return out

def search(keywords, location, seconds, remote=True, pages=2):
    """Public search cards: up to `pages` x 25 (the endpoint pages 25 at a time with start=)."""
    out, seen = [], set()
    for p in range(pages):
        q = {'keywords': keywords, 'location': location, 'f_TPR': f'r{int(seconds)}', 'start': p * 25, 'sortBy': 'DD'}
        if remote: q['f_WT'] = '2'
        cards = _cards(_get(f'{BASE}/seeMoreJobPostings/search?' + urllib.parse.urlencode(q)))
        new = [c for c in cards if c['id'] not in seen]
        for c in new: seen.add(c['id']); out.append(c)
        if len(cards) < 25: break
        time.sleep(1)
    return out

def _days(txt):
    """'16 hours ago' / 'há 3 semanas' / '2 months ago' -> days (int) or None."""
    m = re.search(r'(\d+)\s*(hour|hora|day|dia|week|semana|month|mês|mes)', txt or '', re.I)
    if not m: return None
    n, u = int(m.group(1)), m.group(2).lower()
    return 0 if u.startswith(('hour', 'hora')) else n if u.startswith(('day', 'dia')) else n * 7 if u.startswith(('week', 'semana')) else n * 30

def _n_applicants(txt):
    """'Over 200 applicants' / 'Mais de 100 candidaturas' / '37 applicants' -> int or None ('Be among the first 25' -> 0)."""
    if not txt: return None
    if re.search(r'first|primeir', txt, re.I): return 0
    m = re.search(r'(\d+)', txt); return int(m.group(1)) if m else None

def is_saturated(v):
    return (v.get('days') or 0) >= SATURATED_DAYS and (v.get('n_applicants') or 0) > SATURATED_APPLICANTS

def saturated_out(v, top=False):
    """Saturated by the config rule and not spared as TOP APPLICANT (saturation.top_applicant_exempt)."""
    return is_saturated(v) and not (top and TOP_APPLICANT_EXEMPT)

def detail(jid):
    h = _get(f'{BASE}/jobPosting/{jid}')
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', h, re.S)
    desc = html.unescape(re.sub(r'<br\s*/?>|</p>|</li>', '\n', m.group(1))) if m else ''
    desc = re.sub(r'<[^>]+>', ' ', desc); desc = re.sub(r'[ \t]+', ' ', desc); desc = re.sub(r'\n\s*\n+', '\n', desc).strip()
    crit = re.findall(r'description__job-criteria-text[^>]*>\s*([^<]+?)\s*<', h)
    crit = [html.unescape(x.strip()) for x in crit]
    cand = re.search(r'num-applicants__caption[^>]*>\s*([^<]+?)\s*<', h)
    posted = _txt(re.search(r'posted-time-ago__text[^>]*>\s*([^<]+?)\s*<', h))
    return {'posted': posted, 'days': _days(posted), 'n_applicants': _n_applicants(_txt(cand) if cand else ''),
            'id': jid, 'description': desc, 'level': crit[0] if len(crit) > 0 else '', 'kind': crit[1] if len(crit) > 1 else '',
            'job_function': crit[2] if len(crit) > 2 else '', 'industry': crit[3] if len(crit) > 3 else '',
            'applicants': _txt(cand) if cand else '',
            'title': _txt(re.search(r'top-card-layout__title[^>]*>\s*([^<]+?)\s*<', h)),
            'location': _txt(re.search(r'topcard__flavor--bullet[^>]*>\s*([^<]+?)\s*<', h)),
            'company': _txt(re.search(r'topcard__org-name-link[^>]*>\s*([^<]+?)\s*<', h)),
            'recruiter': recruiter(h),
            'url': f'https://www.linkedin.com/jobs/view/{jid}'}

def recruiter(h):
    """Who posted the job, when the public page shows it ('Meet the hiring team' / message-the-recruiter):
    {'name', 'headline', 'profile'} or None. No login needed."""
    m = re.search(r'message-the-recruiter(.*?)</section>', h, re.S)
    if not m: return None
    b = m.group(1)
    name = _txt(re.search(r'base-main-card__title[^>]*>\s*([^<]+?)\s*<', b))
    if not name: return None
    url = re.search(r'href="(https://[a-z]+\.linkedin\.com/in/[^"?]+)', b)
    return {'name': name, 'headline': _txt(re.search(r'base-main-card__subtitle[^>]*>\s*([^<]+?)\s*<', b)),
            'profile': url.group(1) if url else ''}

def job_page(jid):
    """One read of the public page: {'state': 'aberta' | 'encerrada' | 'removida', 'days', 'n_applicants', 'applicants'}.
    The shortlist uses it to check, with a single request per job, that the job is open and how many applied (01/10/2026:
    a job that entered the pipeline with fewer applicants than the saturation rule passed it later and was recommended)."""
    try:
        h = _get(f'{BASE}/jobPosting/{jid}')
    except RuntimeError as e:
        if 'HTTP 404' in str(e) or 'HTTP 410' in str(e): return {'state': 'removida', 'days': None, 'n_applicants': None, 'applicants': ''}
        raise
    cand = re.search(r'num-applicants__caption[^>]*>\s*([^<]+?)\s*<', h)
    posted = _txt(re.search(r'posted-time-ago__text[^>]*>\s*([^<]+?)\s*<', h))
    return {'state': 'encerrada' if re.search(r'No longer accepting applications|closed-job__flavor--closed', h) else 'aberta',
            'days': _days(posted), 'n_applicants': _n_applicants(_txt(cand) if cand else ''), 'applicants': _txt(cand) if cand else ''}

def job_state(jid):
    """'aberta' | 'encerrada' (public page says "No longer accepting applications") | 'removida' (HTTP 404).
    Verified on 23/09/2026 with old jobs; no cookie, same page as a logged-out visitor."""
    return job_page(jid)['state']

def logged_in_detail(jid):
    """Extras that only the logged-in API gives (linkedin_msgs session): applicants, views, salary, screening questions. None without a session."""
    try:
        import linkedin_msgs as lm
        if not lm.cfg().get('li_at'): return None
        d = lm.api(f'/voyager/api/jobs/jobPostings/{jid}')
    except SystemExit as e:
        return {'error': str(e.code)[:120]}
    questions = [q.get('question', {}).get('text') or str(q)[:80] for q in (d.get('requiredScreeningQuestions') or []) + (d.get('preferredScreeningQuestions') or [])]
    am = d.get('applyMethod') or {}
    return {'applicants': d.get('applies'), 'views': d.get('views'), 'salary': d.get('formattedSalaryDescription') or '',
            'remote': d.get('workRemoteAllowed'), 'easy_apply': any('OnsiteApply' in k for k in am), 'expires': _iso(d.get('expireAt')),
            'questions': questions, 'level': d.get('formattedExperienceLevel') or ''}

def logged_in_application(jid):
    """Whether YOU applied to this job, through the logged-in API (applyingInfo): {'has_applied': bool, 'when': iso, 'cv': file name,
    'closed': bool, 'activities': [{'kind', 'text'}]}. It is LinkedIn's official signal (Easy Apply and 'Yes, I applied' on an external site), unlike the feed's
    'Candidatou-se' text, which is a template. None without a session; SystemExit propagates (expired session = broken source)."""
    import linkedin_msgs as lm
    if not lm.cfg().get('li_at'): return None
    ai = lm.api(f'/voyager/api/jobs/jobPostings/{jid}').get('applyingInfo') or {}
    from datetime import datetime, timezone
    q = ai.get('appliedAt')
    return {'has_applied': bool(ai.get('applied')), 'when': datetime.fromtimestamp(q / 1000, timezone.utc).isoformat(timespec='seconds') if q else '',
            'cv': ai.get('resumeFileName') or '', 'closed': bool(ai.get('closed')),
            # progress LinkedIn shows you: APPLY ('You applied ...') and, when it happens, application viewed,
            # CV downloaded etc. The type is stable; the text carries the 'N days ago'.
            'activities': [{'kind': a.get('type', ''), 'text': a.get('text', '')} for a in ai.get('activities') or []]}

def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%d/%m') if ms else ''

WORK_FORMATS = [(r'\bclt\b', 'CLT'), (r'\b(pj|pessoa jur[ií]dica)\b', 'PJ'), (r'\b(contractor|contrato de prestac|b2b|independent contractor)\b', 'contractor'),
                (r'\b(hourly|por hora|/h\b|per hour)', 'hourly'), (r'\b(freelance|freelancer)\b', 'freelance'), (r'\b(h[ií]brido|hybrid)\b', 'híbrido'), (r'\b(presencial|on-?site|in-office)\b', 'presencial')]

# "hybrid"/"hibrido" is also a technical term (hybrid search, nuvem hibrida, app hibrido): it only counts as a work arrangement
# when it is not attached to one of those terms. "remoto" only when the posting says so explicitly (fully remote, 100% remoto, Remote - Brazil).
HYBRID_TECHNICAL = re.compile(r'(search|busca|cloud|nuvem|app|apps|aplicativ|mobile|retrieval|rag|approach|abordagem|architecture|arquitetura|'
                              r'infra|environment|ambiente|storage|database|banco|encryption|criptografia)', re.I)
EXPLICIT_REMOTE = re.compile(r'fully remote|100\s*%\s*remot[eo]|totalmente remot|remote[- ]first|trabalho remoto|remote\s*[-–]\s*(brazil|brasil|latam|latin america)|'
                             r'remote (in|from) (brazil|brasil|latam|latin america|anywhere)|remote across|home office|anywhere in (brazil|latam|the world)', re.I)

def _is_work_hybrid(text):
    for m in re.finditer(r'\b(h[ií]brid[oa]s?|hybrid)\b', text, re.I):
        near = text[max(0, m.start() - 22):m.end() + 22]
        if not HYBRID_TECHNICAL.search(near.replace(m.group(0), ' ')): return True
    return False

def work_format(text):
    """Contract type/work arrangement tags found in the description (the 220-char summary hides this; Caylent said CLT in the footer)."""
    tags = [label for rx, label in WORK_FORMATS if label != 'híbrido' and re.search(rx, text, re.I)]
    if _is_work_hybrid(text): tags.insert(len(tags) - ('presencial' in tags), 'híbrido')
    if EXPLICIT_REMOTE.search(text or ''): tags.append('remoto')
    return tags

# Technical test in the hiring process (26/09/2026, user: "no interest in technical tests; always look for this"). Only
# explicit phrases: a take-home, a live coding round, a named test platform. The model judges; this only surfaces it.
TECH_TESTS = [(r'take[- ]?home', 'take-home'), (r'live[- ]coding', 'live coding'),
              (r'coding (challenge|test|exercise|assessment|interview|round)', 'coding challenge'),
              (r'technical (test|assessment|challenge|exercise|assignment)', 'teste técnico'),
              (r'(teste|prova|desafio|case|avalia[cç][aã]o) t[eé]cnic[oa]', 'teste técnico'),
              (r'pair[- ]programming (interview|session|exercise)', 'pair programming'),
              (r'\b(hackerrank|codility|codesignal|testgorilla|coderbyte|leetcode|devskiller|qualified\.io)\b', None)]

def tech_test(text):
    """Technical-test signals in the posting ('take-home', 'HackerRank'...), deduplicated; [] when the posting says nothing."""
    out = []
    for rx, label in TECH_TESTS:
        for m in re.finditer(rx, text or '', re.I):
            lb = label or m.group(0).lower()
            if lb not in out: out.append(lb)
            if label: break
    return out

def test_tag(v):
    """' · PROVA TECNICA: take-home' for the job line (empty when the posting does not mention one)."""
    return f' · PROVA TECNICA: {"/".join(v["tech_test"])}' if v.get('tech_test') else ''

def title_ok(t):
    return bool(INCLUDE.search(t)) and not EXCLUDE.search(t)

# ---------------- Quick match (every job; the full matrix is for the strong ones) ----------------
# Generic technology vocabulary: what the job ASKS for (found in the description) against what you HAVE (config "skills":
# list of labels from this vocabulary, or [regex, label] for something not here). Score = have / asked. It is a cheap
# number for sorting, not the judgment: synonyms and "nice to have" are not told apart.
TECH = [
    (r'\bkubernetes|\bk8s\b|\baks\b|\beks\b|\bgke\b', 'Kubernetes'), (r'\bdocker\b|contain', 'Docker'), (r'\bhelm\b', 'Helm'),
    (r'argo ?cd', 'Argo CD'), (r'\bterraform\b', 'Terraform'), (r'\bpulumi\b', 'Pulumi'), (r'\bbicep\b|\barm templates?\b', 'Bicep/ARM'),
    (r'\bansible\b', 'Ansible'), (r'\bazure\b', 'Azure'), (r'\baws\b|amazon web services', 'AWS'), (r'\bgcp\b|google cloud', 'GCP'),
    (r'azure devops|\bado\b', 'Azure DevOps'), (r'github actions', 'GitHub Actions'), (r'gitlab', 'GitLab CI'), (r'\bjenkins\b', 'Jenkins'),
    (r'\bharness\b', 'Harness'), (r'circleci', 'CircleCI'), (r'prometheus', 'Prometheus'), (r'grafana', 'Grafana'), (r'datadog', 'Datadog'),
    (r'new relic', 'New Relic'), (r'opentelemetry|\botel\b', 'OpenTelemetry'), (r'\belk\b|elasticsearch|opensearch', 'Elastic'),
    (r'splunk', 'Splunk'), (r'\blinux\b', 'Linux'), (r'\bbash\b|shell script', 'Bash'), (r'powershell', 'PowerShell'),
    (r'\bpython\b', 'Python'), (r'\.net\b|\bc#|dotnet|asp\.net', '.NET/C#'), (r'\bjava\b(?!script)', 'Java'), (r'\bgo\b|golang', 'Go'),
    (r'\brust\b', 'Rust'), (r'node\.?js|\bnode\b', 'Node.js'), (r'typescript', 'TypeScript'), (r'javascript', 'JavaScript'),
    (r'\breact\b', 'React'), (r'angular', 'Angular'), (r'\bphp\b|laravel', 'PHP'), (r'\bruby\b|rails', 'Ruby'), (r'kotlin', 'Kotlin'),
    (r'\bsql server\b|t-sql|mssql', 'SQL Server'), (r'postgres', 'PostgreSQL'), (r'mysql', 'MySQL'), (r'mongodb', 'MongoDB'),
    (r'cosmos ?db', 'Cosmos DB'), (r'\bredis\b', 'Redis'), (r'kafka', 'Kafka'), (r'rabbitmq', 'RabbitMQ'), (r'service bus', 'Service Bus'),
    (r'microservi', 'Microsserviços'), (r'\bapi(s)?\b|rest\b|graphql', 'APIs'), (r'serverless|lambda|azure functions', 'Serverless'),
    (r'devsecops|appsec|sast|dast', 'DevSecOps'), (r'sonar', 'SonarQube'), (r'snyk', 'Snyk'), (r'vault\b', 'Vault'), (r'key vault', 'Key Vault'),
    (r'\biam\b|entra id|azure ad|active directory', 'IAM'), (r'soc ?2|iso ?27001|pci|hipaa|lgpd|gdpr', 'Compliance'),
    (r'\bllms?\b|large language model', 'LLM'), (r'agentic|ai agents?|agentes? de ia', 'Agentes de IA'), (r'\bmcp\b|model context protocol', 'MCP'),
    (r'\brag\b|retrieval', 'RAG'), (r'langchain|langgraph', 'LangChain'), (r'crewai', 'CrewAI'), (r'openai|anthropic|claude|gpt', 'APIs de LLM'),
    (r'machine learning|\bml\b|mlops', 'ML/MLOps'), (r'blockchain|web3|smart contracts?|solidity|evm', 'Blockchain'),
    (r'\bsre\b|site reliability|on-?call|incident', 'SRE/on-call'), (r'finops|cost optim', 'FinOps'), (r'networking|vpc|vnet|dns|load balanc', 'Redes cloud'),
    (r'\bscrum\b|agile|kanban', 'Ágil'), (r'mentor|lideran|leadership|tech lead', 'Liderança técnica'),
]
_SKILLS_CFG = _CFG.get('skills') or []
SKILLS = {h for h in _SKILLS_CFG if isinstance(h, str)}
TECH_EXTRA = [tuple(h) for h in _SKILLS_CFG if isinstance(h, list)]

def quick_match(text):
    """(pct, missing) with what the job asks for against the config's 'skills'; None if the job asks for fewer than 3
    recognizable things or if the user has not filled in 'skills' yet."""
    if not SKILLS and not TECH_EXTRA: return None
    asked = [r for rx, r in TECH + TECH_EXTRA if re.search(rx, text or '', re.I)]
    if len(asked) < 3: return None
    have = [r for r in asked if r in SKILLS or any(r == x[1] for x in TECH_EXTRA)]
    return round(100 * len(have) / len(asked)), [r for r in asked if r not in have]

def extras(v):
    """' · match~ 70% (falta: Go, Datadog) · REPUBLICADA (1a vez em 02/09)' for the job line."""
    out = ''
    m = v.get('quick_match')
    if m: out += f' · match~ {m[0]}%' + (f' (falta: {", ".join(m[1][:4])})' if m[1] else '')
    if v.get('reposted'): out += f' · REPUBLICADA (1a vez em {v["reposted"]})'
    if v.get('recruiter'): out += f' · publicada por {v["recruiter"]["name"]}'
    return out

def score(text):
    tags = [label for rx, label in POINTS if re.search(rx, text, re.I)]
    return len(tags), tags

def parse_since(s):
    m = re.fullmatch(r'(\d+)\s*(m|h|d)', s.strip())
    if not m: sys.exit('--since: use 30m, 4h, 2d')
    n, u = int(m.group(1)), m.group(2)
    return n * {'m': 60, 'h': 3600, 'd': 86400}[u]

# Signals for the "LinkedIn changed the HTML" alarm (scout.py reads it after each collect): raw search cards and
# detail pages that came back without a description. A broken parser gives no error, it gives zero: without this the scout would go silent.
LAST_COLLECTION = {'cards': 0, 'details': 0, 'empty_details': 0}

def collect(seconds, filter_titles=True, max_details=MAX_DETAILS_PER_TICK, ignore=frozenset(), pages=2):
    """Runs all SEARCHES, dedups by id, filters by title, fetches the detail of the candidates and scores. Returns (jobs, errors)."""
    jobs, errors = {}, []
    LAST_COLLECTION.update(cards=0, details=0, empty_details=0)
    for b in SEARCHES:
        try:
            for c in search(b['keywords'], b['location'], seconds, b.get('remote', True), pages):
                LAST_COLLECTION['cards'] += 1
                if c['id'] in ignore or c['id'] in jobs: continue
                if filter_titles and not title_ok(c['title']): continue
                c['search'] = b['keywords'][:30]; jobs[c['id']] = c
        except RuntimeError as e:
            errors.append(f'{b["keywords"][:30]} @ {b["location"]}: {e}')
        time.sleep(1)
    if errors and len(errors) == len(SEARCHES): raise RuntimeError('todas as buscas falharam: ' + '; '.join(errors))
    items = list(jobs.values())
    for i, v in enumerate(items):
        if i >= max_details: v['detailed'] = False; v['points'], v['tags'] = score(v['title']); continue
        try:
            d = detail(v['id']); v.update(level=d['level'], kind=d['kind'], applicants=d['applicants'], detailed=True,
                                          posted=d['posted'], days=d['days'], n_applicants=d['n_applicants'],
                                          snippet=d['description'][:400].replace('\n', ' '), work_format=work_format(d['description']),
                                          tech_test=tech_test(d['description']))
            v['points'], v['tags'] = score(v['title'] + '\n' + d['description'])
            v['quick_match'] = quick_match(v['title'] + '\n' + d['description']); v['recruiter'] = d.get('recruiter')
            v['level_excluded'] = d['level'] in EXCLUDED_LEVELS
            LAST_COLLECTION['details'] += 1; LAST_COLLECTION['empty_details'] += not d['description'].strip()
        except RuntimeError as e:
            v['detailed'] = False; v['points'], v['tags'] = score(v['title']); v['error'] = str(e)
        time.sleep(1)
    items.sort(key=lambda v: (-v['points'], v['title']))
    if filter_titles:
        sat = [v for v in items if is_saturated(v)]
        if sat: errors.append(f'{len(sat)} saturada(s) (>{SATURATED_DAYS} dias e >{SATURATED_APPLICANTS} candidatos): ' + '; '.join(f'{v["title"][:40]} — {v["company"]}' for v in sat[:5]))
        items = [v for v in items if v['points'] >= MIN_SCORE and not v.get('level_excluded') and not is_saturated(v)]
    return items, errors

def age(v):
    """' · há 16 hours ago · 200+ candidatos (concorrida)' for the job line."""
    p = []
    if v.get('posted'): p.append(v['posted'])
    if v.get('n_applicants') is not None:
        n = v['n_applicants']; p.append('primeiros 25' if n == 0 else f'{n}{"+" if "ver" in (v.get("applicants") or "").lower() or "mais" in (v.get("applicants") or "").lower() else ""} candidatos' + (' (concorrida)' if n > SATURATED_APPLICANTS else ''))
    return (' · ' + ' · '.join(p)) if p else ''

def print_block(jobs, errors=()):
    if not jobs and not errors: return False
    if jobs:
        print(f'== VAGAS ({len(jobs)} novas que passam no filtro)')
        for v in jobs:
            level = f' · {v["level"]}' if v.get('level') else ''
            out = ' · NIVEL FORA' if v.get('level_excluded') else ''
            fmt = f' · formato: {"/".join(v["work_format"])}' if v.get('work_format') else ''
            print(f'-- [{v["points"]}] {v["title"]} — {v["company"]} · {v["location"]}{level}{out}{fmt}{test_tag(v)}{age(v)} · {", ".join(v["tags"]) or "-"}{extras(v)}')
            if v.get('snippet'): print(f'   {v["snippet"][:220]}…')
            print(f'   {v["url"]}')
    for e in errors: print(f'   (busca falhou: {e})')
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='24h'); ap.add_argument('--all', '--tudo', dest='tudo', action='store_true', help='without the title filter')
    ap.add_argument('--show', metavar='ID'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if a.show:
        d = detail(a.show)
        if a.json: print(json.dumps(d, ensure_ascii=False, indent=1)); return
        print(f'{d["title"]} — {d["company"]}\n{d["level"]} · {d["kind"]} · {d["job_function"]} · {d["industry"]} · {d["applicants"]} · {d["posted"]}' + (f' · SATURADA (>{SATURATED_APPLICANTS} candidatos' + (f' e {SATURATED_DAYS}+ dias' if SATURATED_DAYS else '') + ')' if is_saturated(d) else '') + (f' · PROVA TECNICA: {"/".join(tech_test(d["description"]))}' if tech_test(d['description']) else '') + f'\n{d["url"]}')
        x = logged_in_detail(a.show)
        if x and 'error' in x: print(f'(logado: {x["error"]})')
        elif x:
            print(f'logado: {x["applicants"]} candidatos · {x["views"]} views · salario: {x["salary"] or "-"} · remoto: {x["remote"]} · easy apply: {x["easy_apply"]} · expira {x["expires"]}')
            for q in x['questions']: print(f'   pergunta de triagem: {q}')
        print(); print(d['description']); return
    jobs, errors = collect(parse_since(a.since), filter_titles=not a.tudo)
    if a.json: print(json.dumps({'jobs': jobs, 'errors': errors}, ensure_ascii=False, indent=1)); return
    if not print_block(jobs, errors): print('nenhuma vaga nova no periodo')

if __name__ == '__main__':
    main()
