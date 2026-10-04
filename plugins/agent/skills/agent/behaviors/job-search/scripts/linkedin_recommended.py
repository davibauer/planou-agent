#!/usr/bin/env python3
"""Job recommendations from LOGGED-IN LinkedIn ("Top job picks for you" on the jobs home), through the new SDUI page.

One POST call to /flagship-web/rsc-action/actions/pagination?sduiid=…jobsHomeFeedModuleList (same session as
linkedin_msgs) returns an RSC stream (React flight, ~1.4 MB) with ~40 cards: id, title, company, location and the insights
LinkedIn computes for YOU — especially "Voce seria um dos melhores candidatos" (top applicant, Premium).
It is not JSON: parsing is by regex on the rendered text nodes. If the markup changes, the regexes in `cards()` are the place.
Validated on 21/09/2026 (cURL captured from the jobs home; body in `body()`, headers in `HEADERS`).

  linkedin_recommended.py                 # lists the current recommendations (one call)
  linkedin_recommended.py --json
"""
import os, sys, re, json, argparse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import linkedin_msgs as lm

URL = 'https://www.linkedin.com/flagship-web/rsc-action/actions/pagination?sduiid=com.linkedin.sdui.pagers.jobseeker.jobsHomeFeedModuleList'
PAGER = 'com.linkedin.sdui.pagers.jobseeker.jobsHomeFeedModuleList'
COUNT = 20                     # requested; the server returns ~2x ("top picks" + "more jobs" modules)
TOP = re.compile(r'melhores candidatos|top applicant', re.I)
LOCATION = re.compile(r'\((Remoto|Presencial|H[ií]brido|Remote|On-site|Hybrid)\)')

def body():
    ra = {"$type": "proto.sdui.actions.requests.RequestedArguments", "requestedStateKeys": [],
          "payload": {"start": 0, "count": COUNT, "hasSeenTopJymbii": False},
          "requestMetadata": {"$type": "proto.sdui.common.RequestMetadata"}}
    return {"pagerId": PAGER,
            "clientArguments": dict(ra, states=[], screenId="com.linkedin.sdui.flagshipnav.jobs.JobsHome", knownTemplateIds=[]),
            "paginationRequest": {"$type": "proto.sdui.actions.requests.PaginationRequest", "pagerId": PAGER,
                                  "trigger": {"$case": "itemDistanceTrigger", "itemDistanceTrigger": {"$type": "proto.sdui.actions.requests.ItemDistanceTrigger", "preloadDistance": 3, "preloadLength": 250}},
                                  "retryCount": 0, "requestedArguments": ra}}

def fetch():
    c = lm.session(); js = c['jsessionid'].strip('"')
    h = {'User-Agent': c.get('ua') or lm.UA, 'accept': '*/*', 'content-type': 'application/json', 'csrf-token': js,
         'origin': 'https://www.linkedin.com', 'referer': 'https://www.linkedin.com/jobs/', 'x-li-rsc-stream': 'true',
         'x-li-anchor-page-key': 'd_flagship3_job_home', 'x-li-page-instance': 'urn:li:page:d_flagship3_job_home;JuUaqxr4SLWjw46WSrg5kA==',
         'x-li-application-version': '0.2.7404', 'Cookie': f'li_at={c["li_at"]}; JSESSIONID="{js}"; sdui_ver=sdui-flagship:0.1.53818+SduiFlagship0'}
    lm.api  # (same pause between calls as the messages module)
    wait = lm._last + lm.PAUSE - __import__('time').monotonic()
    if wait > 0: __import__('time').sleep(wait)
    lm._last = __import__('time').monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(URL, data=json.dumps(body()).encode(), headers=h, method='POST'), timeout=60) as r:
            return r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403): sys.exit(f'Sessao recusada ({e.code}) nas recomendacoes: li_at venceu — regravar com linkedin_msgs.py --session')
        if e.code == 999: sys.exit('HTTP 999 nas recomendacoes: bloqueio anti-automacao — parar o job-scout')
        sys.exit(f'HTTP {e.code} nas recomendacoes: {e.read()[:200]!r}')
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f'rede (recomendacoes): {e}')

def _txt(s): return json.loads(f'"{s}"') if '\\' in s else s

def cards(t):
    firsts, seen = [], set()
    for m in re.finditer(r'"jobId":"(\d{8,})"', t):
        if m.group(1) not in seen: seen.add(m.group(1)); firsts.append((m.start(), m.group(1)))
    out = []
    # a card's rendered content (company, location, insights, title) comes BEFORE its jobId: segment = (previous id, this id]
    for k, (pos, jid) in enumerate(firsts):
        seg = t[firsts[k-1][0] if k else max(0, pos-60000): pos]
        titles = re.findall(r'Fechar vaga de (.+?)"', seg) or re.findall(r'Dismiss (.+?) job"', seg)
        title = titles[-1] if titles else None      # the last one before the id is this card's; the first may be the previous one repeated
        nodes = [_txt(n) for n in re.findall(r'"children":\["((?:[^"\\]|\\.){1,160})"\]', seg)]
        company = location = ''
        for i, n in enumerate(nodes):
            if n == ' • ' and i > 0 and i+1 < len(nodes) and LOCATION.search(nodes[i+1]): company, location = nodes[i-1], nodes[i+1]; break
        if not location:
            loc = next((n for n in nodes if LOCATION.search(n)), ''); location = loc
        connections = next((n for n in nodes if re.search(r'conex[aõ]|alumni|trabalha', n)), '')
        out.append({'id': jid, 'title': _txt(title) if title else '?', 'company': company, 'location': location,
                    'top': bool(TOP.search(seg)), 'promoted': 'Promovida' in nodes or 'Promoted' in nodes,
                    'verified': 'Vaga verificada' in seg or 'Verified' in seg, 'connections': connections,
                    'url': f'https://www.linkedin.com/jobs/view/{jid}'})
    return out

def recommended():
    return cards(fetch())

def print_block(cs, title='== RECOMENDADAS (LinkedIn)'):
    if not cs: return False
    print(f'{title} ({len(cs)})')
    for v in cs:
        tags = [x for x, ok in (('TOP APPLICANT', v['top']), ('promovida', v['promoted']), (v['connections'], bool(v['connections']))) if ok]
        import linkedin_jobs as lj
        extra = (f' · {v.get("level")}' if v.get('level') else '') + (f' · formato: {"/".join(v["work_format"])}' if v.get('work_format') else '') + lj.test_tag(v) + lj.age(v)
        pts = f'[{v["points"]}] ' if 'points' in v else ''
        print(f'-- {pts}{v["title"]} — {v["company"] or "?"} · {v["location"] or "?"}{extra} · {", ".join(tags) or "-"}{lj.extras(v)}')
        if v.get('snippet'): print(f'   {v["snippet"][:220]}…')
        print(f'   {v["url"]}')
    return True

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--json', action='store_true'); a = ap.parse_args()
    cs = recommended()
    if a.json: print(json.dumps(cs, ensure_ascii=False, indent=1)); return
    if not print_block(cs): print('nenhuma recomendacao (markup mudou? ver --json / o stream cru)')

if __name__ == '__main__':
    main()
