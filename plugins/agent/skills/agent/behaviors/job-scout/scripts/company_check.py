#!/usr/bin/env python3
"""Brazilian company check without captcha, from two public sources:
  - BrasilAPI (brasilapi.com.br, Receita Federal data): legal name, opening date, status, activity, capital, partners count;
  - DJEN, the CNJ national electronic court gazette (comunicaapi.pje.jus.br): court notices where the company is a party,
    searched by the exact legal name (the API searches by name, so namesakes are filtered out here);
  - DataJud, the CNJ public API (api-publica.datajud.cnj.jus.br, free public key): for each case number the DJEN found,
    subjects (e.g. employment relationship claims), filing date, instance and last movement, and whether it is closed.
    DataJud has no party names, so it only details cases the DJEN already found.

  company_check.py --cnpj 23673165000120 [--since 2020-01-01] [--save "<name in reputation.json>"] [--json]
  company_check.py --name "ACME TECNOLOGIA LTDA" [...]

The DJEN only has what was published there (old cases or cases under seal do not show). No personal names are printed:
the other parties of a case are often private individuals.
"""
import sys, os, re, json, time, argparse, unicodedata, urllib.request, urllib.parse, urllib.error
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, schema
from watch_core import fileio

UA = 'Mozilla/5.0 (job-scout)'
DJEN = 'https://comunicaapi.pje.jus.br/api/v1/comunicacao'
BRASILAPI = 'https://brasilapi.com.br/api/cnpj/v1/{}'
PER_PAGE, MAX_PAGES = 100, 10
DATAJUD_MAX = 15          # cases detailed in the DataJud per check (1 request each)
# the labor courts (TRT) only publish in the DJEN since 01/2025 (before: their own gazette, DEJT); older cases without new
# notices since then do not show up here
COVERAGE = ' (cobertura: processos trabalhistas só a partir de 2025; cíveis parcial antes disso; ausência não é prova)'
ROLE = {'P': 'ré', 'A': 'autora'}          # DJEN 'polo': P = passive (defendant), A = active; T/D = third parties
DATAJUD = 'https://api-publica.datajud.cnj.jus.br/api_publica_{}/_search'
DATAJUD_WIKI = 'https://datajud-wiki.cnj.jus.br/api-publica/acesso/'
# the public key the CNJ publishes on the wiki (may be rotated: on 401/403 it is read again from the wiki page)
DATAJUD_KEY = os.environ.get('DATAJUD_KEY') or 'cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=='
UF = '01AC 02AL 03AP 04AM 05BA 06CE 07DFT 08ES 09GO 10MA 11MT 12MS 13MG 14PA 15PB 16PR 17PE 18PI 19RJ 20RN 21RS 22RO 23RR 24SC 25SE 26SP 27TO'
UF = {x[:2]: x[2:].lower() for x in UF.split()}
CLOSED = re.compile(r'arquivamento|baixa definitiva|tr[aâ]nsito em julgado|extin[cç][aã]o', re.I)
EMPLOYMENT_LINK = re.compile(r'reconhecimento de rela[cç][aã]o de emprego|v[ií]nculo empregat|rela[cç][aã]o de emprego', re.I)
SUFFIX = re.compile(r'\b(LTDA|S ?A|S ?\/ ?A|EIRELI|ME|EPP|SOCIEDADE ANONIMA|LIMITADA)\b')

def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as r: return json.load(r)

def norm(name):
    """'Thunders Tecnologia Ltda.' -> 'THUNDERS TECNOLOGIA' (no accents, punctuation or company-type suffix)."""
    t = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().upper()
    t = re.sub(r'[^A-Z0-9/ ]', ' ', t)
    return re.sub(r'\s+', ' ', SUFFIX.sub(' ', t)).strip()

def registry(cnpj):
    d = _get(BRASILAPI.format(re.sub(r'\D', '', cnpj)))
    return {'legal_name': d.get('razao_social'), 'trade_name': d.get('nome_fantasia'), 'opened': d.get('data_inicio_atividade'),
            'status': d.get('descricao_situacao_cadastral'), 'activity': d.get('cnae_fiscal_descricao'),
            'city': f"{_city(d.get('municipio'))}/{d.get('uf') or ''}", 'capital': d.get('capital_social'),
            'partners': len(d.get('qsa') or []), 'cnpj': re.sub(r'\D', '', cnpj)}

def _city(c):
    return ' '.join(w if w in ('de', 'da', 'do', 'das', 'dos') else w.capitalize() for w in (c or '').lower().split())

def notices(name, since):
    """All DJEN notices whose party list contains a party with this name (paged; capped at MAX_PAGES)."""
    out, total = [], 0
    for page in range(1, MAX_PAGES + 1):
        q = urllib.parse.urlencode({'nomeParte': name, 'dataDisponibilizacaoInicio': since, 'dataDisponibilizacaoFim': date.today().isoformat(),
                                    'itensPorPagina': PER_PAGE, 'pagina': page})
        d = _get(f'{DJEN}?{q}'); items = d.get('items') or []; total = d.get('count') or 0
        out += items
        if len(items) < PER_PAGE or len(out) >= total: break
        time.sleep(1)
    return out, total

def datajud_alias(number):
    """'1000630-47.2026.5.02.0422' -> 'trt2' (segment J and court TR of the CNJ number); None for courts not mapped."""
    d = re.sub(r'\D', '', number or '')
    if len(d) != 20: return None
    j, tr = d[13], d[14:16]
    if j == '5': return f'trt{int(tr)}'
    if j == '4': return f'trf{int(tr)}'
    if j == '8' and tr in UF: return 'tj' + UF[tr]
    return None

def _datajud_post(alias, number):
    global DATAJUD_KEY
    body = json.dumps({'query': {'match': {'numeroProcesso': re.sub(r'\D', '', number)}}}).encode()
    for attempt in (1, 2):
        req = urllib.request.Request(DATAJUD.format(alias), data=body, headers={'Authorization': 'APIKey ' + DATAJUD_KEY,
                                     'Content-Type': 'application/json', 'User-Agent': UA})
        try:
            with urllib.request.urlopen(req, timeout=40) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403) or attempt == 2: raise
            page = urllib.request.urlopen(urllib.request.Request(DATAJUD_WIKI, headers={'User-Agent': UA}), timeout=30).read().decode('utf-8', 'ignore')
            m = re.search(r'APIKey\s+([A-Za-z0-9+/=]{20,})', page)
            if not m: raise
            DATAJUD_KEY = m.group(1)

def datajud(number):
    """Metadata of one case in the DataJud (all instances merged): subjects, filing date, instances, last movement, closed.
    None when the court is not mapped or the case is not indexed (the DataJud lags behind the gazette)."""
    alias = datajud_alias(number)
    if not alias: return None
    try: hits = (_datajud_post(alias, number).get('hits') or {}).get('hits') or []
    except Exception as e: return {'error': str(e)[:80]}
    if not hits: return None
    subjects, instances, filed, moves = [], [], [], []
    for h in hits:
        s = h.get('_source') or {}
        for a in s.get('assuntos') or []:
            n = a.get('nome') if isinstance(a, dict) else None
            if n and n not in subjects: subjects.append(n)
        if s.get('grau'): instances.append(s['grau'])
        if s.get('dataAjuizamento'): filed.append(str(s['dataAjuizamento'])[:8])
        moves += [(m.get('dataHora') or '', m.get('nome') or '') for m in s.get('movimentos') or []]
    moves.sort()
    last = moves[-1] if moves else ('', '')
    f = min(filed) if filed else ''
    return {'subjects': subjects, 'instances': sorted(set(instances)), 'filed': f'{f[:4]}-{f[4:6]}-{f[6:8]}' if f else '',
            'last_move': (last[0][:10], last[1]), 'closed': any(CLOSED.search(n) for _, n in moves),
            'employment_link': any(EMPLOYMENT_LINK.search(x) for x in subjects)}

def kind_of(court):
    court = court or ''
    if court.startswith(('TRT', 'TST')): return 'trabalhista'
    if court.startswith('TJ'): return 'cível/estadual'
    if court.startswith('TRF'): return 'federal'
    return 'outros'

def summarize(items, legal_name):
    """Distinct cases where the company itself is a party. Returns {'cases': [...], 'namesakes': n}."""
    target, cases, namesakes = norm(legal_name), {}, 0
    for i in items:
        mine = [p for p in (i.get('destinatarios') or []) if norm(p.get('nome')) == target]
        if not mine: namesakes += 1; continue
        n = i.get('numeroprocessocommascara') or i.get('numero_processo')
        c = cases.setdefault(n, {'number': n, 'court': i.get('siglaTribunal'), 'kind': kind_of(i.get('siglaTribunal')),
                                 'class': (i.get('nomeClasse') or '').capitalize(), 'role': set(), 'last': ''})
        for p in mine: c['role'].add(ROLE.get(p.get('polo'), 'outra parte'))
        c['last'] = max(c['last'], i.get('data_disponibilizacao') or '')
    for c in cases.values(): c['role'] = '/'.join(sorted(c['role']))
    return {'cases': sorted(cases.values(), key=lambda c: c['last'], reverse=True), 'namesakes': namesakes}

def sentences(reg, summ, since, capped):
    """The two one-line findings that go to reputation.json 'extra'."""
    out = {}
    if reg:
        cap = ', capital R$ ' + f"{reg['capital']:,.0f}".replace(',', '.') if reg.get('capital') else ''
        out['cnpj'] = (f"{reg['cnpj']}, {(reg['status'] or '?').lower()}, aberta em {'/'.join(reversed((reg['opened'] or '').split('-')))}, "
                       f"{reg['activity']}, {reg['city']}{cap}, {reg['partners']} sócios")
    cs = summ['cases']; ano = since[:4]
    if not cs:
        out['processos'] = f"nenhum processo no Diário de Justiça Eletrônico desde {ano} com o nome exato da empresa" + COVERAGE
    else:
        by = {}
        for c in cs: by[(c['kind'], c['role'])] = by.get((c['kind'], c['role']), 0) + 1
        parts = ', '.join(f"{n} {k} como {r}" for (k, r), n in sorted(by.items(), key=lambda x: -x[1]))
        out['processos'] = f"{len(cs)} processo(s) no Diário de Justiça Eletrônico desde {ano}: {parts}; mais recente {cs[0]['last']} ({cs[0]['court']}, {cs[0]['class'].lower()})" + COVERAGE
    if capped: out['processos'] += ' (lista truncada: muitas comunicações)'
    det = [c for c in cs if isinstance(c.get('datajud'), dict) and 'error' not in c['datajud']]
    if det:
        subj = {}
        for c in det:
            for x in c['datajud']['subjects']: subj[x] = subj.get(x, 0) + 1
        top = ', '.join(f'{x} ({n})' if n > 1 else x for x, n in sorted(subj.items(), key=lambda x: -x[1])[:4])
        closed = sum(1 for c in det if c['datajud']['closed']); link = sum(1 for c in det if c['datajud']['employment_link'])
        out['datajud'] = (f"{len(det)} de {len(cs)} detalhado(s) no DataJud: {len(det) - closed} em andamento, {closed} encerrado(s); assuntos: {top}"
                          + (f'; ATENÇÃO: {link} pedido(s) de reconhecimento de vínculo empregatício' if link else ''))
    return out

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cnpj'); ap.add_argument('--name'); ap.add_argument('--since', default='2020-01-01')
    ap.add_argument('--save', metavar='COMPANY', help='writes the findings into reputation.json extra of this company')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if not (a.cnpj or a.name): ap.error('informe --cnpj ou --name')
    reg = registry(a.cnpj) if a.cnpj else None
    legal = (reg or {}).get('legal_name') or a.name
    items, total = notices(legal, a.since)
    summ = summarize(items, legal)
    for c in summ['cases'][:DATAJUD_MAX]:
        c['datajud'] = datajud(c['number']); time.sleep(0.5)
    found = sentences(reg, summ, a.since, total > len(items))
    if a.json:
        print(json.dumps({'registry': reg, **summ, 'found': found}, ensure_ascii=False, indent=1)); return
    print(f'== EMPRESA {legal}' + (f" ({reg['trade_name']})" if reg and reg.get('trade_name') else ''))
    for k, v in found.items(): print(f'   {k}: {v}')
    for c in summ['cases'][:8]:
        dj = c.get('datajud') or {}
        extra = (f" · {', '.join(dj['subjects'][:3])} · ajuizado {dj['filed']} · {'/'.join(dj['instances'])} · último: {dj['last_move'][0]} {dj['last_move'][1]}"
                 + (' · ENCERRADO' if dj['closed'] else '') if dj and 'error' not in dj else (' · DataJud: ' + dj['error'] if dj else ''))
        print(f"   - {c['last']} {c['court']} {c['number']} · {c['class']} · {c['role']}{extra}")
    if summ['namesakes']: print(f"   ({summ['namesakes']} comunicação(ões) de outras empresas com nome parecido, ignoradas)")
    if a.save:
        rep = schema.reputation(json.load(open(paths.REPUTATION, encoding='utf-8'))) if os.path.exists(paths.REPUTATION) else {}
        e = rep.setdefault(a.save, {}); e.setdefault('extra', {}).update(found)
        e['date'] = date.today().isoformat()
        fileio.write_json(paths.REPUTATION, rep, indent=1, ensure_ascii=False)
        print(f'   (gravado em reputation.json: {a.save})')

if __name__ == '__main__':
    main()
