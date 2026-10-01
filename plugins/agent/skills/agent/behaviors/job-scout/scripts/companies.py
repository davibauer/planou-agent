#!/usr/bin/env python3
"""Jobs at the companies that matter by relationship, not by search: former clients (whoever already paid is the easiest
client to sell to again), current clients, consultancies that sell what the user does (subcontracting) and target companies in his ecosystem.

Uses the PUBLIC jobs-guest search with the company filter (f_C=<id,id,...>, validated on 21/09/2026) — no login. The numeric id
comes from `urn:li:organization:<id>` on the company's public page (linkedin.com/company/<slug>), resolved once and stored
in the state (`companies.ids`). Runs 1x/day in the tick (slow routine), not hourly.

  companies.py                # jobs from the last 48h at all COMPANIES (query, saves nothing)
  companies.py --resolve     # (re)resolves the company ids and prints them; 404 = wrong slug, fix it in COMPANIES
"""
import os, sys, re, json, time, argparse, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import linkedin_jobs as lj

# public page slug -> (short name, type). Types: ex-cliente | atual | consultoria | ecossistema.
# Wrong slug = 404 on --resolve; fix it here. Without a resolved id the company is skipped (does not bring down the others).
# {slug of the URL linkedin.com/company/<slug>: (Name, type)}; comes from config.json (criteria.py), nothing hardcoded here
COMPANIES = {k: tuple(v) for k, v in (lj._CFG.get('companies') or {}).items()}
WINDOW_H = 48

def resolve(slug):
    """numeric id of the organization from the public page; None if 404/no id."""
    try:
        r = urllib.request.Request(f'https://www.linkedin.com/company/{slug}/', headers={'User-Agent': lj.UA, 'Accept': 'text/html'})
        with urllib.request.urlopen(r, timeout=30) as resp: t = resp.read().decode('utf-8', 'replace')
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError): return None
    m = re.search(r'urn:li:organization:(\d+)', t)
    return m.group(1) if m else None

def ids(cache):
    """Fills cache {slug: id|None} with what is missing (1 request per new slug, 1 s apart)."""
    for slug in COMPANIES:
        if slug not in cache: cache[slug] = resolve(slug); time.sleep(1)
    return cache

def fetch(cache, hours=WINDOW_H, ignore=frozenset()):
    """One public call per batch of up to 10 companies; returns cards with 'kind' (company type)."""
    by_id = {v: slug for slug, v in cache.items() if v}
    batch, out, seen = [], [], set()
    id_list = list(by_id)
    for i in range(0, len(id_list), 10):
        q = {'f_C': ','.join(id_list[i:i + 10]), 'f_TPR': f'r{hours * 3600}', 'start': 0, 'sortBy': 'DD'}
        cards = lj._cards(lj._get(f'{lj.BASE}/seeMoreJobPostings/search?' + urllib.parse.urlencode(q)))
        for c in cards:
            if c['id'] in ignore or c['id'] in seen: continue
            seen.add(c['id'])
            slug = next((s for s, (n, t) in COMPANIES.items() if n.lower().split()[0] in c['company'].lower()), None)
            name, kind = COMPANIES.get(slug, (c['company'], '?'))
            c['kind'] = kind; c['points'], c['tags'] = lj.score(c['title']); c['gatekeeper'] = lj.title_ok(c['title'])
            out.append(c)
        time.sleep(1)
    # the same job posted per city (Chainlink does this): one line, with the count of locations
    dedup = {}
    for c in out:
        k = (c['title'].lower(), c['company'].lower())
        if k in dedup: dedup[k]['locations'] = dedup[k].get('locations', 1) + 1; dedup[k]['extra_ids'] = dedup[k].get('extra_ids', []) + [c['id']]
        else: dedup[k] = c
    out = list(dedup.values())
    out.sort(key=lambda c: (c['kind'] != 'ex-cliente', c['kind'] != 'atual', not c['gatekeeper'], -c['points']))
    return out

def print_block(cards, gatekeeper_only=True):
    cs = [c for c in cards if c['gatekeeper']] if gatekeeper_only else cards
    if not cs: return False
    print(f'== EMPRESAS ({len(cs)} vagas novas em ex-clientes/atuais/consultorias/ecossistema; 1x/dia)')
    for c in cs:
        loc = f'{c["location"]} (+{c["locations"] - 1} localidades)' if c.get('locations', 1) > 1 else c['location']
        print(f'-- [{c["points"]}] {c["title"]} — {c["company"]} ({c["kind"]}) · {loc} · {", ".join(c["tags"]) or "-"}')
        print(f'   {c["url"]}')
    left_out = len(cards) - len(cs)
    if gatekeeper_only and left_out: print(f'   (+{left_out} vagas nessas empresas fora do porteiro — `companies.py --all` lista)')
    return True

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--resolve', '--resolver', dest='resolver', action='store_true'); ap.add_argument('--all', '--tudo', dest='tudo', action='store_true'); ap.add_argument('--hours', '--horas', dest='horas', type=int, default=WINDOW_H)
    a = ap.parse_args()
    cache = {}
    ids(cache)
    if a.resolver:
        for slug, v in cache.items(): print(f'{slug:20s} {v or "NAO RESOLVIDO (slug errado?)":>12s}  {COMPANIES[slug][0]} · {COMPANIES[slug][1]}')
        return
    if not print_block(fetch(cache, a.horas), gatekeeper_only=not a.tudo): print('nada novo nessas empresas no periodo')

if __name__ == '__main__':
    main()
