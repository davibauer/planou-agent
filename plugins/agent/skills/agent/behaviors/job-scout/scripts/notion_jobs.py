#!/usr/bin/env python3
"""Database "Vagas — job-scout" in Notion (REST, integration token in ~/.config/job-scout/secrets/notion.env, else the
one of watch_core that writes the daily page: notion_min.token). SIMPLE, so it does not become noise: 7 columns and only the jobs worth attention —
status `interested` or a pipeline stage (applied…lost). Discarded ones do not go in; when it turns `ignored` the row is archived. The tick syncs
what changed (creates/updates; the page id lives in state.jobs.tracked[id].notion) and reads the Pedido column (the way
back: the user marks detalhar/candidatura/aplicar/descartar on the row; the scout executes it and clears it).

  notion_jobs.py --create            # creates the database inside Dailies (once); stores the id in state.notion_jobs.db
  notion_jobs.py --sync [--all]    # pushes the annotated jobs that changed (or all of them)
  notion_jobs.py --requests          # rows with Pedido filled in
  notion_jobs.py --clear-request <page_id>
"""
import os, sys, re, json, argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from notion_min import _req, token, PARENT as PARENT_PAGE      # parent page of the database: NOTION_PARENT_PAGE

import paths, schema
from watch_core import fileio
STATE = paths.STATE
API = 'https://api.notion.com/v1'
STATUS = list(schema.STATUS.values())   # English stored values, in pipeline order; Notion shows schema.label(status)
FIT = ['forte', 'médio-forte', 'médio', 'médio-fraco', 'fraco', 'fora']
FORMATS = ['PJ', 'CLT', 'contractor', 'hourly', 'freelance', 'remoto', 'híbrido', 'presencial']
SOURCES = ['busca', 'recomendadas', 'gmail', 'mercado', 'empresas']
REQUEST_OPTIONS = ['detalhar', 'candidatura', 'aplicar', 'descartar']
COLOR = {'forte': 'green', 'médio-forte': 'blue', 'médio': 'yellow', 'médio-fraco': 'orange', 'fraco': 'gray', 'fora': 'red',
         # Status colors are keyed by the Portuguese LABEL (the Notion option name)
         'interessa': 'blue', 'aplicado': 'purple', 'respondeu': 'green', 'entrevista': 'green', 'proposta': 'green', 'fechado': 'green', 'perdido': 'red', 'ignorar': 'gray'}
PUBLISH = {'forte', 'médio-forte', 'médio', 'médio-fraco'}   # fits that become a row; pipeline status (applied+) always

def load(): return json.load(open(STATE))
def save(s):
    fileio.write_json(STATE, s, indent=1, ensure_ascii=False)

def fit_from_note(note):
    m = re.match(r'\s*(forte|médio-forte|medio-forte|médio|medio|médio-fraco|medio-fraco|fraco|fora|duplicata)\b', note or '', re.I)
    e = (m.group(1).lower().replace('medio', 'médio') if m else '')
    return 'fora' if e == 'duplicata' else e

def sel(name): return {'select': {'name': name}} if name else {'select': None}
def txt(s): return {'rich_text': [{'type': 'text', 'text': {'content': (s or '')[:1900]}}]}

# columns added after the database was created (ensure_columns creates the missing ones, once)
NEW_COLUMNS = {'Publicada por': {'rich_text': {}}, 'Fonte': {'select': {'options': [{'name': n} for n in SOURCES]}}}
# the 'Nota do vigia' column keeps its old name on purpose: existing databases already have it
NOTION_SOURCE = {'linkedin': 'busca', 'remoteok': 'mercado', 'wwr': 'mercado', 'web3career': 'mercado', 'mercado': 'mercado'}

def ensure_columns(s, tok):
    """Database created before a new column existed: adds the column (once; marks it in notion_jobs.columns)."""
    nv = s.setdefault('notion_jobs', {})
    missing = {k: v for k, v in NEW_COLUMNS.items() if k not in (nv.get('columns') or [])}
    if not missing or not nv.get('db'): return
    _req('PATCH', f'{API}/databases/{nv["db"]}', tok, {'properties': missing})
    nv['columns'] = sorted(set(nv.get('columns') or []) | set(missing))

def create(tok):
    if not PARENT_PAGE: raise RuntimeError('defina NOTION_PARENT_PAGE (id da pagina compartilhada com a integracao) em ~/.config/job-scout/secrets/notion.env')
    body = {'parent': {'type': 'page_id', 'page_id': PARENT_PAGE}, 'title': [{'type': 'text', 'text': {'content': 'Vagas — job-scout'}}],
            'properties': {
                'Vaga': {'title': {}},
                'Status': {'select': {'options': [{'name': schema.label(n), 'color': COLOR[schema.label(n)]} for n in STATUS]}},
                'Encaixe': {'select': {'options': [{'name': n, 'color': COLOR[n]} for n in FIT]}},
                'Formato': {'multi_select': {'options': [{'name': n} for n in FORMATS]}},
                'Pedido': {'select': {'options': [{'name': n, 'color': 'red'} for n in REQUEST_OPTIONS]}},
                'Nota do vigia': {'rich_text': {}}, 'Link': {'url': {}},
                'Faixa': {'rich_text': {}}, 'Match': {'rich_text': {}}, 'Candidatura': {'url': {}}, **NEW_COLUMNS}}
    d = _req('POST', f'{API}/databases', tok, body)
    return d['id'], d.get('url')

def props(jid, v):
    st = v.get('status', 'interested'); fit = fit_from_note(v.get('note', ''))
    fmt = [f for f in (v.get('work_format') or []) if f in FORMATS]
    if v.get('remote') and 'remoto' not in fmt: fmt.append('remoto')
    note = re.sub(r'^\s*(forte|médio-forte|medio-forte|médio|medio|médio-fraco|medio-fraco|fraco|fora)\s*[:(]?\s*', '', v.get('note') or '', flags=re.I)
    if v.get('top'): note = 'TOP APPLICANT · ' + note
    if v.get('closure'): note = f"VAGA {v['closure']['kind'].upper()} NO LINKEDIN em {v['closure']['when']} · " + note
    if v.get('location'): note = f"{v['location']} · " + note
    p = {'Vaga': {'title': [{'type': 'text', 'text': {'content': f"{v.get('title') or '?'} — {v.get('company') or '?'}"[:200]}}]},
         'Status': sel(schema.label(st)), 'Encaixe': sel(fit), 'Formato': {'multi_select': [{'name': f} for f in fmt]},
         'Nota do vigia': txt(note), 'Link': {'url': v.get('url') or f'https://www.linkedin.com/jobs/view/{jid}'}}
    if v.get('match'): p['Match'] = txt(v['match'])        # requirement x experience fit (matrix.py)
    elif v.get('quick_match'): p['Match'] = txt(f"~{v['quick_match'][0]}% (rápido)")
    f = NOTION_SOURCE.get(v.get('source'), v.get('source'))
    if f in SOURCES: p['Fonte'] = sel(f)
    rc = v.get('recruiter')
    if rc: p['Publicada por'] = {'rich_text': [{'type': 'text', 'text': {'content': f"{rc['name']} ({rc.get('headline', '')})"[:200],
                                                                          'link': {'url': rc['profile']} if rc.get('profile') else None}}]}
    if v.get('pay_range'): p['Faixa'] = txt(v['pay_range'])        # pay range: posted or estimated ("pay_ranges" key of config.json)
    if v.get('cv_gdoc'): p['Candidatura'] = {'url': v['cv_gdoc']}      # targeted CV in Google Docs (cv_docx.py + Drive connector)
    return p

ICON = {'sim': '✅', 'parcial': '🟡', 'nao': '❌'}

def _rt(t): return [{'type': 'text', 'text': {'content': (t or '-')[:1900]}}]

def matrix_blocks(v):
    """Fit matrix (matrix.py) as page blocks: heading, requirement x experience table and the score."""
    rows = [['#', 'Requisito da vaga', 'Você', 'Evidência (onde fez)', 'Preparar']]
    for k, i in enumerate(v['matrix'], 1):
        rows.append([str(k), i['req'], ICON.get(i['level'], '?'), i.get('evidence', ''), i.get('study', '')])
    return [
        {'object': 'block', 'type': 'heading_2', 'heading_2': {'rich_text': _rt('Matriz de aderência (requisito x experiência)')}},
        {'object': 'block', 'type': 'table', 'table': {'table_width': 5, 'has_column_header': True, 'has_row_header': False,
            'children': [{'object': 'block', 'type': 'table_row', 'table_row': {'cells': [_rt(c) for c in l]}} for l in rows]}},
        {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': _rt(f"Aderência: {v.get('match', '')} (✅ = 1, 🟡 = 0,5, ❌ = 0). Os 🟡/❌ são o roteiro do que revisar antes da entrevista.")}},
    ]

def application_blocks(v):
    """Page 'Candidatura' section: CV in Google Docs, PDF, CV sent, interview script, who posted."""
    items = []
    def li(label, text, url=None):
        rt = [{'type': 'text', 'text': {'content': f'{label}: '}, 'annotations': {'bold': True}},
              {'type': 'text', 'text': {'content': (text or '')[:1800], 'link': {'url': url} if url else None}}]
        items.append({'object': 'block', 'type': 'bulleted_list_item', 'bulleted_list_item': {'rich_text': rt}})
    rc = v.get('recruiter')
    if rc: li('Quem publicou', f"{rc['name']} ({rc.get('headline', '')})", rc.get('profile') or None)
    if v.get('cv_gdoc'): li('CV no Google Docs', v['cv_gdoc'], v['cv_gdoc'])
    if v.get('cv_pdf'): li('PDF para anexar', v['cv_pdf'])
    if v.get('cv_sent'): li('CV enviado pelo LinkedIn', v['cv_sent'])
    if v.get('prep'): li('Roteiro de entrevista', v['prep'])
    if v.get('quick_match') and v['quick_match'][1]: li('Match rápido: falta', ', '.join(v['quick_match'][1]))
    if not items: return []
    return [{'object': 'block', 'type': 'heading_2', 'heading_2': {'rich_text': _rt('Candidatura')}}] + items

def sync_matrix(v, tok):
    """Writes/rewrites the job page body (Candidatura section + matrix) when something in it changes.
    The previous blocks (ids in v['notion_matrix_blocks']) are deleted first; the rest of the page is not touched."""
    if not v.get('notion'): return False
    blocks = application_blocks(v) + (matrix_blocks(v) if v.get('matrix') else [])
    if not blocks: return False
    mark = schema.page_mark(v)
    if v.get('notion_matrix_mark') == mark: return False
    for b in v.get('notion_matrix_blocks') or []:
        try: _req('DELETE', f'{API}/blocks/{b}', tok)
        except Exception: pass
    r = _req('PATCH', f'{API}/blocks/{v["notion"]}/children', tok, {'children': blocks})
    v['notion_matrix_blocks'] = [b['id'] for b in r.get('results', [])]; v['notion_matrix_mark'] = mark
    return True

def is_publishable(v):
    return v.get('status') in STATUS[:7]          # interested + pipeline; ignored stays out

def sync(s, tok, everything=False):
    db = (s.get('notion_jobs') or {}).get('db')
    if not db: raise RuntimeError('database nao criada: notion_jobs.py --create')
    an = (s.get('jobs') or {}).get('tracked') or {}
    ensure_columns(s, tok)
    done = []
    for jid, v in an.items():
        if not is_publishable(v):
            if v.get('notion'):                    # left the pipeline (turned ignored): archive the row
                _req('PATCH', f'{API}/pages/{v["notion"]}', tok, {'archived': True}); v.pop('notion', None); v.pop('notion_mark', None); done.append(('arquivada', jid))
            continue
        mark = schema.row_mark(v)
        if not everything and v.get('notion') and v.get('notion_mark') == mark: continue
        p = props(jid, v)
        if v.get('notion'):
            _req('PATCH', f'{API}/pages/{v["notion"]}', tok, {'properties': p}); done.append(('atualizada', jid))
        else:
            r = _req('POST', f'{API}/pages', tok, {'parent': {'database_id': db}, 'properties': p}); v['notion'] = r['id']; done.append(('criada', jid))
        v['notion_mark'] = mark
    for jid, v in an.items():
        if is_publishable(v) and sync_matrix(v, tok): done.append(('pagina', jid))
    return done

def requests(s, tok):
    db = (s.get('notion_jobs') or {}).get('db')
    if not db: return []
    r = _req('POST', f'{API}/databases/{db}/query', tok, {'filter': {'property': 'Pedido', 'select': {'is_not_empty': True}}})
    out = []
    for pg in r.get('results', []):
        pr = pg['properties']
        link = pr.get('Link', {}).get('url') or ''
        m = re.search(r'jobs/view/(\d+)', link)
        out.append({'page': pg['id'], 'request': (pr['Pedido'].get('select') or {}).get('name'),
                    'id': m.group(1) if m else link, 'job': ''.join(t['plain_text'] for t in pr['Vaga'].get('title', []))})
    return out

def clear_request(page, tok):
    _req('PATCH', f'{API}/pages/{page}', tok, {'properties': {'Pedido': {'select': None}}})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--create', '--criar', dest='criar', action='store_true'); ap.add_argument('--sync', action='store_true'); ap.add_argument('--all', '--tudo', dest='tudo', action='store_true')
    ap.add_argument('--requests', '--pedidos', dest='pedidos', action='store_true'); ap.add_argument('--clear-request', '--limpar-pedido', dest='limpar_pedido', metavar='PAGE')
    a = ap.parse_args(); tok = token(); s = load()
    if a.criar:
        if (s.get('notion_jobs') or {}).get('db'): sys.exit(f'ja existe: {s["notion_jobs"]["db"]}')
        db, url = create(tok); s['notion_jobs'] = {'db': db, 'url': url}; save(s); print(f'database criada: {url}'); return
    if a.sync:
        f = sync(s, tok, a.tudo); save(s)
        print(f'sync: {len(f)} linhas (' + ', '.join(f'{k} {j}' for k, j in f[:10]) + ('…' if len(f) > 10 else '') + ')' if f else 'sync: nada mudou'); return
    if a.pedidos:
        for p in requests(s, tok): print(f'-- {p["request"]:12s} {p["job"]} · id {p["id"]} · page {p["page"]}')
        return
    if a.limpar_pedido: clear_request(a.limpar_pedido, tok); print('limpo'); return
    ap.print_help()

if __name__ == '__main__':
    main()
