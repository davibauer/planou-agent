"""Decisoes marcadas no Notion (caixinhas na entrada da daily) — copia unica para todos os agents de trabalho.

Pedido do usuario em 21/09/2026: as opcoes de cada decisao ficam como to-dos aninhados no proprio item da daily
(`- [ ] a3-A Pedir a aprovacao…`); ele marca a caixinha ali mesmo e o agent le no tick seguinte. Leitura pela REST
do Notion com o token de uma integracao interna em ~/.config/watch-core/secrets/notion.env (NOTION_TOKEN=secret_…; a pagina
Dailies precisa estar compartilhada com a integracao). Sem token, o agente confere pelo conector (fetch da pagina)
no maximo a cada 30 min — caro, por isso o token e' o caminho.
"""
import json, os, re, urllib.request

from . import config

ENV = config.file(config.NOTION_ENV)
API = 'https://api.notion.com/v1'
RE_COD = re.compile(r'^([ap]\d+)-([A-Z])\b')   # `a3-A` (chave interna) ou `p3-A` (codigo da daily, o que vai na pagina)

def token():
    try:
        for l in open(ENV):
            if l.startswith('NOTION_TOKEN='): return l.split('=', 1)[1].strip().strip('"\'')
    except FileNotFoundError: pass
    return os.environ.get('NOTION_TOKEN')

def _get(url, tok):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {tok}', 'Notion-Version': '2022-06-28'})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())

def _texto(b):
    t = b.get(b['type'], {}).get('rich_text') or []
    return ''.join(x.get('plain_text', '') for x in t)

def _filhos(bid, tok):
    out, cursor = [], None
    while True:
        u = f'{API}/blocks/{bid}/children?page_size=100' + (f'&start_cursor={cursor}' if cursor else '')
        r = _get(u, tok); out += r.get('results', [])
        if not r.get('has_more'): return out
        cursor = r['next_cursor']

def marcadas(pagina_id, tok=None, so_codigos=None, empresa=None):
    """To-dos marcados cujo texto comeca com `aN-X`/`pN-X`. Com `empresa` ('AUR', 'TKB'...), SO dentro das secoes
    `## <empresa>` da pagina (ate o proximo heading): os codigos se repetem entre os agents — cada um tem o seu a3/p3 —
    e um `a3-C` marcado na secao BRV foi lido como resposta do AUR em 21/09/2026. Sem `empresa`, pagina inteira (legado).
    Devolve [{'acao': 'a3', 'opcao': 'A', 'texto': ..., 'block': id}]. Percorre um nivel de filhos dos bullets."""
    tok = tok or token()
    if not tok: return None
    achados, dentro = [], empresa is None
    for b in _filhos(pagina_id, tok):
        if empresa and b['type'] in ('heading_1', 'heading_2', 'heading_3'):
            dentro = _texto(b).strip().upper() == empresa.upper(); continue
        if not dentro: continue
        blocos = [b]
        if b.get('has_children') and b['type'] in ('bulleted_list_item', 'numbered_list_item', 'to_do', 'paragraph'):
            blocos += _filhos(b['id'], tok)
        for x in blocos:
            if x['type'] != 'to_do' or not x['to_do'].get('checked'): continue
            m = RE_COD.match(_texto(x).strip())
            if not m: continue
            if so_codigos and m.group(1) not in so_codigos: continue
            achados.append({'acao': m.group(1), 'opcao': m.group(2), 'texto': _texto(x).strip(), 'block': x['id']})
    return achados

if __name__ == '__main__':
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else config.get('notion.month_page')
    if not pid: sys.exit('usage: python3 -m watch_core.notion_decisions <page_id> (or notion.month_page in config.json)')
    r = marcadas(pid)
    print(f'sem token em {ENV}' if r is None else json.dumps(r, ensure_ascii=False, indent=1))


RE_ITEM = re.compile(r'^`?([ptwb]\d+)`?(?![-\w])')

def concluidos(pagina_id, empresa, tok=None):
    """To-dos de PRIMEIRO nivel marcados na secao `## <empresa>` cujo texto comeca com um codigo de item (p1, w2, t1, b1):
    o usuario marcou = "fiz isso". Devolve [{'codigo', 'texto', 'block', 'checked'}] — inclui os desmarcados tambem
    (checked False) para o verificador saber o que ja reabriu."""
    tok = tok or token()
    if not tok: return None
    out, dentro = [], False
    for b in _filhos(pagina_id, tok):
        if b['type'] in ('heading_1', 'heading_2', 'heading_3'):
            dentro = _texto(b).strip().upper() == empresa.upper(); continue
        if not dentro or b['type'] != 'to_do': continue
        m = RE_ITEM.match(_texto(b).strip())
        if m: out.append({'codigo': m.group(1), 'texto': _texto(b).strip(), 'block': b['id'], 'checked': bool(b['to_do'].get('checked')),
                          'rich': b['to_do'].get('rich_text') or []})   # rich_text original: reescrever preservando negrito/link/codigo
    return out
