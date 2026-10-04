#!/usr/bin/env python3
"""Job sources OUTSIDE LinkedIn, all public and without login (zero risk to the account):
  remoteok    https://remoteok.com/api                     JSON, ~100 most recent jobs, tags/salary/date
  web3career  https://web3.career/<page>                   HTML (table), remote web3 jobs, many contractor, estimated salary (off by default)
  wwr         https://weworkremotely.com/categories/*.rss  RSS of the chosen categories
Sources and pages/categories come from config.json, key "boards": {"sources": [...], "web3career": [...], "wwr": [...]}.
Each source has its own `seen` (in the scout state) and its own error line: one down does not wipe the others.
Gatekeeper and scoring are the same as linkedin_jobs.py (title_ok / score); the judgment is the model's, with profile.md.

  job_boards.py --since 24h [--all] [--json]    # what came out in the sources in the period (saves nothing)
"""
import os, sys, re, json, html, time, argparse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import linkedin_jobs as lj

UA = lj.UA
BRT = lj.BRT
_M = lj._CFG.get('boards') or {}
SOURCES = _M.get('sources') or []
WEB3_PAGES = _M.get('web3career') or []     # e.g.: devops+remote-jobs, remote+security-jobs, remote+solidity-jobs, remote-jobs
WWR_FEEDS = _M.get('wwr') or []             # e.g.: remote-programming-jobs, remote-devops-sysadmin-jobs
MAX_DESCRIPTION = 1500

def _get(url, timeout=30):
    r = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(r, timeout=timeout) as resp: return resp.read().decode('utf-8', 'replace')

def _clean(s): return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()

def _fix_mojibake(s):
    """The RemoteOK API returns part of the texts with UTF-8 read as Latin-1 ('grabaciÃ³n'); undo it when possible."""
    if not s or not re.search('[ÃÂâ][\x80-\xbfŒ-™]', s): return s
    for enc in ('cp1252', 'latin-1'):
        try: return s.encode(enc).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError): pass
    return s

# ---------------- remoteok ----------------
def remoteok(since):
    d = json.loads(_get('https://remoteok.com/api'))
    if len(d) <= 1: raise RuntimeError('remoteok: 0 vagas na API (o formato mudou?)')
    out = []
    for j in d[1:]:
        when = datetime.fromtimestamp(j.get('epoch') or 0, timezone.utc)
        if when < since: continue
        sal = f"US$ {j['salary_min'] // 1000}k–{j['salary_max'] // 1000}k/ano" if j.get('salary_min') else ''
        j = {k: _fix_mojibake(v) if isinstance(v, str) else v for k, v in j.items()}
        out.append({'source': 'remoteok', 'id': f'rok-{j["id"]}', 'title': _clean(j.get('position')), 'company': _clean(j.get('company')),
                    'location': _clean(j.get('location')) or 'Remote', 'when': when.isoformat(), 'salary': sal,
                    'tags': [t for t in (j.get('tags') or []) if isinstance(t, str)][:8],
                    'description': _clean(j.get('description'))[:MAX_DESCRIPTION], 'url': j.get('url') or j.get('apply_url')})
    return out

# ---------------- web3.career ----------------
def web3career(since):
    out, seen = [], set()
    for page in WEB3_PAGES:
        try: t = _get(f'https://web3.career/{page}')
        except (urllib.error.URLError, TimeoutError) as e: raise RuntimeError(f'web3.career/{page}: {e}')
        rows = list(re.finditer(r'<tr data-jobid=(\d+)(.*?)</tr>', t, re.S))
        if not rows: raise RuntimeError(f'web3.career/{page}: 0 vagas na pagina (o HTML mudou?)')
        for m in rows:
            jid, row = m.group(1), m.group(2)
            if jid in seen: continue
            dt_ = re.search(r'datetime="([^"]+)"', row)
            when = datetime.fromisoformat(dt_.group(1)) if dt_ else None
            if when and when.astimezone(timezone.utc) < since: continue
            seen.add(jid)
            href = re.search(r'href="(/[^"]+/%s)"' % jid, row)
            tit = re.search(r'<h2[^>]*>(.*?)</h2>', row, re.S); comp = re.search(r'<h3[^>]*>(.*?)</h3>', row, re.S)
            loc = re.search(r'job-loc-pin.*?</span>\s*<span[^>]*>\s*([^<]*?)\s*<', row, re.S)
            sal = re.search(r'text-salary[^>]*>\s*([^<]+?)\s*<', row)
            tags = re.findall(r'href="/([a-z0-9-]+)-jobs"[^>]*>\s*([^<]+?)\s*</a>', row)
            out.append({'source': 'web3career', 'id': f'w3-{jid}', 'title': _clean(tit.group(1)) if tit else '?', 'company': _clean(comp.group(1)) if comp else '?',
                        'location': _clean(loc.group(1)) if loc else '', 'when': when.astimezone(timezone.utc).isoformat() if when else None,
                        'salary': (_clean(sal.group(1)) + (' (estimado)' if 'estimated_star' in row else '')) if sal else '',
                        'tags': [x[1].strip() for x in tags][:8], 'description': '', 'url': 'https://web3.career' + href.group(1) if href else f'https://web3.career/{page}'})
        time.sleep(1)
    return out

# ---------------- weworkremotely ----------------
def wwr(since):
    out = []
    for feed in WWR_FEEDS:
        try: x = ET.fromstring(_get(f'https://weworkremotely.com/categories/{feed}.rss'))
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as e: raise RuntimeError(f'wwr/{feed}: {e}')
        items = list(x.iter('item'))
        if not items: raise RuntimeError(f'wwr/{feed}: 0 vagas no RSS (o formato mudou?)')
        for it in items:
            g = lambda k: (it.findtext(k) or '').strip()
            try: when = datetime.strptime(g('pubDate'), '%a, %d %b %Y %H:%M:%S %z')
            except ValueError: when = None
            if when and when.astimezone(timezone.utc) < since: continue
            title = g('title'); comp = ''
            if ':' in title: comp, _, title = title.partition(':')
            link = g('link') or g('guid')
            out.append({'source': 'wwr', 'id': 'wwr-' + re.sub(r'\W+', '-', link.split('/')[-1])[:60], 'title': title.strip(), 'company': comp.strip(),
                        'location': g('region') or 'Remote', 'when': when.astimezone(timezone.utc).isoformat() if when else None,
                        'salary': '', 'tags': [g('type')] if g('type') else [], 'description': _clean(g('description'))[:MAX_DESCRIPTION], 'url': link})
        time.sleep(1)
    return out

# ---------------- common ----------------
def collect(since, filter_titles=True, ignore=frozenset()):
    """Returns (jobs, errors_by_source). Jobs already went through the gatekeeper (title) and come scored."""
    jobs, errors = [], {}
    for name, fn in (('remoteok', remoteok), ('web3career', web3career), ('wwr', wwr)):
        if name not in SOURCES: continue
        try:
            for v in fn(since):
                if v['id'] in ignore: continue
                if filter_titles and not lj.title_ok(v['title']): continue
                v['points'], v['ptags'] = lj.score(v['title'] + '\n' + ' '.join(v['tags']) + '\n' + v['description']); v['work_format'] = lj.work_format(v['description']); v['tech_test'] = lj.tech_test(v['description'])
                v['quick_match'] = lj.quick_match(v['title'] + '\n' + ' '.join(v['tags']) + '\n' + v['description'])
                if filter_titles and v['points'] < lj.MIN_SCORE and not v['description']: v['points'] = max(v['points'], 0)
                jobs.append(v)
        except Exception as e:
            errors[name] = f'{type(e).__name__}: {str(e)[:120]}'
    if filter_titles: jobs = [v for v in jobs if v['points'] >= lj.MIN_SCORE or (v['source'] == 'web3career' and v['points'] >= 1)]
    jobs.sort(key=lambda v: (-v['points'], v['title']))
    return jobs, errors

def print_block(jobs, errors):
    if not jobs and not errors: return False
    if jobs:
        print(f'== MERCADO ({len(jobs)} novas fora do LinkedIn: ' + ', '.join(f'{f} {sum(1 for v in jobs if v["source"] == f)}' for f in SOURCES if any(v['source'] == f for v in jobs)) + ')')
        for v in jobs:
            sal = (f' · {v["salary"]}' if v.get('salary') else '') + (f' · formato: {"/".join(v["work_format"])}' if v.get('work_format') else '') + lj.test_tag(v)
            print(f'-- [{v["points"]}] {v["title"]} — {v["company"]} · {v["location"]}{sal} · {v["source"]} · {", ".join(v["ptags"]) or "-"}{lj.extras(v)}')
            if v.get('description'): print(f'   {v["description"][:220]}…')
            print(f'   {v["url"]}')
    for f, e in errors.items(): print(f'   (fonte {f} falhou: {e})')
    return True

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--since', default='24h'); ap.add_argument('--all', '--tudo', dest='tudo', action='store_true'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    since = datetime.now(timezone.utc) - timedelta(seconds=lj.parse_since(a.since))
    jobs, errors = collect(since, filter_titles=not a.tudo)
    if a.json: print(json.dumps({'jobs': jobs, 'errors': errors}, ensure_ascii=False, indent=1)); return
    if not print_block(jobs, errors): print('nada novo nas fontes externas no periodo')

if __name__ == '__main__':
    main()
