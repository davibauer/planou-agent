#!/usr/bin/env python3
"""Publicacao da daily no Notion pela REST (token da integracao em ~/.config/watch-core/secrets/notion.env) — copia unica, todos os agents de trabalho.

Pedido de 21/09/2026 ("faca a 1 e a 2"): (1) uma SUBPAGINA POR DIA dentro da pagina do mes — `Dailies / 2026/09 /
2026-09-22 (ter)` — em vez de tudo na pagina do mes (o fetch da pagina do mes ja custava ~15k tokens e crescia o mes
inteiro); (2) edicao POR BLOCO, nao por texto: a secao `## <SIGLA>` da pagina do dia e' reescrita trocando os blocos
(delete + append `after` o heading), sem `update_content` por `old_str` (o Notion reescreve formatacao e o casamento de
texto falhava). Os block ids da secao ficam no estado (`daily.notion`), mas sao sempre reconferidos pela pagina.

Markdown aceito (o subconjunto que a daily usa): paragrafo, `**Bloco**` em linha propria (vira paragrafo em negrito),
`- item` (bullet), `\\t- [ ] opcao` / `\\t- [x]` (to-do aninhado no bullet anterior), `## Titulo` (heading_2). `▸ Titulo` = toggle, com os `\\t- …` seguintes como filhos (23/09/2026).
Rich text: **negrito**, *italico*, `codigo`, [texto](url). Linhas em branco separam blocos.

CLI (debug):
  notion_daily.py dia 2026-09-22                    # id da pagina do dia (cria se nao existir)
  notion_daily.py secao 2026-09-22 AUR              # blocos atuais da secao
  notion_daily.py publicar 2026-09-22 AUR arq.md    # reescreve a secao com o markdown do arquivo
"""
import json, os, re, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

from . import config
from .notion_decisions import token, API, _get, _filhos, _texto

MES_DEFAULT = config.get('notion.month_page')        # page of the current month (Dailies / YYYY/MM), config.json
DAILIES = config.get('notion.dailies_page')          # parent page of the months, config.json
BRT = timezone(timedelta(hours=-3))
DIAS = ['seg', 'ter', 'qua', 'qui', 'sex', 'sab', 'dom']
HEAD = {'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}


def _req(method, url, tok, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**HEAD, 'Authorization': f'Bearer {tok}'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Notion {method} {url.split("/v1/")[-1]}: HTTP {e.code} {e.read()[:300].decode(errors="replace")}')


# ---------------- markdown -> blocos ----------------
RT = re.compile(r'(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+`|\[[^\]]+\]\([^)]+\))')

def rich(texto):
    """Markdown em linha -> rich_text do Notion (negrito, italico, codigo, link; negrito+codigo nao aninha)."""
    out = []
    for part in RT.split(texto):
        if not part: continue
        m = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', part)
        if m:
            inner = m.group(1); ann = {}
            if inner.startswith('**') and inner.endswith('**'): inner = inner[2:-2]; ann['bold'] = True
            out.append({'type': 'text', 'text': {'content': inner, 'link': {'url': m.group(2)}}, 'annotations': ann}); continue
        if part.startswith('**') and part.endswith('**') and len(part) > 4:
            inner = part[2:-2]
            if inner.startswith('`') and inner.endswith('`'): out.append({'type': 'text', 'text': {'content': inner[1:-1]}, 'annotations': {'bold': True, 'code': True}})
            else: out.append({'type': 'text', 'text': {'content': inner}, 'annotations': {'bold': True}})
        elif part.startswith('`') and part.endswith('`') and len(part) > 2:
            out.append({'type': 'text', 'text': {'content': part[1:-1]}, 'annotations': {'code': True}})
        elif part.startswith('*') and part.endswith('*') and len(part) > 2:
            out.append({'type': 'text', 'text': {'content': part[1:-1]}, 'annotations': {'italic': True}})
        else:
            out.append({'type': 'text', 'text': {'content': part}})
    # o Notion limita 2000 chars por rich_text; a daily nao chega perto
    return out or [{'type': 'text', 'text': {'content': ''}}]


def blocos(md):
    """Markdown (subconjunto) -> lista de blocos; `\\t- [ ] x` vira to_do filho do ultimo bullet."""
    out = []
    for ln in md.splitlines():
        if not ln.strip(): continue
        # toggle (23/09/2026, backlog recolhido): `▸ Titulo` e os filhos `\t- item` / `\t- [ ] item` logo abaixo
        if ln.startswith('▸ '):
            out.append({'object': 'block', 'type': 'toggle', 'toggle': {'rich_text': rich(ln[2:]), 'children': []}}); continue
        if out and out[-1]['type'] == 'toggle' and re.match(r'^(\t| {2,4})- ', ln):
            m = re.match(r'^(?:\t| {2,4})- \[( |x|X)\] (.*)$', ln)
            filho = ({'object': 'block', 'type': 'to_do', 'to_do': {'rich_text': rich(m.group(2)), 'checked': m.group(1).lower() == 'x'}} if m
                     else {'object': 'block', 'type': 'bulleted_list_item', 'bulleted_list_item': {'rich_text': rich(re.sub(r'^(\t| {2,4})- ', '', ln))}})
            out[-1]['toggle']['children'].append(filho); continue
        m = re.match(r'^(\t| {2,4})- \[( |x|X)\] (.*)$', ln)
        if m and out and out[-1]['type'] == 'bulleted_list_item':
            out[-1]['bulleted_list_item'].setdefault('children', []).append(
                {'object': 'block', 'type': 'to_do', 'to_do': {'rich_text': rich(m.group(3)), 'checked': m.group(2).lower() == 'x'}})
            continue
        m = re.match(r'^- \[( |x|X)\] (.*)$', ln)
        if m: out.append({'object': 'block', 'type': 'to_do', 'to_do': {'rich_text': rich(m.group(2)), 'checked': m.group(1).lower() == 'x'}}); continue
        if ln.startswith('- '): out.append({'object': 'block', 'type': 'bulleted_list_item', 'bulleted_list_item': {'rich_text': rich(ln[2:])}}); continue
        m = re.match(r'^(#{1,3}) (.*)$', ln)
        if m:
            h = f'heading_{len(m.group(1))}'; out.append({'object': 'block', 'type': h, h: {'rich_text': rich(m.group(2))}}); continue
        out.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich(ln)}})
    return out


# ---------------- paginas ----------------
def titulo_dia(data):
    d = datetime.strptime(data, '%Y-%m-%d')
    return f'{data} ({DIAS[d.weekday()]})'


_MESES = {}


def pagina_mes(data, tok=None, criar=True):
    """Id da pagina do mes de `data` (`AAAA/MM`, filha de notion.dailies_page); cria se faltar. Sem dailies_page no
    config, notion.month_page (fixa). 25/09/2026: a pagina do mes era fixa (setembro) e em 01/10 a do dia nao nasceria."""
    if not DAILIES: return MES_DEFAULT
    titulo = f'{data[:4]}/{data[5:7]}'
    if titulo in _MESES: return _MESES[titulo]
    tok = tok or token()
    for b in _filhos(DAILIES, tok):
        if b['type'] == 'child_page' and b['child_page']['title'].strip() == titulo:
            _MESES[titulo] = b['id']; return b['id']
    if not criar: return None
    r = _req('POST', f'{API}/pages', tok, {'parent': {'page_id': DAILIES}, 'properties': {'title': [{'text': {'content': titulo}}]}})
    _MESES[titulo] = r['id']
    return r['id']


def pagina_do_dia(data, mes=None, tok=None, criar=True):
    """Id da subpagina `AAAA-MM-DD (dia)` dentro da pagina do mes; cria (vazia) se nao existir. `mes` vazio ou igual ao
    padrao do config = a pagina do mes de `data` (pagina_mes); um id diferente (--daily page <id>) vale como esta."""
    tok = tok or token()
    if not mes or mes == MES_DEFAULT:
        mes = pagina_mes(data, tok, criar)
        if not mes: return None
    for b in _filhos(mes, tok):
        if b['type'] == 'child_page' and b['child_page']['title'].startswith(data): return b['id']
    if not criar: return None
    r = _req('POST', f'{API}/pages', tok, {'parent': {'page_id': mes}, 'properties': {'title': [{'text': {'content': titulo_dia(data)}}]}})
    return r['id']


def secao(pagina, sigla, tok=None):
    """(heading_id, [blocos da secao ate o proximo heading_2]) ou (None, [])."""
    tok = tok or token()
    head, corpo, dentro = None, [], False
    for b in _filhos(pagina, tok):
        if b['type'] == 'heading_2':
            if dentro: break
            if _texto(b).strip().upper() == sigla.upper(): head = b['id']; dentro = True
            continue
        if dentro: corpo.append(b)
    return head, corpo


def _antes_das_ultimas(pagina, sigla, tok):
    """Secao nova de uma sigla que NAO esta em notion.last_sections (config) entra antes da primeira que esta (25/09/2026:
    a LKD, da busca de vaga, fica no fim da pagina, depois das secoes dos clientes, por causa do compartilhamento de tela).
    Devolve o id do bloco depois do qual inserir, ou None (fim da pagina)."""
    ultimas = [x.upper() for x in (config.get('notion.last_sections') or [])]
    if not ultimas or sigla.upper() in ultimas: return None
    prev = None
    for b in _filhos(pagina, tok):
        if b['type'] == 'heading_2' and _texto(b).strip().upper() in ultimas: return prev
        prev = b['id']
    return None


def publicar(pagina, sigla, md, tok=None, carimbo=True):
    """Reescreve a secao `## <sigla>`: cria o heading se faltar, apaga os blocos atuais, insere os novos logo apos o
    heading. Primeira linha = `*Atualizado em dd/mm HH:MM*` (regra de 21/09). Devolve {'heading': id, 'blocos': n}."""
    tok = tok or token()
    if not tok: raise RuntimeError(f'sem NOTION_TOKEN em {config.NOTION_ENV}')
    head, corpo = secao(pagina, sigla, tok)
    if head is None:
        corpo_req = {'children': [{'object': 'block', 'type': 'heading_2', 'heading_2': {'rich_text': rich(sigla)}}]}
        antes = _antes_das_ultimas(pagina, sigla, tok)
        if antes: corpo_req['after'] = antes
        r = _req('PATCH', f'{API}/blocks/{pagina}/children', tok, corpo_req)
        head = r['results'][0]['id']
    # visoes vinculadas (child_database) ficam: sao as listas da base Tarefas (instancia piloto, 23/09/2026), e o texto novo entra
    # logo apos o heading, entao elas seguem no fim da secao
    # (com o rotulo heading_3 logo antes de cada uma: do primeiro rotulo/visao em diante nada e' apagado)
    i = next((k for k, b in enumerate(corpo) if b['type'] == 'child_database'), len(corpo))
    if i and corpo[i - 1]['type'] == 'heading_3': i -= 1
    for b in corpo[:i]: _req('DELETE', f'{API}/blocks/{b["id"]}', tok)
    if carimbo: md = f'*Atualizado em {datetime.now(BRT).strftime("%d/%m %H:%M")}*\n' + md
    novos = blocos(md)
    # o Notion aceita ate 100 filhos por chamada; `after` insere logo depois do heading (na ordem)
    ultimo = head; n = 0
    for i in range(0, len(novos), 100):
        lote = novos[i:i + 100]
        r = _req('PATCH', f'{API}/blocks/{pagina}/children', tok, {'children': lote, 'after': ultimo})
        res = r.get('results', [])[:len(lote)]       # com `after` o Notion devolve tambem os irmaos seguintes; so os novos contam
        n += len(res)
        if res: ultimo = res[-1]['id']
    return {'heading': head, 'blocos': n}


def _chave_rich(rt):
    """Forma comparavel de um rich_text (texto + anotacoes que usamos + link) — do bloco lido OU do gerado por rich()."""
    out = []
    for t in rt or []:
        txt = (t.get('text') or {}).get('content', t.get('plain_text', ''))
        ann = t.get('annotations') or {}
        link = ((t.get('text') or {}).get('link') or {}).get('url') or t.get('href')
        if link:                                   # o Notion devolve a URL re-codificada (%2F <-> /): compara decodificada
            from urllib.parse import unquote; link = unquote(link).rstrip('/')   # e o Notion poe "/" no fim de URL sem caminho
        out.append((txt, bool(ann.get('bold')), bool(ann.get('italic')), bool(ann.get('code')), link or None))
    # junta pedacos vizinhos iguais em estilo (o Notion as vezes divide/une trechos)
    j = []
    for x in out:
        if j and j[-1][1:] == x[1:]: j[-1] = (j[-1][0] + x[0],) + x[1:]
        else: j.append(x)
    return tuple(j)


def _chave_bloco(b, tok=None):
    t = b['type']; c = b.get(t) or {}
    filhos = None
    if t == 'toggle':                    # o conteudo do backlog muda sem o titulo mudar: compara os filhos
        fs = c.get('children') if 'children' in c else (_filhos(b['id'], tok) if b.get('id') and b.get('has_children') else [])
        filhos = tuple(_chave_bloco(f, tok) for f in fs or [])
    return (t, _chave_rich(c.get('rich_text')), bool(c.get('checked')) if t == 'to_do' else None,
            bool(b.get('has_children') or c.get('children')), filhos)


CARIMBO = 'Atualizado em '


def publicar_diff(pagina, sigla, md, tok=None):
    """Como `publicar`, mas mexe SO nos blocos que mudaram (23/09/2026): compara a secao atual com o markdown bloco a bloco
    (difflib), faz PATCH no bloco do mesmo tipo com texto novo, DELETE no que sobrou e insere o que falta no lugar certo.
    Nada mudou = 0 chamadas de escrita (o carimbo "Atualizado em" so e' trocado quando algo mudou). Bloco com filhos e
    child_database nao entram no diff (filhos: recria; child_database: fica). Devolve {'heading', 'mudancas'}."""
    import difflib
    tok = tok or token()
    head, corpo = secao(pagina, sigla, tok)
    if head is None: return {**publicar(pagina, sigla, md, tok), 'mudancas': -1}
    corpo = [b for b in corpo if b['type'] != 'child_database']
    carimbo = corpo[0] if corpo and corpo[0]['type'] == 'paragraph' and _texto(corpo[0]).startswith(CARIMBO) else None
    atuais = corpo[1:] if carimbo else corpo
    novos = blocos(md)
    ka, kn = [_chave_bloco(b, tok) for b in atuais], [_chave_bloco(b, tok) for b in novos]
    ops = difflib.SequenceMatcher(a=ka, b=kn, autojunk=False).get_opcodes()
    if all(o[0] == 'equal' for o in ops) and carimbo: return {'heading': head, 'mudancas': 0}
    n = 0
    ultimo = carimbo['id'] if carimbo else head          # id depois do qual o proximo bloco novo entra
    def insere(lote):
        nonlocal ultimo, n
        for i in range(0, len(lote), 100):
            r = _req('PATCH', f'{API}/blocks/{pagina}/children', tok, {'children': lote[i:i + 100], 'after': ultimo})
            res = r.get('results', [])[:len(lote[i:i + 100])]
            if res: ultimo = res[-1]['id']
            n += len(res)
    for tag, a0, a1, b0, b1 in ops:
        if tag == 'equal':
            ultimo = atuais[a1 - 1]['id']; continue
        velhos, vindos = atuais[a0:a1], novos[b0:b1]
        # pares do mesmo tipo, sem filhos: PATCH no lugar
        k = 0
        while k < min(len(velhos), len(vindos)) and velhos[k]['type'] == vindos[k]['type'] and \
                not velhos[k].get('has_children') and not (vindos[k][vindos[k]['type']].get('children')):
            t = vindos[k]['type']
            _req('PATCH', f'{API}/blocks/{velhos[k]["id"]}', tok, {t: vindos[k][t]}); n += 1
            ultimo = velhos[k]['id']; k += 1
        for b in velhos[k:]: _req('DELETE', f'{API}/blocks/{b["id"]}', tok); n += 1
        if vindos[k:]: insere(vindos[k:])
    stamp = {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich(f'*{CARIMBO}{datetime.now(BRT).strftime("%d/%m %H:%M")}*')}}
    if carimbo: _req('PATCH', f'{API}/blocks/{carimbo["id"]}', tok, {'paragraph': stamp['paragraph']})
    else: _req('PATCH', f'{API}/blocks/{pagina}/children', tok, {'children': [stamp], 'after': head})
    return {'heading': head, 'mudancas': n}


def marcar_decisao(pagina, sigla, codigo, opcao, texto, tok=None):
    """Troca o bloco de opcoes da decisao `codigo` (bullet cujo texto comeca com `codigo`) por `✓ codigo = opcao: texto`."""
    tok = tok or token()
    _, corpo = secao(pagina, sigla, tok)
    for b in corpo:
        if b['type'] == 'bulleted_list_item' and _texto(b).strip().startswith(codigo):
            for f in (_filhos(b['id'], tok) if b.get('has_children') else []): _req('DELETE', f'{API}/blocks/{f["id"]}', tok)
            _req('PATCH', f'{API}/blocks/{b["id"]}', tok, {'bulleted_list_item': {'rich_text': rich(f'✓ `{codigo}` = {opcao}: {texto} *(feito {datetime.now(BRT).strftime("%d/%m %H:%M")})*')}})
            return True
    return False


def atualizar_todo(block_id, texto_md, checked, tok=None, base_rich=None):
    """Reescreve um to_do. Com `base_rich` (rich_text original do bloco), PRESERVA a formatacao do item (negrito, links,
    codigo) e so' acrescenta `texto_md` como sufixo — sem isso o item reaberto perdia a formatacao (reclamacao de 21/09/2026).
    Segmentos anteriores nossos (' — ✓ …' / ' — ⚠ …') sao removidos antes de acrescentar o novo."""
    tok = tok or token()
    if base_rich:
        base = []
        for seg in base_rich:
            t = (seg.get('plain_text') or (seg.get('text') or {}).get('content') or '')
            if t.lstrip().startswith(('— ✓', '— ⚠')): break
            base.append({'type': 'text', 'text': {'content': t, **({'link': seg['text']['link']} if (seg.get('text') or {}).get('link') else {})},
                         'annotations': {k: v for k, v in (seg.get('annotations') or {}).items() if k in ('bold', 'italic', 'code', 'strikethrough', 'underline')}})
        if base and base[-1]['text']['content'].rstrip() != base[-1]['text']['content']: base[-1]['text']['content'] = base[-1]['text']['content'].rstrip()
        rt = base + rich(' ' + texto_md.lstrip())
    else:
        rt = rich(texto_md)
    _req('PATCH', f'{API}/blocks/{block_id}', tok, {'to_do': {'rich_text': rt, 'checked': bool(checked)}})
    return True


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(0)
    a[0] = {'day': 'dia', 'section': 'secao', 'publish': 'publicar'}.get(a[0], a[0])   # English commands; Portuguese kept as aliases
    if a[0] == 'dia': print(pagina_do_dia(a[1]))
    elif a[0] == 'secao':
        pid = pagina_do_dia(a[1], criar=False); h, c = secao(pid, a[2]) if pid else (None, [])
        print('pagina', pid, '| heading', h); [print(' ', b['type'], _texto(b)[:100]) for b in c]
    elif a[0] == 'publicar':
        pid = pagina_do_dia(a[1]); print(publicar(pid, a[2], open(a[3], encoding='utf-8').read()))
