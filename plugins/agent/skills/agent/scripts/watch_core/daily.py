"""== DAILY do agent (uma copia para todas as instancias; uma instancia pode ter a sua num adapter proprio).

Fechamento (primeiro tick >= HORA_FECHAMENTO BRT, dia util, uma vez por dia): imprime a EVIDENCIA do dia — as
linhas de item (`-- ...`) que o proprio tick reportou hoje, guardadas em s['diario_daily'][dia] — para o agente
escrever o report da daily seguinte e gravar com `--daily set "..."`. Primeiro tick do dia util seguinte: reimprime
o texto gravado uma vez (`== DAILY (preparada em dd/mm — texto para falar hoje)`). O agente publica o mesmo texto
no Notion (pagina Dailies/AAAA-MM, secao da empresa) — regra no SKILL.md.
"""
import re
from datetime import datetime, timezone, timedelta

BRT = timezone(timedelta(hours=-3))
HORA_FECHAMENTO = 18
HORA_RASCUNHO = 9          # dia util a partir desta hora: cobrar o rascunho mesmo sem evidencia nenhuma
GUARDA_DIAS = 7
MAX_LINHAS_DIA = 80

def _agora(): return datetime.now(BRT)
def _hoje(): return _agora().date().isoformat()
def _dia_util(d): return d.weekday() < 5

# ---------------- evidencia ----------------
def registra_saida(s, texto):
    """Guarda as linhas de item (`-- ...`) e os cabecalhos (`== ...`) da saida de um tick como evidencia do dia."""
    linhas = [l.strip() for l in texto.splitlines() if l.startswith('-- ') or l.startswith('== ')]
    if not linhas: return
    di = s.setdefault('diario_daily', {})
    for d in [d for d in di if d < (_agora() - timedelta(days=GUARDA_DIAS)).date().isoformat()]: del di[d]
    hoje = di.setdefault(_hoje(), [])
    h = _agora().strftime('%H:%M'); cab = None
    for l in linhas:
        if l.startswith('== '): cab = l[3:].split(' (')[0].lower(); continue
        item = f'{h} [{cab or "?"}] {l[3:]}'
        if item[6:] not in (x[6:] for x in hoje) and len(hoje) < MAX_LINHAS_DIA: hoje.append(item)

def evidencia(s): return (s.get('diario_daily') or {}).get(_hoje(), [])

def imprime_evidencia(s, extra=None):
    print('== DAILY (evidencia de hoje — escrever o report e gravar com --daily set "...")')
    ev = evidencia(s) + list(extra or [])
    for x in ev: print('  · ' + x)
    if not ev: print('  (nada registrado hoje nas fontes do agent — o report sai do que aconteceu na sessao)')
    return True

# ---------------- report ----------------
def daily_set(s, texto):
    d = s.get('daily') or {}
    mesmo_dia = d.get('dia') == _hoje()
    # update, nao troca: o dict carrega heads/feitos/notion (paginas, progresso ticado); trocar apagava tudo isso
    # no --daily set do fechamento (23/09/2026)
    d.update({'dia': _hoje(), 'texto': texto, 'lido': False, 'notion_md': d.get('notion_md') if mesmo_dia else None}); s['daily'] = d
    rascunho_visto(s)

def daily_pendente_leitura(s):
    d = s.get('daily') or {}
    if d.get('texto') and d.get('dia') != _hoje() and not d.get('lido'): return d
    return None

def imprime_daily(d, marcar=None):
    print(f'== DAILY (preparada em {d["dia"][8:10]}/{d["dia"][5:7]} — texto para falar hoje)')
    print(d['texto'])
    if marcar is not None: marcar['lido'] = True
    return True

def fechamento_devido(s, dry):
    ag = _agora()
    return (not dry and _dia_util(ag) and ag.hour >= HORA_FECHAMENTO and s.get('daily_fechamento') != _hoje())

def tick(s, dry, extra=None):
    """Chamar no fim da impressao do tick (antes do save_state). Devolve True se imprimiu algo."""
    algo = False
    if dry: return algo
    d = daily_pendente_leitura(s)
    if d: algo |= imprime_daily(d, marcar=s['daily'])
    if imprime_gate_decisoes(s): algo = True
    if fechamento_devido(s, dry):
        if algo: print()
        algo |= imprime_evidencia(s, extra); s['daily_fechamento'] = _hoje()
    else:
        import io, contextlib
        b = io.StringIO()
        with contextlib.redirect_stdout(b): r = rascunho_tick(s)
        if r:
            if algo: print()
            print(b.getvalue(), end=''); algo = True
    return algo

# ---------------- rascunho vivo ----------------
# Pedido do usuario em 21/09/2026: "a qualquer momento do dia, se eu abrir, eu consigo ver como foi o dia e estar
# pronto pra reportar meu progresso". Cada tick compara a evidencia de hoje com a que gerou a ultima versao do
# texto; evidencia nova = o script cobra: reescrever (--daily set + --daily md + republicar no Notion) ou
# --daily visto (a novidade e' de terceiros e nao muda o que VOCE fez). O fechamento das 18:00 vira revisao final.
def rascunho_tick(s):
    itens = evidencia(s); d = s.get('daily') or {}
    escrito_hoje = d.get('dia') == _hoje()
    vistos = d.get('ev_itens') if escrito_hoje else None
    if not escrito_hoje and not itens:
        # Dia quieto tambem tem daily: sem esta cobranca, um dia sem nada nas fontes nunca dispara o rascunho e a
        # secao da empresa simplesmente NAO APARECE na pagina do dia seguinte (22/09/2026: a pagina 2026-09-23 tinha
        # HAK e nao tinha BRV, e o usuario perguntou por que). Pendente/Aguardando existem mesmo em dia parado.
        a = _agora()
        if not (_dia_util(a) and a.hour >= HORA_RASCUNHO): return False
        print('== DAILY (rascunho ainda nao escrito: nenhuma evidencia hoje nas fontes — escrever com o que saiu da '
              'sessao e publicar assim mesmo (--daily set "..." + --daily md "..." + --daily publicar); dia parado '
              'tambem vai para o Notion, nem que seja so Pendente/Aguardando)')
        return True
    novos = [x for x in itens if x[6:] not in [v[6:] for v in (vistos or [])]]   # compara sem a hora
    if not novos: return False
    print(f'== DAILY (rascunho {"desatualizado" if vistos is not None else "ainda nao escrito"}: +{len(novos)} na evidencia — se for coisa SUA, reescrever (--daily set "..." + --daily md "...") e republicar no Notion; senao --daily visto)')
    for x in novos: print('  + ' + x)
    return True

def rascunho_visto(s):
    d = s.setdefault('daily', {'dia': _hoje(), 'texto': '', 'lido': False})
    if d.get('dia') != _hoje(): d.update({'dia': _hoje(), 'texto': d.get('texto', ''), 'lido': False, 'notion_md': None})
    d['ev_itens'] = list(evidencia(s))

def daily_md(s, md):
    """Guarda o markdown publicado no Notion e devolve o anterior (old_str do proximo update_content).
    Guarda tambem o CABECALHO (fala + Ontem/Hoje/Impedimentos, tudo antes de `**Pendente**`): e' o que `publicar_auto`
    reaproveita para republicar sozinho quando so' as secoes GTD mudam (22/09/2026)."""
    d = s.setdefault('daily', {'dia': _hoje(), 'texto': '', 'lido': False})
    ant = d.get('notion_md'); d['notion_md'] = md
    head = md.split('**Pendente**')[0].rstrip()
    d['notion_head'] = head
    # Cabecalho por pagina (23/09/2026): texto escrito no fechamento (>= 18:00) e' a fala de AMANHA e so' aparece na
    # pagina nova a meia-noite; antes disso e' o cabecalho da pagina de hoje (progresso ticado).
    ag = _agora()
    data = proximo_dia_util(ag) if (_dia_util(ag) and ag.hour >= HORA_FECHAMENTO) else data_daily(ag)
    d.setdefault('heads', {})[data] = head
    for k in [k for k in d['heads'] if k < _hoje()]: d['heads'].pop(k)
    return ant


def publicar_auto(s, secoes_md, dry=False):
    """Rascunho vivo SEM depender de mim: a cada tick recompoe `cabecalho + secoes` e, se mudou em relacao ao que esta
    no Notion, republica. Antes (22/09/2026) a publicacao so' acontecia quando eu reescrevia a daily na sessao — num dia
    em que a evidencia nova nao era trabalho dele (`--daily visto`), a pagina do dia seguinte ficava vazia o dia inteiro.
    Devolve [] (nada mudou) ou uma linha para o relatorio do tick."""
    d = s.setdefault('daily', {})
    alvo = data_daily()
    # Cabecalho da pagina alvo (`daily.heads[data]`). Sem cabecalho para ela: se a secao ja existe na pagina (escrita por
    # outra via), NAO reescrever — so' as secoes apagariam o Ontem/Hoje falado; sem secao, publica Pendente/Aguardando.
    head = ((d.get('heads') or {}).get(alvo) or '').rstrip()
    if not head and (d.get('notion') or {}).get('data') != alvo:
        if d.get('notion_skip') == alvo: return []
        from . import notion_page as nd
        pid = nd.pagina_do_dia(alvo, d.get('notion_pagina') or nd.MES_DEFAULT, criar=False)
        if pid and nd.secao(pid, SIGLA)[0]:
            d['notion_skip'] = alvo; return []
    md = (head + '\n\n' if head else '') + secoes_md
    ja_na_pagina = (d.get('notion') or {}).get('data') == data_daily()
    if ja_na_pagina and md.strip() == (d.get('notion_md') or '').strip(): return []
    if dry: return ['-- daily: secoes mudaram (dry: nao publicado)']
    r = publicar_notion(s, md, auto=True)
    return [f'-- daily republicada no Notion ({r["data"]}, secoes atualizadas)' + ('' if head else ' — ainda sem o texto de Ontem/Hoje')]

def proximo_dia_util(agora=None):
    d = (agora or datetime.now(BRT)).date() + timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return d.isoformat()

def data_daily(agora=None):
    """Pagina em que o agent escreve = a de HOJE, o dia inteiro (a daily falada de manha, com o progresso ticado); a
    pagina nova nasce quando o dia vira, com a fala escrita no fechamento. Fim de semana = a do proximo dia util.
    Ate 23/09/2026 era sempre o dia seguinte: o trabalho de hoje aparecia como "Ontem" numa pagina de amanha desde a
    meia-noite, e o usuario leu como erro ("tem items em Ontem que foram feitos hj"; "ate o final do dia ... na pagina
    de hj, e quando comeca um novo dia na pagina de amanha")."""
    ag = agora or datetime.now(BRT)
    return ag.date().isoformat() if _dia_util(ag) else proximo_dia_util(ag)

def publicar_notion(s, md=None, auto=False):
    """Publica/reescreve a secao `## SIGLA` da SUBPAGINA DO DIA (`Dailies / AAAA/MM / AAAA-MM-DD (dia)`) pela REST, por bloco
    (notion_daily.py; pedido de 21/09/2026). md = o markdown (default: o guardado por --daily md). Guarda ids em daily.notion."""
    ag = _agora()
    if not auto and _dia_util(ag) and ag.hour >= HORA_FECHAMENTO:
        # Fala do fechamento e' de AMANHA: a pagina nova so' nasce quando o dia vira (`publicar_auto` usa heads[data]).
        return {'data': proximo_dia_util(ag), 'pagina_dia': 'ainda nao criada (sai no primeiro tick depois da meia-noite)', 'blocos': 0}
    from . import notion_page as nd
    d = s.setdefault('daily', {})
    md = md if md is not None else d.get('notion_md')
    if not md: raise RuntimeError('sem markdown: --daily md "..." antes (ou passe o arquivo)')
    if not SIGLA: raise RuntimeError('dv.SIGLA nao definido (o agent define depois do import)')
    mes = d.get('notion_pagina') or nd.MES_DEFAULT
    data = data_daily()
    pid = nd.pagina_do_dia(data, mes)
    r = nd.publicar(pid, SIGLA, md)
    d['notion'] = {'data': data, 'pagina_dia': pid, 'heading': r['heading'], 'publicado': datetime.now(timezone.utc).isoformat()}
    d['notion_md'] = md
    return {'data': data, 'pagina_dia': pid, **r}

# English subcommands; the Portuguese ones stay as aliases
DAILY_ALIASES = {'evidence': 'evidencia', 'sections': 'secoes', 'codes': 'codigos', 'page': 'pagina', 'seen': 'visto', 'publish': 'publicar'}
ACTION_ALIASES = {'bucket=': 'balde=', 'who=': 'quem=', 'waiting': 'aguardando', 'decision': 'decisao', 'next': 'proximas', 'blocker': 'impedimento'}


def cli(args, s, save_state):
    """`--daily` (mostra), `set "texto"` (grava a prosa falada), `md "markdown"` (guarda o publicado no Notion, devolve o anterior), `visto` (evidencia nova nao muda o texto), `evidencia` (materia-prima). Devolve True se tratou."""
    if args is None: return False
    if args: args = [DAILY_ALIASES.get(args[0], args[0])] + list(args[1:])
    if args and args[0] == 'set':
        daily_set(s, ' '.join(args[1:])); save_state(s); print('daily gravada para', s['daily']['dia']); return True
    if args and args[0] == 'evidencia': imprime_evidencia(s); return True
    if args and args[0] == 'secoes': print(secoes_md(s)); save_state(s); return True
    if args and args[0] == 'codigos':
        for k, v in sorted(_codigos(s).items()): print(f'{k} -> {v}')
        return True
    if args and args[0] == 'pagina': s.setdefault('daily', {})['notion_pagina'] = args[1]; save_state(s); print('pagina do mes:', args[1]); return True
    if args and args[0] == 'visto': rascunho_visto(s); save_state(s); print('evidencia de hoje marcada como vista (rascunho mantido)'); return True
    if args and args[0] == 'md':
        daily_md(s, ' '.join(args[1:])); save_state(s); print('md guardado; publicar com --daily publicar'); return True
    if args and args[0] == 'publicar':
        md = open(args[1], encoding='utf-8').read() if len(args) > 1 else None
        r = publicar_notion(s, md); save_state(s)
        print(f'publicado: {SIGLA} em {r["data"]} (pagina {r["pagina_dia"]}, {r["blocos"]} blocos)'); return True
    d = s.get('daily') or {}
    print(f'== DAILY (preparada em {d["dia"]})\n{d["texto"]}' if d.get('texto') else 'sem daily gravada'); return True

# ---------------- acoes manuais + secoes Pendente / Aguardando (GTD) ----------------
# Pedido de 21/09/2026: a entrada do Notion leva tambem **Pendente** (o que VOCE ainda vai fazer) e **Aguardando por**
# (o que interessa a voce mas depende de outros). Fontes: pendencias abertas do agent + acoes manuais (--acao add).
def acoes(s): return s.setdefault('acoes', [])

def acao_add(s, texto, balde='proximas', quem=None):
    s['acao_seq'] = s.get('acao_seq', 0) + 1
    a = {'id': s['acao_seq'], 'texto': texto, 'balde': balde, 'quem': quem, 'desde': datetime.now(timezone.utc).isoformat(), 'status': 'aberta'}
    acoes(s).append(a); return a

def _instante(v):
    """An origin timestamp (ISO 8601; a trailing Z or no offset = UTC) as UTC isoformat, or None."""
    if not v: return None
    try: t = datetime.fromisoformat(str(v).strip().replace('Z', '+00:00'))
    except ValueError: return None
    if len(str(v).strip()) <= 10: return None             # a bare date is not an instant
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def acao_done(s, ident, resposta=None, quando=None):
    """Closes an action. `quando` (ISO 8601 with offset) is when it really closed, from the origin (PR merged_at);
    without it, now."""
    for a in acoes(s):
        if a['id'] == ident and a['status'] == 'aberta':
            a['status'] = 'feita'; a['feita'] = _instante(quando) or datetime.now(timezone.utc).isoformat()
            if resposta: a['resposta'] = resposta
            return a
    return None

def secoes_md(s):
    # cada item leva o codigo de exibicao (p1.., w1..; pedido de 21/09/2026) — a chave interna (a1, p3 da fila) fica no mapa
    prox = [{'chave': f'a{a["id"]}', 'texto': a['texto']} for a in acoes(s) if a['status'] == 'aberta' and a['balde'] == 'proximas']
    prox += [{'chave': p['chave'], 'texto': f'responder {p["quem"]} ({p["fonte"]}): {p["assunto"][:80]}'} for p in s.get('pendencias', []) if p.get('status') == 'aberta']
    dec = [{'chave': f'a{a["id"]}', 'texto': f'*(decisão sua)* {a["texto"]}'} for a in acoes(s) if a['status'] == 'aberta' and a['balde'] == 'decisao']
    def _desde(iso):
        d = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT); dias = (_agora().date() - d.date()).days
        return f' · desde {d:%d/%m}' + (f' ({dias}d)' if dias >= 1 else '')
    agu = [{'chave': f'a{a["id"]}', 'texto': f'{a["texto"]} — **{a.get("quem") or "?"}**{_desde(a["desde"])}'} for a in acoes(s) if a['status'] == 'aberta' and a['balde'] == 'aguardando']
    inv = atribui_codigos(s, {'proximas': prox + dec, 'aguardando': agu})
    # decisoes respondidas hoje continuam na entrada como ✓ (o codigo que ela tinha; a republicacao nao apaga o registro)
    hoje = _hoje(); m = _codigos(s)
    feitas = [a for a in acoes(s) if a['status'] == 'feita' and a['balde'] == 'decisao' and a.get('feita') and datetime.fromisoformat(a['feita']).astimezone(BRT).strftime('%Y-%m-%d') == hoje]   # 'feita' e' UTC
    inv_f = {v: k for k, v in m.items()}
    linhas_f = []
    for a in feitas:
        cod = inv_f.get(f'a{a["id"]}', f'a{a["id"]}'); quando = datetime.fromisoformat(a['feita']).astimezone(BRT).strftime('%d/%m %H:%M')
        linhas_f.append(f'- ✓ `{cod}` {a.get("resposta") or "decidido"} *(decidido {quando})*')
    abertos = {inv[x['chave']]: x['texto'] for x in prox + agu}
    feitos = feitos_do_dia(s, abertos)
    fp = [feitos[c] for c in sorted(feitos, key=_ordem) if c.startswith('p') and '-' not in c]
    fw = [feitos[c] for c in sorted(feitos, key=_ordem) if c.startswith('w')]
    out = ['**Pendente**'] + ([f'- [ ] `{inv[x["chave"]]}` {x["texto"]}' for x in prox] + [f'- `{inv[x["chave"]]}` {x["texto"]}' for x in dec] + linhas_f + fp or ['- nada'])
    out += ['', '**Aguardando por**'] + ([f'- [ ] `{inv[x["chave"]]}` {x["texto"]}' for x in agu] + fw or ['- nada'])
    return '\n'.join(out)

def _acao_en(x):
    for en, pt in ACTION_ALIASES.items():
        if en.endswith('='):
            if x.startswith(en): x = pt + x[len(en):]
        elif x.endswith('=' + en): x = x[:-len(en)] + pt
    return x


def cli_acao(args, s, save_state):
    """`--acao add "texto" [balde=aguardando|decisao] [quem=Fulano]` | `--acao done N` | `--acao list` (decisao = o agent espera resposta SUA; quem = de quem se espera, obrigatorio no aguardando)."""
    args = [_acao_en(x) for x in args] if args else args
    cmd = args[0] if args else 'list'
    if cmd == 'add':
        balde = next((x.split('=', 1)[1] for x in args[2:] if x.startswith('balde=')), 'proximas')
        quem = next((x.split('=', 1)[1] for x in args[2:] if x.startswith('quem=')), None)
        # impedimento (23/09/2026, instancia piloto): vira linha com a etiqueta Impedimento na base Tarefas, no topo da lista
        if balde not in ('proximas', 'aguardando', 'decisao', 'impedimento'): print('balde: proximas|aguardando|decisao|impedimento'); return True
        x = acao_add(s, args[1], balde, quem); save_state(s); print(f'a{x["id"]} [{balde}] {x["texto"]}' + (f' · aguardando {quem}' if quem else '')); return True
    if cmd == 'done':
        x = acao_done(s, int(args[1]), ' '.join(args[2:]) or None); save_state(s); print(f'a{args[1]}: feita' if x else f'a{args[1]}: nao existe ou ja feita'); return True
    if cmd == 'notion':   # --acao notion N <url da linha na base de decisoes>
        for x in acoes(s):
            if x['id'] == int(args[1]): x['notion'] = args[2]; save_state(s); print(f'a{args[1]}: notion={args[2]}'); return True
        print(f'a{args[1]}: nao existe'); return True
    for x in acoes(s):
        if x['status'] == 'aberta': print(f'a{x["id"]} [{x["balde"]}] {x["texto"]} · desde {x["desde"][:16]}')
    return True

# ---------------- decisoes no Notion ----------------
# Pedido de 21/09/2026: item que so' precisa de um Sim/Nao vira uma linha na base de decisoes do Notion ("Decisoes do vigia", nome dado antes do rename para agent)
# (uma data source do Notion); o usuario marca Resposta la' e o agent le no tick seguinte.
# O script nao fala com o Notion (sem token); ele so' avisa o agente QUANDO ha' decisao aberta, para o agente
# consultar a base pelo conector (uma query pequena) e dar seguimento.

def decisoes_abertas(s):
    return [a for a in acoes(s) if a['status'] == 'aberta' and a['balde'] == 'decisao']

SIGLA = None   # cada agent define dv.SIGLA = 'AUR'|'TKB'|'BRV'|'HAK' depois do import: escopo das caixinhas na pagina do mes

def imprime_gate_decisoes(s):
    """Decisoes abertas: le as caixinhas marcadas na pagina do mes no Notion (decisoes_notion.py, precisa do token);
    sem token, so' lembra o agente de conferir pelo conector (no maximo a cada 30 min, s['decisoes_mcp_check'])."""
    ab = decisoes_abertas(s)
    if not ab: return False
    from . import notion_decisions as dn
    from . import notion_page as nd
    mes = (s.get('daily') or {}).get('notion_pagina') or nd.MES_DEFAULT
    pagina = ((s.get('daily') or {}).get('notion') or {}).get('pagina_dia') or nd.pagina_do_dia(data_daily(), mes, criar=False) or mes
    cods = {f'a{a["id"]}' for a in ab} | {k for k, v in _codigos(s).items() if v in {f'a{a["id"]}' for a in ab}}   # a3 e o p3 que o mapeia
    try: r = dn.marcadas(pagina, so_codigos=cods, empresa=SIGLA)
    except Exception as e: r = f'erro: {type(e).__name__}: {e}'
    if isinstance(r, list):
        if not r: return False           # token ok, nada marcado: silencio
        print(f'== DECISOES ({len(r)} respondida(s) no Notion — agir agora, marcar feito na pagina e --acao done)')
        for x in r:
            interna = x['acao'] if x['acao'].startswith('a') else (resolve_codigo(s, x['acao']) or x['acao'])
            print(f'  · {interna} ({x["acao"]}) = {x["opcao"]}: {x["texto"]}')
        return True
    print(f'== DECISOES ({len(ab)} aberta(s); {"sem token do Notion" if r is None else r} — conferir as caixinhas pelo conector se a ultima checagem passou de 30 min)')
    for a in ab: print(f'  · a{a["id"]} {a["texto"][:80]}')
    return True

# ---------------- codigos de exibicao (pedido de 21/09/2026) ----------------
# Na entrada do Notion cada item tem um codigo curto por secao: Ontem y1.., Hoje t1.., Impedimentos b1.., Pendente p1..,
# Aguardando por w1... y/t/b sao numerados pelo agente ao escrever o texto; p/w sao atribuidos aqui, UMA vez por dia
# (persistidos em s['daily']['codigos']) para o usuario poder dizer "p2 feito" sem o numero mudar quando outro item sai.
def _codigos(s):
    c = s.setdefault('daily', {}).setdefault('codigos', {'dia': _hoje(), 'map': {}})
    if c.get('dia') != _hoje(): c['dia'] = _hoje(); c['map'] = {}
    return c['map']

def atribui_codigos(s, b):
    """b = {'proximas': [{'chave':..}], 'aguardando': [...]}; devolve {chave_interna: codigo_exibicao} e persiste os novos."""
    m = _codigos(s); inv = {v: k for k, v in m.items()}
    for balde, pref in (('proximas', 'p'), ('aguardando', 'w')):
        for x in b[balde]:
            if x['chave'] in inv: continue
            n = 1 + sum(1 for k in m if k[0] == pref and k[1:].isdigit())
            m[f'{pref}{n}'] = x['chave']; inv[x['chave']] = f'{pref}{n}'
    return inv

def feitos_do_dia(s, abertos):
    """Progresso do dia (pedido de 23/09/2026, quatro agents de trabalho: "nao achei nada ticado hoje, nao fizemos nada?"). `abertos` =
    {codigo: texto_md} dos itens ainda abertos nesta renderizacao. Guarda o ultimo texto de cada codigo do dia e, quando um
    codigo some da lista aberta, carimba a hora da baixa. Devolve {codigo: '- [x] `cod` texto — ✓ feito HH:MM'} dos que
    ja sairam hoje — ficam na secao marcados ate o fechamento, em vez de sumir."""
    d = s.setdefault('daily', {})
    f = d.setdefault('feitos', {'dia': _hoje(), 'texto': {}, 'hora': {}})
    if f.get('dia') != _hoje(): f.update({'dia': _hoje(), 'texto': {}, 'hora': {}})
    for cod, txt in abertos.items(): f['texto'][cod] = txt; f['hora'].pop(cod, None)
    out = {}
    for cod, txt in f['texto'].items():
        if cod in abertos: continue
        h = f['hora'].setdefault(cod, _agora().strftime('%H:%M'))
        out[cod] = f'- [x] `{cod}` {txt} — ✓ feito {h}'
    return out

def _ordem(cod): return (cod[0], int(cod[1:]) if cod[1:].isdigit() else 0)

def resolve_codigo(s, cod):
    """'p2' -> chave interna ('a1', 'pr41678', 'qa75582', 'p3' da fila...) ou None."""
    return _codigos(s).get(cod)


# ---------------- itens marcados como feitos (verificacao por evidencia) ----------------
# Pedido de 21/09/2026: "os itens de hoje, impedimentos, pendente e aguardando por poderiam ter todos checkbox; se eu marcar
# um, estou indicando que foi feito, e voce deveria procurar a evidencia nas fontes; se nao encontrou, pode reabrir (tirar o
# tic) com justificativa; se encontrou, adicionar 'evidenciado por xyz em xyz'". Segunda marcacao do mesmo item sem evidencia
# = confirmado por ele (nao reabre de novo).
def verifica_marcados(s, evidencia_fn):
    """Le os to-dos marcados na secao SIGLA da subpagina do dia; para cada item ainda nao verificado chama
    evidencia_fn(codigo, chave_interna, texto) -> (ok, detalhe) e reescreve o bloco no Notion. Devolve linhas para o tick."""
    from . import notion_decisions as dn, notion_page as nd
    pagina = ((s.get('daily') or {}).get('notion') or {}).get('pagina_dia')
    if not pagina or not SIGLA: return []
    try: itens = dn.concluidos(pagina, SIGLA)
    except Exception as e: return [f'== FEITOS (erro ao ler o Notion: {type(e).__name__}: {str(e)[:80]})']
    if not itens: return []
    ver = s.setdefault('daily', {}).setdefault('verif', {})
    hoje = _hoje(); agora = _agora().strftime('%d/%m %H:%M'); linhas = []
    for it in itens:
        cod = it['codigo']; reg = ver.get(cod) or {'dia': hoje, 'tentativas': 0, 'status': None}
        if reg.get('dia') != hoje: reg = {'dia': hoje, 'tentativas': 0, 'status': None}
        if not it['checked']:
            if reg.get('status') == 'reaberto': pass          # esta' desmarcado por nos; espera nova marcacao
            continue
        if reg.get('status') in ('evidenciado', 'confirmado'): continue   # ja fechado
        # marcado POR MIM (progresso do dia: '— ✓ feito HH:MM' / '— ✓ merged' / '— ✓ HH:MM'): nao e' marcacao dele, nao
        # verificar. Em 23/09/2026 a verificacao desmarcava ("⚠ reaberto") o que eu tinha acabado de marcar e o usuario
        # abriu a pagina sem nada ticado.
        if re.search(r'—\s+✓\s+(feito|merged|\d{1,2}:\d{2})', it['texto']): continue
        chave = resolve_codigo(s, cod)
        texto_base = re.sub(r'\s+—\s+(✓|⚠).*$', '', it['texto'])
        try: ok, det = evidencia_fn(cod, chave, texto_base)
        except Exception as e: ok, det = None, f'erro ao verificar: {type(e).__name__}'
        if ok:
            nd.atualizar_todo(it['block'], f'— ✓ evidenciado por {det} *({agora})*', True, base_rich=it.get('rich'))
            reg.update({'status': 'evidenciado', 'detalhe': det}); linhas.append(f'-- {cod} feito: evidenciado por {det}')
            _fecha_interno(s, chave)
        elif reg['tentativas'] >= 1:
            nd.atualizar_todo(it['block'], f'— ✓ confirmado por você, sem evidência automática ({det}) *({agora})*', True, base_rich=it.get('rich'))
            reg.update({'status': 'confirmado', 'detalhe': det}); linhas.append(f'-- {cod} feito: confirmado por voce (2a marcacao; {det})')
            _fecha_interno(s, chave)
        else:
            nd.atualizar_todo(it['block'], f'— ⚠ reaberto {agora}: {det}; se fez por outro canal, marque de novo que eu aceito', False, base_rich=it.get('rich'))
            reg.update({'status': 'reaberto', 'tentativas': reg['tentativas'] + 1, 'detalhe': det}); linhas.append(f'-- {cod} REABERTO: {det}')
        ver[cod] = reg
    return (['== FEITOS (caixinhas marcadas na daily)'] + linhas) if linhas else []


def _fecha_interno(s, chave):
    """Da baixa no item interno quando o usuario marcou e foi aceito: acao manual -> feita; pendencia -> resolvida."""
    if not chave: return
    if chave.startswith('a') and chave[1:].isdigit(): acao_done(s, int(chave[1:]))
    for p in s.get('pendencias', []):
        if p.get('chave') == chave and p.get('status') == 'aberta':
            p['status'] = 'resolvida'; p['como'] = 'marcado como feito na daily'; p['resolvida'] = datetime.now(timezone.utc).isoformat()
