#!/usr/bin/env python3
"""Motor COMPARTILHADO das tarefas na pagina do dia do Notion (watch_core; nasceu numa instancia piloto em 23/09/2026 e
virou compartilhado em 24/09/2026: "pra que ambos usem a mesma logica, e se eu melhorar um, melhora o outro").

O ESTADO de cada agent (acoes `aN` + pendencias `pN` + fontes extras do agent) e' a unica fonte; cada tarefa ganha um
id fixo <SIGLA>-N. A cada tick o agent gera a secao `## <SIGLA>` da pagina do dia como checklist nativo (fala +
Impedimentos / A fazer hoje (+ Backlog) / Feito hoje / Aguardando por / Ontem), publicando so os blocos que mudaram.

O que e' de cada agent entra por `configura(sigla, **ganchos)` no wrapper `scripts/tarefas_notion.py` dele:
  link_auto(texto) -> url        link da tarefa a partir do texto (MR do GitLab, work item do ADO...)
  autolink(texto) -> md          refs no detalhe que viram link (antes do link de URL solta, que e' generico)
  necessidades(s) -> [...]       o que o agent precisa do usuario (v-*), alem das fontes quebradas (generico)
  conserto {fonte: texto}        como consertar cada fonte quebrada deste agent
  fontes_extras(s, agora) -> {}  tarefas que nao sao acao/pendencia (cards do ADO, PRs...)
  verifica_links(s, dry) -> []   baixa automatica pelo estado do link (MR/PR mergeada)
  inicio 'AAAA-MM-DD'            a partir de que pagina o gerador vale (antes, o fluxo antigo segue)
  rotulos {chave: texto}         rotulos da pagina (ROT_EN para pagina em ingles)
Regras de uso e historico: SKILL.md da vigia-daily (skill antiga, arquivada), secao "Tarefas na pagina do dia".
"""
import os, sys, json
from datetime import datetime, timezone, timedelta

from . import notion_page as nd
from . import notion_decisions as dn

API = 'https://api.notion.com/v1'
SIGLA = None                              # configura() define: 'AUR', 'TKB', ...
G = {'link_auto': lambda t: '', 'autolink': lambda t: t, 'necessidades': lambda s: [], 'conserto': {},
     'fontes_extras': lambda s, agora: {}, 'verifica_links': lambda s, dry: [], 'sem_tarefa_fontes': lambda s, ini: [],
     'inicio': None, 'verifica_cwd': os.path.expanduser('~'), 'rotulos': {},
     # 24/09/2026 (work-watch, custo por tarefa): texto depois da linha de uma tarefa (' · ⏱ 19min · US$ 5,27') e
     # linhas no fim da secao do dia; os agents que nao configuram ficam como estavam
     'extra_linha': lambda s, it: '', 'rodape': lambda s, data: [],
     # 25/09/2026 (job-scout, secao LKD): speech=False = secao so de tarefas, sem a fala e sem "Ontem"; ela nunca cria
     # a pagina do dia, entra na pagina que os outros agents criaram
     'speech': True}

# Rotulos da pagina (gancho `rotulos` sobrescreve; uma pagina em ingles usa ROT_EN)
ROT_PT = {'impedimentos': 'Impedimentos', 'nenhum': 'Nenhum', 'a_fazer': 'A fazer hoje', 'backlog': 'Backlog',
          'feito': 'Feito hoje', 'aguardando': 'Aguardando por', 'ontem': 'Ontem', 'nada': 'nada',
          'plano_vazio': 'nada no plano — puxe do backlog ("{sigla}-N hoje" no chat)', 'decisao': 'decisão sua',
          'agent': 'o agent precisa', 'recomendada': 'Recomendada', 'desde': 'desde', 'ate': 'até', 'ate_hoje': 'até hoje',
          'venceu': 'venceu', 'decidido': 'Decidido: ', 'decidir': 'Decidir: ',
          'top': 'Top do dia — se isto sair, o dia valeu'}
ROT_EN = {'impedimentos': 'Blockers', 'nenhum': 'None', 'a_fazer': 'Today', 'backlog': 'Backlog', 'feito': 'Done today',
          'aguardando': 'Waiting on', 'ontem': 'Yesterday', 'nada': 'nothing',
          'plano_vazio': 'nothing planned — pull from the backlog ("{sigla}-N today" in chat)', 'decisao': 'your call',
          'agent': 'the agent needs', 'recomendada': 'Recommended', 'desde': 'since', 'ate': 'due', 'ate_hoje': 'due today',
          'venceu': 'overdue', 'decidido': 'Decided: ', 'decidir': 'Decide: ',
          'top': 'Top of the day — if these ship, the day counted'}


ROT_LEGADO = {'agent': 'vigia'}   # chave antiga de rotulo (ate work-watch 0.18): um gancho `rotulos` com 'vigia' ainda vale


def R(k):
    r = G['rotulos']
    if k not in r and ROT_LEGADO.get(k) in r: return r[ROT_LEGADO[k]]
    return {**ROT_PT, **r}[k]


def configura(sigla, **ganchos):
    global SIGLA
    SIGLA = sigla
    for k, v in ganchos.items():
        if k not in G: raise KeyError(f'gancho desconhecido: {k}')
        G[k] = v
BRT = timezone(timedelta(hours=-3))
JANELA_PEND = timedelta(days=14)          # pendencia resolvida/ignorada ha mais que isso nao entra (a fila guarda historico longo)
# ordem = ordem das colunas do quadro Kanban AUR (aba da base). "A fazer"/"Feito" substituiram "Aberta"/"Concluída" em
# 23/09/2026 (a API nao renomeia opcao de select: as linhas foram movidas e as opcoes velhas ficaram vazias)
STATUS = {'Impedimento': 'red', 'Decisão': 'orange', 'A fazer': 'blue', 'Fazendo': 'purple', 'Aguardando': 'yellow',
          'Feito': 'green', 'Descartada': 'gray', 'Sem ação': 'default'}
ABERTOS = ('Impedimento', 'Decisão', 'A fazer', 'Fazendo', 'Aguardando')
# pendencia que se resolveu sem trabalho seu (e-mail so lido, reuniao que passou, convite cancelado/visto no calendario)
# vira "Sem ação": fica na base mas fora do "Feito hoje" / "Ontem" (pedido de 23/09: so aparece feito o que voce fez)
# 'sem ação no Planou': the person closed it in Planou as "no action" (28/09/2026)
# work-watch github: a green PR waiting for review left the queue by itself (review came, Draft, merged/closed, red CI)
SEM_ACAO = {'lido', 'passou', 'cancelado', 'convite (ver CALENDARIO)', 'sem ação no Planou',
            'review chegou', 'voltou a Draft', 'PR mergeada', 'PR fechada', 'CI vermelho (pausada)'}
PRIO = {'impedimento': 0, 'decisao': 1, 'proximas': 2, 'pendencia': 2, 'reuniao': 2, 'aguardando': 5}


def _tok():
    t = dn.token()
    if not t: raise RuntimeError(f'sem NOTION_TOKEN em {dn.ENV}')
    return t


def _data(iso):
    if not iso: return None
    try: return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT).isoformat(timespec='minutes')
    except ValueError: return None


def itens(s, agora=None):
    out = _itens_crus(s, agora); m = ids(s, agora)
    for c, it in out.items(): it['uid'] = m.get(c, c)
    return out


def _itens_crus(s, agora=None):
    """Tarefas vistas pelo agent: {codigo: {...}}."""
    agora = agora or datetime.now(timezone.utc)
    out = {}
    for a in s.get('acoes') or []:
        st = 'Feito' if a.get('status') == 'feita' else {'aguardando': 'Aguardando', 'decisao': 'Decisão', 'impedimento': 'Impedimento'}.get(a.get('balde'), 'A fazer')
        out[f'a{a["id"]}'] = {'titulo': a['texto'], 'status': st, 'origem': 'ação', 'quem': a.get('quem') or '',
                              'entrou': _data(a.get('desde')), 'concluida': _data(a.get('feita')) if st == 'Feito' else None,
                              'prio': PRIO.get(a.get('balde') or 'proximas', 3), 'deadline': a.get('deadline')}
    corte = agora - JANELA_PEND
    from . import planou
    abertas = planou.open_codes()       # o Planou ainda tem aberta: o fechamento vai mesmo depois do corte (PLN0028)
    for p in s.get('pendencias') or []:
        # convite de reuniao nao e' tarefa (24/09/2026, usuario: "responder convite de reuniao eu nao sei se e' uma tarefa
        # relevante"): continua no aviso/lembrete do agent e na agenda, mas fora da lista da pagina do dia
        if p.get('fonte') == 'calendar': continue
        fim = p.get('resolvida')
        # ignorada nao tem data: entra mesmo assim (so atualiza linha que ja existe; linha nova sem data e' pulada no tick)
        if (p.get('status') != 'aberta' and fim and datetime.fromisoformat(fim.replace('Z', '+00:00')) < corte
                and p['chave'] not in abertas): continue
        st = {'aberta': 'A fazer', 'resolvida': 'Feito', 'ignorar': 'Descartada'}.get(p.get('status'), 'A fazer')
        if st == 'Feito' and p.get('como') in SEM_ACAO: st = 'Sem ação'
        assunto = ' '.join((p.get('assunto') or '').split())[:180]
        out[p['chave']] = {'titulo': f'{assunto} — {p.get("quem") or ""}'.strip(' —'), 'status': st, 'origem': p.get('fonte') or '',
                           'quem': p.get('quem') or '', 'entrou': _data(p.get('desde') or p.get('criada')),
                           'concluida': _data(fim) if st == 'Feito' else None,
                           'prio': PRIO.get('reuniao' if p.get('fonte') == 'reuniao' else 'pendencia'),
                           'curto': curto_pend(p), 'ref': f"{p.get('fonte')}:{p.get('ref')}" if p.get('ref') else '',
                           'deadline': prazo_da_pendencia(p)}
    for a in s.get('acoes') or []:
        out[f'a{a["id"]}']['curto'] = curto_acao(a)
        r = a.get('resposta') or ''
        if r and r not in ('marcada no Notion', 'decidido'): out[f'a{a["id"]}']['evidencia'] = r[:160]
    for cod, it in (G['fontes_extras'](s, agora) or {}).items():
        it.setdefault('origem', 'extra'); it.setdefault('quem', ''); it.setdefault('concluida', None)
        it.setdefault('prio', 2); it.setdefault('curto', _corta(it['titulo']))
        out[cod] = it
    tit = (s.get('tarefas_notion') or {}).get('titulos') or {}
    lk = (s.get('tarefas_notion') or {}).get('links') or {}
    pend = {p['chave']: p for p in s.get('pendencias') or []}
    for cod, it in out.items():
        it['curto'] = tit.get(cod) or it['curto']
        it['link'] = lk.get(cod) or it.get('link') or G['link_auto'](it['titulo']) or (pend.get(cod) or {}).get('link') or ''
        dm = ((s.get('tarefas_notion') or {}).get('detalhes') or {}).get(cod)
        if dm is not None: it['detalhe_manual'] = dm
        pz = ((s.get('tarefas_notion') or {}).get('prazos') or {}).get(cod)
        it['prazo'] = pz if pz is not None else prazo_do_texto(it['titulo'], it.get('entrou'))
        pe = pend.get(cod) or {}
        if pz is None and not it['prazo'] and pe.get('fonte') == 'calendar':     # convite: responder ate o dia da reuniao
            import re
            m = re.search(r'(\d{2})/(\d{2})', pe.get('assunto') or '')
            if m: it['prazo'] = datetime(MESES_ANO, int(m.group(2)), int(m.group(1))).date().isoformat()
    return out


MESES_ANO = 2026
FONTES_MENSAGEM = ('slack', 'teams', 'chat', 'google_chat', 'email', 'gmail')


def prazo_da_pendencia(p):
    """The Prazo (Planou deadline, YYYY-MM-DD) of a pending item, or None: the one its source recorded (the minutes'
    action, resolved against the meeting day), else a request in a message ("até sexta", "prazo 03/10", "by Friday")
    resolved against the day the message was written. An action of older minutes (before the field existed) is resolved
    from its "· prazo <cell>" against the meeting day in its ref. A goodbye ("valeu, até amanhã") is no deadline, and a
    message without its own time (p['msg']['em']) gets none. Never invented: no clear deadline, None."""
    from . import deadlines
    if deadlines.valid(p.get('deadline')): return p['deadline']
    fonte, assunto = p.get('fonte') or '', p.get('assunto') or ''
    if fonte in FONTES_MENSAGEM:
        # against when THAT message was written (the source keeps it with its link); `desde` is when the conversation
        # started waiting and may be weeks older than the text in `assunto`: without the message time, no deadline
        em = (p.get('msg') or {}).get('em')
        return deadlines.resolve(assunto, em, message=True) if em else None
    if fonte == 'reuniao' and ' · prazo ' in assunto:
        dia = str(p.get('ref') or '')[:10]
        return deadlines.resolve(assunto.rsplit(' · prazo ', 1)[1], dia if deadlines.valid(dia) else p.get('desde'),
                                 marker=False)
    return None


def prazo_do_texto(texto, entrou=None):
    """Prazo (AAAA-MM-DD) lido do texto: "prazo semana de 28/09" (= sexta dessa semana), "até/antes de dd/mm",
    "prazo dd/mm", "até amanhã"/"até hoje" (relativo ao dia em que o item entrou). '' se nao ha. Override: --prazo."""
    import re
    t = ' '.join((texto or '').lower().split())
    def dt(dd, mm, aa=None):
        try: return datetime(int(aa) if aa else MESES_ANO, int(mm), int(dd)).date()
        except ValueError: return None
    m = re.search(r'semana de (\d{1,2})/(\d{1,2})(?:/(\d{4}))?', t)
    if m and (d := dt(*m.groups())): return (d + timedelta(days=(4 - d.weekday()) % 7)).isoformat()
    m = re.search(r'(?:prazo|até|ate|antes de|antes da|antes do)\s+(?:o dia |a |dia )?(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', t)
    if m and (d := dt(*m.groups())): return d.isoformat()
    if entrou and re.search(r'até amanhã|ate amanha', t):
        return _dia_util_seguinte(datetime.fromisoformat(entrou).astimezone(BRT).date()).isoformat()
    if entrou and re.search(r'até hoje|ate hoje', t):
        return datetime.fromisoformat(entrou).astimezone(BRT).date().isoformat()
    return ''


def _dia_util_seguinte(d):
    d += timedelta(days=1)
    while d.weekday() >= 5: d += timedelta(days=1)
    return d


def dias_uteis(de, ate):
    """Dias uteis inteiros entre duas datas (de exclusivo, ate inclusivo)."""
    n, d = 0, de
    while d < ate:
        d += timedelta(days=1)
        if d.weekday() < 5: n += 1
    return n


# ---------------- ids sequenciais AUR-N (23/09/2026: "nao seria melhor trabalharmos com ids sequenciais, tipo aur-213") --------
# Um codigo so por tarefa, dado quando ela entra na base e nunca renumerado; o prefixo da empresa evita colisao entre agents
# (21/09: um `a3-C` da secao BRV foi lido como resposta de outra instancia). As chaves internas aN/pN continuam no estado e nos
# comandos do agent; todo comando do tarefas_notion.py aceita AUR-N ou aN/pN.

def ids(s, agora=None):
    """{cod_interno: 'AUR-N'}; atribui os que faltam em ordem de entrada. `agora` = o mesmo relogio de itens() (sem
    ele, uma fonte extra que congela a lista do dia, como a lista curta do job-scout, congelaria a do dia de hoje)."""
    st = s.setdefault('tarefas_notion', {}); m = st.setdefault('ids', {})
    novos = [(it.get('entrou') or '', c) for c, it in _itens_crus(s, agora).items() if c not in m]
    for _, c in sorted(novos):
        st['seq'] = st.get('seq', 0) + 1; m[c] = st['seq']
    return {c: f'{SIGLA}-{n}' for c, n in m.items()}


def ids_gravados(s):
    """{cod_interno: 'AUR-N'} of the tasks already numbered, without numbering new ones (ids() does that)."""
    m = (s.get('tarefas_notion') or {}).get('ids') or {}
    return {c: f'{SIGLA}-{n}' for c, n in m.items()}


def interno(s, x):
    """'AUR-12' / 'aur-12' / 'a6' / 'p13' -> chave interna ('a6')."""
    import re
    m = re.fullmatch(r'(?i)' + SIGLA + r'-(\d+)', (x or '').strip())
    if not m: return x
    n = int(m.group(1))
    return next((c for c, v in ((s.get('tarefas_notion') or {}).get('ids') or {}).items() if v == n), x)


MAXT = 80

def _corta(t, n=MAXT):
    t = ' '.join((t or '').split())
    return t if len(t) <= n else t[:n - 1].rstrip(' ,;:—-') + '…'


def _antes(t, seps=(' — ', ' (', ' · ', ': ')):
    """Primeira oracao: ate o primeiro separador que deixe pelo menos 25 caracteres."""
    t = ' '.join((t or '').split()); corte = len(t)
    for sp in seps:
        i = t.find(sp)
        if 25 <= i < corte: corte = i
    return t[:corte]


def curto_acao(a):
    """Titulo curto de uma acao (o texto inteiro vai para Detalhe). Override manual: tarefas_notion.py --titulo aN "..."."""
    t = _antes(a['texto'], (' — ', ' (')) if a.get('balde') != 'decisao' else _antes(a['texto'], (' — ', ' ('))
    if a.get('balde') == 'decisao' and not t.lower().startswith(('decid', R('decidir').strip(': ').lower())): t = R('decidir') + t
    return _corta(t)


def curto_pend(p):
    import re
    # 'autor' = quem escreveu a ultima mensagem (a fonte grava desde 26/09/2026); 'quem' e' o nome do chat, que num grupo
    # sem nome e' a lista de participantes — "Responder <primeiro participante>" saia para a mensagem de outra pessoa (25/09)
    fonte, assunto = p.get('fonte'), ' '.join((p.get('assunto') or '').split())
    quem = (p.get('autor') or p.get('quem') or '').split(' - ')[0]
    nome = ''
    if quem and not quem.startswith('('):
        nome = quem.split('@')[0].split(',')[0].split()[0]
        if '.' in nome: nome = [x for x in nome.split('.') if x != 'ext'][0].capitalize()   # ext.mauricio.tomida -> Mauricio
    limpo = re.sub(r'^((ENC|RE|FW|FWD|RES|Convite|Invitation)\s*:\s*)+', '', assunto, flags=re.I)
    if fonte == 'calendar':
        m = re.search(r'(\d{2}/\d{2})', assunto)
        return _corta(f'Responder convite: {limpo.split(" · ")[0]}' + (f' ({m.group(1)})' if m else ''))
    if fonte == 'email':
        return _corta(f'Responder e-mail{" de " + nome if nome else ""}: {limpo}')
    if fonte == 'reuniao':
        return _corta(_antes(limpo))
    if fonte == 'teams':
        return _corta(f'Responder{" " + nome if nome else ""} no Teams: {limpo}')
    return _corta(limpo)


def _dia_util_anterior(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d


def tick(s, dry=False, aplicar=None):
    """So a pagina do dia (23/09/2026 ~20:50, decisao A: "se tudo esta no estado, o banco no Notion e' redundante" — a base
    Tarefas e o Kanban sairam; o estado e' a unica fonte). Atribui os AUR-N novos, le as caixinhas/opcoes marcadas na
    pagina e regera a secao. Plano do dia manual: --hoje AUR-N (fica ate a tarefa sair)."""
    st = s.setdefault('tarefas_notion', {}); ids(s)
    its = _itens_crus(s)
    for c in [c for c in (st.get('plano') or {}) if (its.get(c) or {}).get('status') not in ABERTOS]: st['plano'].pop(c)
    return publica_pagina(s, _tok(), dry, aplicar)


# ---------------- checklist nativo na pagina do dia (23/09/2026, ~19:30) ----------------
# A tabela vinculada funcionava mas "nao ficou tao bonito quanto era antes" (print do checklist da HAK: caixinha nativa,
# riscado, codigo em destaque). A secao `## AUR` volta a ser checklist NATIVO, mas GERADO pelo agent a partir da base a
# cada tick (ninguem reescreve texto): fala + Impedimentos / A fazer / Feito hoje / Aguardando por / Ontem. Marcar a
# caixinha na pagina = baixa no tick seguinte. Pagina de dia anterior fica congelada (diario). O Kanban fica na base.

def _efetivo(it, plano):
    """'A fazer' marcado com --hoje (ou "AUR-N hoje" no chat) conta como Fazendo: entra no plano do dia."""
    return 'Fazendo' if it['status'] == 'A fazer' and plano else it['status']


def _hhmm(iso):
    try: return datetime.fromisoformat(iso).astimezone(BRT).strftime('%H:%M')
    except (TypeError, ValueError): return ''


def _ddmm(iso):
    try: return datetime.fromisoformat(iso).astimezone(BRT).strftime('%d/%m')
    except (TypeError, ValueError): return ''


def opcoes(a):
    """Opcoes de uma decisao: `a['opcoes']` (gravadas por --opcoes aN "A*: texto" "B: texto"; * = recomendada) ou lidas do
    texto: "(A, recomendada) x; (B) y" / "A = x; B = y (recommended)". [(letra, texto, recomendada)]."""
    import re
    if a.get('opcoes'): return [(o['k'], o['texto'], o.get('rec', False)) for o in a['opcoes']]
    t = ' '.join((a.get('texto') or '').split())
    partes = re.split(r'\(([A-D])(,\s*recomendad[ao])?\)\s*', t)
    if len(partes) >= 5:
        out = []
        for i in range(1, len(partes) - 1, 3):
            out.append((partes[i], partes[i + 2].strip(' ;,.'), bool(partes[i + 1])))
        return out
    partes = re.split(r'(?:^|[;:—]\s*)([A-D])\s*=\s*', t)
    if len(partes) >= 5:
        out = []
        for i in range(1, len(partes) - 1, 2):
            x = partes[i + 1].strip(' ;,.')
            rec = bool(re.search(r'\(?(recomendad[ao]|recommended)\)?', x, re.I))
            out.append((partes[i], re.sub(r'\s*\(?(recomendad[ao]|recommended)\)?', '', x, flags=re.I).strip(' ;,.'), rec))
        return out
    return []


# o que o AGENT precisa do usuario (23/09/2026: "se o vigia precisar de alguma acao minha, isso deveria estar ali tb, em
# A fazer, me listando as opcoes e indicando a recomendada"): credencial vencida, fonte quebrada, verificador sem acesso.
# Somem sozinhas quando a condicao some. Codigo `v-<chave>`.
def necessidades(s):
    """[(codigo, texto_md, prazo_iso|'', [(letra, opcao, rec)])] do que o agent precisa do usuario agora: fonte do tick
    quebrada (`s['quebrado']`, gravado pelo agent e limpo quando a fonte roda limpa) + o que o agent acrescenta."""
    out = []
    for f, q in sorted((s.get('quebrado') or {}).items()):
        if f == 'tarefas': continue
        out.append((f'v-{f}', f'Agent sem a fonte **{f}** desde {_ddmm(q.get("desde"))}: {G["conserto"].get(f, (q.get("erro") or "")[:120])}', '', []))
    return out + list(G['necessidades'](s) or [])


def _autolink(t):
    """Refs do agent (gancho `autolink`: MR, work item...) e URL solta viram link markdown (git-dev sempre https)."""
    import re
    t = G['autolink'](t)
    t = re.sub(r'(?<![\w/(\[])(https?://[^\s)\]"]+)',
               lambda m: f'[{m.group(1).split("://", 1)[1][:60]}]({m.group(1).replace("http://git-dev", "https://git-dev")})', t)
    return t


# 23/09/2026: "deixar os titulos muito longos dificulta a leitura, deveria ter um tamanho suficiente pra nao dar quebra de
# linha" — a linha inteira (codigo + titulo + detalhe + prazo/quem) cabe em LINHA caracteres visiveis (pagina em largura
# total no Notion do usuario ≈ 150 por linha); o texto inteiro fica na coluna Detalhe da base
LINHA = 140


def detalhe(it, cabe=400):
    """Texto inteiro do item sem repetir o comeco que ja esta no titulo curto; ate ~400 caracteres. Override:
    --detalhe AUR-N "texto" ("-" = sem detalhe; vazio = automatico). Feito ganha a evidencia da baixa."""
    import re
    evt = f'({it["evidencia"]})' if it.get('status') == 'Feito' and it.get('evidencia') else ''
    def monta(txt):
        txt = (txt or '').strip()
        if evt:                                  # evidencia primeiro (e' o que prova o feito); o texto usa o que sobrar
            if len(evt) >= cabe: return f'*{_corta(evt, max(cabe, 12))}*'
            txt = _corta(txt, cabe - len(evt) - 1) if txt and cabe - len(evt) - 1 >= 12 else ''
            return (_autolink(txt) + ' ' if txt else '') + f'*{evt}*'
        return _autolink(_corta(txt, cabe)) if txt and cabe >= 12 else ''
    if it.get('detalhe_manual') is not None:
        return monta(it['detalhe_manual'] if it['detalhe_manual'] not in ('', '-') else '')
    t = ' '.join((it.get('titulo') or '').split())
    base = re.sub(r'^(Decidir|Decidido|Responder[^:]*):\s*', '', it['curto']).rstrip('…').strip()
    if base and t.lower().startswith(base.lower()):
        resto = t[len(base):]
        # titulo curto cortado no meio da palavra ("…ev…"): o detalhe nao comeca com o pedaco que sobrou ("ents inside")
        if it['curto'].endswith('…') and resto[:1].isalnum() and base[-1:].isalnum():
            resto = resto.split(' ', 1)[1] if ' ' in resto else ''
        t = resto
    t = t.lstrip(' —:-,;·').strip()
    if it['status'] == 'Aguardando' and it.get('quem') and t.endswith(it['quem']): t = t[:-len(it['quem'])].rstrip(' —')
    # tira do comeco do detalhe as oracoes que so repetem o titulo (>= 60% das palavras ja estao nele)
    wt = _palavras(it['curto'])
    while t:
        m = re.match(r'(.+?)(\s+[—·]\s+|:\s+|;\s+|\s+(?=\()|$)', t)
        oracao = m.group(1)
        wo = _palavras(oracao)
        if wo and len(wo & wt) / len(wo) >= 0.6: t = t[m.end():].lstrip(' —:-,;·')
        else: break
    if it['status'] in ('A fazer', 'Aguardando') and (it.get('quem') or '') and t.strip() == it['quem']: t = ''
    if not t or t.lower() in it['curto'].lower(): t = ''
    return monta(t.replace('[', '(').replace(']', ')'))


def render_md(s, data):
    """Markdown das listas da secao AUR da pagina `data`, a partir dos itens do agent (= linhas da base)."""
    st = s.get('tarefas_notion') or {}; plano = st.get('plano') or {}
    d = datetime.fromisoformat(data).date(); ontem = _dia_util_anterior(d)
    its = itens(s)
    def dia(it): return datetime.fromisoformat(it['concluida']).astimezone(BRT).date() if it.get('concluida') else None
    def tit(it):
        # 23/09/2026: "deixar com tantos detalhes como a versao anterior — encurtamos so por causa das tabelas": na pagina
        # vai o titulo curto em negrito (com o link) + o texto inteiro com MRs/URLs viradas link; na base segue o curto
        t = it['curto'].replace('`', "'").replace('[', '(').replace(']', ')')
        t = R('decidido') + t[len(R('decidir')):] if it['status'] == 'Feito' and t.startswith(R('decidir')) else t
        cab = f'[**{t}**]({it["link"]})' if it.get('link') else f'**{t}**'
        ex = G['extra_linha'](s, it) or ''
        det = detalhe(it, LINHA - len(it['uid']) - len(t) - 3 - sufixo(it) - len(ex))
        return cab + (f' — {det}' if det else '') + ex
    def sufixo(it):                     # o que vem depois do texto na linha (prazo, quem/desde, ✓ hora) — cabe no limite
        n = len(pz(it).replace('*', ''))
        if it['status'] == 'Aguardando': n += len(f' — {it.get("quem") or "?"} · desde 00/00 · ⏰ 0d')
        if it['status'] == 'Feito': n += len(' — ✓ 00:00')
        return n
    def vencida(it): return bool(it.get('prazo')) and it['prazo'] < d.isoformat()
    def ordem(kv): return (not vencida(kv[1]), kv[1]['prio'], kv[1].get('prazo') or '9', kv[1].get('entrou') or '')
    def pz(it):
        if not it.get('prazo') or it['status'] not in ABERTOS: return ''
        dd = datetime.fromisoformat(it['prazo']).strftime('%d/%m')
        return f' · ⚠️ **{R("venceu")} {dd}**' if vencida(it) else (f' · **{R("ate_hoje")}**' if it['prazo'] == d.isoformat() else f' · {R("ate")} {dd}')
    imp, fazer, dec, agu, feito, ont, backlog = [], [], [], [], [], [], []
    secoes = {}                     # item aberto com 'secao' vai para um bloco proprio (ex.: Shortlist do job-scout)
    # plano do dia (23/09/2026: "nao existe um Hoje de verdade" — A fazer era o backlog inteiro): Fazendo, prazo ate o
    # proximo dia util (ou vencido), decisoes e o que o agent precisa; o resto vai para o toggle "Backlog (N)"
    lim = _dia_util_seguinte(d).isoformat()
    def no_plano(it): return bool(it.get('prazo')) and it['prazo'] <= lim
    for cod, it in sorted(its.items(), key=ordem):
        ef = _efetivo(it, plano.get(cod))
        if ef == 'Impedimento': imp.append(f'- `{it["uid"]}` {tit(it)}{pz(it)}')
        elif ef == 'Fazendo': fazer.insert(0, f'- [ ] `{it["uid"]}` {tit(it)}{pz(it)}')
        elif ef == 'A fazer' and it.get('secao'): secoes.setdefault(it['secao'], []).append(f'- [ ] `{it["uid"]}` {tit(it)}{pz(it)}')
        elif ef == 'A fazer' and no_plano(it): fazer.append(f'- [ ] `{it["uid"]}` {tit(it)}{pz(it)}')
        elif ef == 'A fazer': backlog.append(f'\t- [ ] `{it["uid"]}` {tit(it)}{pz(it)}')
        elif ef == 'Decisão':
            a = next((x for x in s.get('acoes') or [] if f'a{x["id"]}' == cod), {})
            dec.append(f'- `{it["uid"]}` *({R("decisao")})* {tit(it)}{pz(it)}')
            for k, txt, rec in opcoes(a):
                dec.append(f'\t- [ ] `{it["uid"]}-{k}` ' + (f'**{R("recomendada")}** — ' if rec else '') + txt)
        elif ef == 'Aguardando':
            quem = it.get('quem') or '?'
            n = dias_uteis(datetime.fromisoformat(it['entrou']).astimezone(BRT).date(), d) if it.get('entrou') else 0
            idade = f' · ⏰ **{n}d**' if n >= COBRAR_DIAS else ''
            agu.append(f'- [ ] `{it["uid"]}` {tit(it)} — **{quem}** · {R("desde")} {_ddmm(it.get("entrou"))}{idade}{pz(it)}')
        elif ef == 'Feito' and dia(it) == d: feito.append((it['concluida'], f'- [x] `{it["uid"]}` {tit(it)} — ✓ {_hhmm(it["concluida"])}'))
        elif ef == 'Feito' and dia(it) == ontem: ont.append((it['concluida'], f'- `{it["uid"]}` {tit(it)}'))
    feito = [x for _, x in sorted(feito)]; ont = [x for _, x in sorted(ont)]
    # Top do dia (24/09/2026, pedido do usuario: "as TOP 3 tarefas do dia, que se forem concluidas o dia valeu a pena —
    # as mais importantes e menos possiveis de fazer outro dia; nao force 3"). Escolha e' minha (--top), ate 3, pode ser 0.
    topo = []
    for cod in ((st.get('top') or {}).get(data) or [])[:3]:
        it = its.get(cod)
        if not it: continue
        feitoh = it['status'] == 'Feito'
        topo.append(f'- [{"x" if feitoh else " "}] `{it["uid"]}` {tit(it)}' + (f' — ✓ {_hhmm(it["concluida"])}' if feitoh and it.get('concluida') else pz(it)))
    out = ([f'**{R("top")}**'] + topo + [''] if topo else [])
    if imp or G['speech']: out += [f'**{R("impedimentos")}**'] + (imp or [f'- {R("nenhum")}'])
    vig = []
    for cod, txt, prazo, ops in necessidades(s):
        pzv = ''
        if prazo:
            pzv = f' · ⚠️ **{R("venceu")} {prazo[8:]}/{prazo[5:7]}**' if prazo < d.isoformat() else f' · {R("ate")} {prazo[8:]}/{prazo[5:7]}'
        vig.append(f'- `{cod}` *({R("agent")})* {txt}{pzv}')
        for k, t, rec in ops: vig.append(f'\t- [ ] `{cod}-{k}` ' + (f'**{R("recomendada")}** — ' if rec else '') + t)
    out += (['', f'**{R("a_fazer")}**'] if out else [f'**{R("a_fazer")}**']) + (vig + dec + fazer or ['- ' + R('plano_vazio').format(sigla=SIGLA)])
    if backlog: out += [f'▸ {R("backlog")} ({len(backlog)})'] + backlog
    for nome, linhas in secoes.items(): out += ['', f'**{nome}**'] + linhas
    if feito: out += ['', f'**{R("feito")}**'] + feito
    out += ['', f'**{R("aguardando")}**'] + (agu or [f'- {R("nada")}'])
    if ont and G['speech']: out += ['', f'**{R("ontem")}** ({ontem:%d/%m})'] + ont
    out += list(G['rodape'](s, data) or [])
    return '\n'.join(out)


def _fala(s, data):
    """Fala (paragrafo) da pagina `data`: a escrita por --daily md para ela, ou a guardada na ultima geracao."""
    st = s['tarefas_notion']; falas = st.setdefault('fala', {})
    d = s.get('daily') or {}
    f = ((d.get('heads') or {}).get(data) or '').strip()
    if not f and (d.get('notion') or {}).get('data') == data and '**A fazer**' not in (d.get('notion_md') or ''):
        f = (d.get('notion_md') or '').strip()
    for corte in ('**Impedimentos**', '**Ontem**', '**Blockers**', '**Yesterday**', '**Today**'):
        f = f.split(corte)[0]
    f = f.strip()
    if f: falas[data] = f
    for k in [k for k in falas if k < data]: falas.pop(k)
    return falas.get(data, '')


def le_caixinhas(s, tok, pid, aplicar):
    """to_do marcado na pagina com codigo `aN`/`pN` de item ainda aberto = baixa (como a caixinha da base)."""
    import re
    _, corpo = nd.secao(pid, SIGLA, tok)
    its = itens(s)
    # reopened elsewhere (Planou, 28/09/2026): its box is still ticked on the page until the section is published again,
    # and must not close it a second time
    reab = (s.get('tarefas_notion') or {}).get('reaberta') or {}
    abertos = {c for c, it in its.items() if it['status'] in ABERTOS and c not in reab}
    rx = re.compile(r'\s*(' + SIGLA + r'-\d+)\b')
    out = []
    for b in corpo:
        if b['type'] != 'to_do' or not b['to_do'].get('checked'): continue
        m = rx.match(''.join(t.get('plain_text', '') for t in b['to_do']['rich_text']))
        c = interno(s, m.group(1)) if m else None
        if c in abertos and aplicar and aplicar(c, 'Feito'):
            out.append(f'-- {m.group(1)} ({c}) marcada na pagina do dia → baixa no agent')
    # opcao marcada numa decisao (`AUR-12-B`): uma linha por escolha — agir e depois --acao done N "<opcao>"
    dec_esc = s['tarefas_notion'].setdefault('dec_escolha', {})
    for b in corpo:
        if b['type'] != 'bulleted_list_item' or not b.get('has_children'): continue
        m = rx.match(nd._texto(b))
        c = interno(s, m.group(1)) if m else None
        if not c or (its.get(c) or {}).get('status') != 'Decisão': continue
        for x in nd._filhos(b['id'], tok):
            if x['type'] != 'to_do' or not x['to_do'].get('checked'): continue
            mm = re.match(r'\s*' + SIGLA + r'-\d+-([A-Z])\b', nd._texto(x))
            if mm and dec_esc.get(c) != mm.group(1):
                dec_esc[c] = mm.group(1)
                out.append(f'-- DECISAO {m.group(1)} ({c}) = {mm.group(1)}: {nd._texto(x)[:140]} — agir e --acao done {c[1:]} "<opcao>"')
    # opcao marcada numa necessidade do agent (`v-sso-B`): uma linha por escolha, para eu executar no tick
    # (as opcoes de decisao `aN-X` sao lidas pelo gate de watch_core.daily, imprime_gate_decisoes)
    esc = s['tarefas_notion'].setdefault('v_escolha', {})
    for b in corpo:
        if b['type'] != 'bulleted_list_item' or not b.get('has_children'): continue
        m = re.match(r'\s*(v-[a-z]+)\b', nd._texto(b))
        if not m: continue
        for x in nd._filhos(b['id'], tok):
            if x['type'] != 'to_do' or not x['to_do'].get('checked'): continue
            mm = re.match(r'\s*(v-[a-z]+)-([A-Z])\b', nd._texto(x))
            if mm and esc.get(mm.group(1)) != mm.group(2):
                esc[mm.group(1)] = mm.group(2)
                out.append(f'-- AGENT PRECISA: {mm.group(1)} = {mm.group(2)} marcada na pagina ({nd._texto(x)[:120]}) — executar')
    return out


def publica_pagina(s, tok, dry=False, aplicar=None):
    """Gera a secao AUR da pagina do dia a partir da base e republica so quando mudou."""
    from . import daily as dvm
    st = s.setdefault('tarefas_notion', {}); data = dvm.data_daily()
    if G['inicio'] and data < G['inicio']: return []          # antes da virada deste agent, o fluxo antigo publica
    d = s.setdefault('daily', {})
    fala = _fala(s, data) if G['speech'] else ''
    # pagina/secao do dia nascem aqui quando ja ha fala para ela (escrita no fechamento com --daily md); sem fala e sem
    # secao, espera (a daily do dia ainda nao foi escrita). Secao sem fala (speech=False) entra na pagina que ja existe
    pid = nd.pagina_do_dia(data, d.get('notion_pagina') or nd.MES_DEFAULT, tok, criar=bool(fala) and not dry)
    if not pid: return []
    head, corpo = nd.secao(pid, SIGLA, tok)
    if not head and not fala and G['speech']: return []
    out = le_caixinhas(s, tok, pid, aplicar) if not dry and head else []
    md = (fala + '\n\n' if fala else '') + render_md(s, data)
    ja = (st.get('render') or {}).get(data)
    tem_tabela = any(b['type'] == 'child_database' for b in corpo)
    if ja == md and not tem_tabela and d.get('notion_md') == md: return out
    if dry: return out + [f'   · republicaria a secao {SIGLA} de {data}']
    for b in corpo:                   # a tabela vinculada (versao anterior) sai
        if b['type'] == 'child_database': nd._req('DELETE', f'{API}/blocks/{b["id"]}', tok)
    r = nd.publicar_diff(pid, SIGLA, md, tok)          # so os blocos que mudaram (23/09/2026)
    st.pop('reaberta', None)                          # the page now shows the reopened tasks unticked
    st.setdefault('render', {})[data] = md
    for k in [k for k in st['render'] if k < data]: st['render'].pop(k)
    d['notion_md'] = md; d['notion'] = {**(d.get('notion') or {}), 'data': data, 'pagina_dia': pid}
    return out + ([f'-- secao {SIGLA} da pagina {data} regerada da base ({r["mudancas"]} bloco(s))'] if ja is None else [])


VERIF_INTERVALO = timedelta(minutes=30)
COBRAR_DIAS = 3                   # Aguardando parado ha N dias uteis: ⏰ na pagina + pedido de rascunho de cobranca


def verifica_links(s, dry=False):
    """Baixa pelo estado do link (MR/PR mergeada) — gancho do agent."""
    return list(G['verifica_links'](s, dry) or [])


def urllib_q(x):
    import urllib.parse; return urllib.parse.quote(x, safe='')


def sem_tarefa(s, dry=False):
    """(fechamento, 1x por dia util a partir das 18:00) Trabalho do dia que nao virou tarefa na base: MR minha criada ou
    mergeada hoje sem tarefa com o link dela, e reuniao de hoje com ata sem tarefa citando o titulo. 23/09/2026: o Ontem
    so mostra o que esta na base — sem isto, o que eu fiz e nao registrei some da daily."""
    import re
    agora = datetime.now(BRT)
    st = s['tarefas_notion']; hoje = agora.date().isoformat()
    if agora.weekday() >= 5 or agora.hour < 18 or st.get('sem_tarefa_dia') == hoje: return []
    if not dry: st['sem_tarefa_dia'] = hoje
    its = itens(s); links = {it.get('link') for it in its.values() if it.get('link')}
    textos = ' '.join((it['curto'] + ' ' + it['titulo']).lower() for it in its.values())
    out = []
    ini = datetime.combine(agora.date(), datetime.min.time(), BRT).astimezone(timezone.utc).isoformat()
    registradas = []
    try:
        for l in G['sem_tarefa_fontes'](s, ini) or []:
            ref, url, txt, quando = (tuple(l) + (None,))[:4]      # 4th, optional: when it closed at the origin (merged_at)
            if url in links: continue
            # MR/PR do dia sem tarefa: vira tarefa sozinha, com o link (26/09/2026: tres MRs mergeadas tiveram que ser
            # cadastradas a mao). Mergeada/concluida = feita; aberta = a fazer.
            if dry:
                out.append(f'-- SEM TAREFA: {ref} — {txt} · {url}'); continue
            from . import daily as dvm
            a = dvm.acao_add(s, _corta(f'{txt} · {ref}', 200))
            st.setdefault('links', {})[f'a{a["id"]}'] = url
            feita = bool(re.search(r'\b(merged|completed|mergeada|concluida)\b', ref, re.I))
            if feita: dvm.acao_done(s, a['id'], ref, quando)
            links.add(url)
            registradas.append(f'-- ✓ registrada {"como feita" if feita else "a fazer"}: a{a["id"]} {ref} — {txt}')
    except (Exception, SystemExit) as e:
        out.append(f'-- SEM TAREFA: nao consegui ler o trabalho do dia ({str(e)[:80]})')
    for ev in ((s.get('gravacoes') or {}).get('eventos') or {}).values():
        if (ev.get('ini') or '')[:10] != hoje or ev.get('estado') not in ('ata_pronta', 'acao_sua'): continue
        chave = ' '.join(w for w in (ev.get('titulo') or '').lower().split() if len(w) > 3)[:40]
        if chave and chave not in textos:
            out.append(f'-- SEM TAREFA: reuniao "{ev["titulo"]}" (ata pronta) — registrar o que saiu dela, se foi trabalho seu')
    if out: out.insert(0, '-- FECHAMENTO: trabalho do dia sem tarefa na base — cadastrar (--acao add + --acao done) antes da fala')
    return registradas + out


def cobrancas(s, dry=False):
    """(3) Aguardando parado >= COBRAR_DIAS dias uteis: uma linha por item por dia util, para eu escrever o rascunho de
    cobranca no chat (o agent nao envia nada)."""
    st = s['tarefas_notion']; ja = st.setdefault('cobrado', {})
    hoje = datetime.now(BRT).date()
    if hoje.weekday() >= 5: return []
    out = []
    for cod, it in itens(s).items():
        if it['status'] != 'Aguardando' or not it.get('entrou'): continue
        n = dias_uteis(datetime.fromisoformat(it['entrou']).astimezone(BRT).date(), hoje)
        if n < COBRAR_DIAS or ja.get(cod) == hoje.isoformat(): continue
        if not dry: ja[cod] = hoje.isoformat()
        out.append(f'-- ⏰ {it["uid"]} ({cod}) aguardando {it.get("quem") or "?"} ha {n} dias uteis: {it["curto"]} — RASCUNHO DE COBRANCA')
    return out


def _palavras(t):
    import re, unicodedata
    t = unicodedata.normalize('NFKD', (t or '').lower()); t = ''.join(c for c in t if not unicodedata.combining(c))
    pare = {'de', 'da', 'do', 'das', 'dos', 'a', 'o', 'e', 'em', 'no', 'na', 'com', 'para', 'pra', 'por', 'os', 'as', 'um',
            'uma', 'que', 'se', 'ao', 'responder', 'fazer', 'the'}
    return {w for w in re.findall(r'[a-z0-9]+', t) if len(w) > 2 and w not in pare}


def duplicadas(s, dry=False):
    """(4) Item aberto novo parecido com outro aberto (>= 60% das palavras do menor em comum): avisa uma vez por par.
    23/09/2026: "Combinar com o colega a virada do GitLab" voltou como p14 pela ata. Juntar: --junta <dup> <fica>."""
    st = s['tarefas_notion']; vistos = set(st.setdefault('dup_avisado', []))
    abertos = [(c, it) for c, it in itens(s).items() if it['status'] in ABERTOS]
    out = []
    for i, (c1, i1) in enumerate(abertos):
        w1 = _palavras(i1['curto'] + ' ' + i1['titulo'][:160])
        for c2, i2 in abertos[i + 1:]:
            par = '|'.join(sorted((c1, c2)))
            if par in vistos: continue
            w2 = _palavras(i2['curto'] + ' ' + i2['titulo'][:160])
            if not w1 or not w2: continue
            # duas pendencias da MESMA fonte com conversa/e-mail diferente nao sao a mesma demanda (AUR-46 x AUR-29, 25/09:
            # mensagens de dois chats do Teams com o mesmo nome no titulo)
            r1, r2 = i1.get('ref') or '', i2.get('ref') or ''
            if r1 and r2 and r1 != r2 and r1.split(':', 1)[0] == r2.split(':', 1)[0]: continue
            if len(w1 & w2) / min(len(w1), len(w2)) >= 0.6:
                novo, velho = sorted((c1, c2), key=lambda c: (dict(abertos)[c].get('entrou') or ''), reverse=True)
                un, uv = dict(abertos)[novo]['uid'], dict(abertos)[velho]['uid']
                out.append(f'-- ≈ {un} parece {uv}: "{dict(abertos)[novo]["curto"]}" × "{dict(abertos)[velho]["curto"]}" '
                           f'— juntar: tarefas_notion.py --junta {un} {uv}')
                if not dry: st['dup_avisado'].append(par)
    return out


def junta(s, dup, fica):
    """Da baixa no duplicado apontando para o que fica (acao: feita com nota; pendencia: ignorar com nota)."""
    from . import daily as dvm
    if dup.startswith('a'):
        return bool(dvm.acao_done(s, int(dup[1:]), f'duplicada de {fica}'))
    p = next((p for p in s.get('pendencias') or [] if p['chave'] == dup and p.get('status') == 'aberta'), None)
    if not p: return False
    p['status'] = 'ignorar'; p['nota'] = f'duplicada de {fica}'; return True


def verifica(s, dry=False):
    """Baixa AUTOMATICA por evidencia (pedido de 23/09/2026: "voce deveria dar baixa automaticamente ao evidenciar que ela
    foi feita, inclusive checando no cluster se as permissoes foram mesmo aplicadas"). Acao aberta com `verifica` (comando
    shell gravado por `--verifica aN "cmd"`) roda a cada 30 min: exit 0 = feito (baixa com a ultima linha da saida como
    evidencia), exit 1 = ainda nao, outro = nao deu para checar (avisa uma vez por erro diferente). Devolve linhas."""
    import subprocess, sys as _s
    from . import daily as dvm
    agora, out = datetime.now(timezone.utc), []
    for a in s.get('acoes') or []:
        cmd = a.get('verifica')
        if a.get('status') != 'aberta' or not cmd: continue
        ult = a.get('verif_ts')
        if ult and agora - datetime.fromisoformat(ult) < VERIF_INTERVALO: continue
        if dry: out.append(f'   · verificaria a{a["id"]}'); continue
        try:
            r = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, timeout=120, cwd=G['verifica_cwd'])
            code, txt = r.returncode, ((r.stdout or r.stderr).strip().splitlines() or [''])[-1][:200]
            import re; txt = re.sub(r'^\[\d\d/\d\d \d\d:\d\d\]\s*', '', txt)   # carimbo do script nao conta como erro novo
        except subprocess.TimeoutExpired:
            code, txt = 124, 'timeout de 120 s'
        a['verif_ts'] = agora.isoformat()
        if code == 0:
            dvm.acao_done(s, a['id'], f'verificado: {txt}')
            out.append(f'-- ✓ a{a["id"]} feita por verificacao: {txt}')
        elif code != 1 and a.get('verif_erro') != txt:
            out.append(f'-- ! a{a["id"]}: verificacao nao rodou ({code}): {txt}')
        a['verif_erro'] = txt if code not in (0, 1) else None
    return out


def top_cobra(s, dry=False):
    """Uma vez por dia util (>= 09:00) cobra a escolha do Top do dia se houver tarefa aberta e o top estiver vazio; e avisa
    uma vez quando todo o top foi concluido. A escolha e' do modelo (criterio: o que mais importa hoje e menos pode
    esperar outro dia — prazo hoje, destrava alguem, pedido da daily/ata); pode ser 0, 1, 2 ou 3."""
    ag = datetime.now(BRT); hoje = ag.date().isoformat()
    if ag.weekday() >= 5 or ag.hour < 9: return []
    st = s.setdefault('tarefas_notion', {}); top = (st.get('top') or {}).get(hoje)
    its = itens(s); av = st.setdefault('top_avisado', {})
    abertos = [c for c, it in its.items() if it['status'] in ABERTOS]
    if top is None:
        if not abertos or av.get('vazio') == hoje: return []
        if not dry: av['vazio'] = hoje
        return [f'-- TOP do dia nao definido ({len(abertos)} tarefas abertas): escolher ate 3 — as mais importantes e que menos '
                f'podem ficar para outro dia (pode ser menos de 3, ou nenhuma: --top -) — tarefas_notion.py --top {SIGLA}-N ...']
    if top and all((its.get(c) or {}).get('status') == 'Feito' for c in top) and av.get('feito') != hoje:
        if not dry: av['feito'] = hoje
        return ['-- TOP do dia concluido: o dia valeu. Se sobrar tempo, puxe o proximo do backlog (--top pode ganhar outro item)']
    return []


# English flags; the Portuguese ones stay as aliases
FLAGS = {'--check': '--verifica', '--title': '--titulo', '--options': '--opcoes', '--today': '--hoje', '--detail': '--detalhe',
         '--merge': '--junta', '--due': '--prazo'}


def cli(vg, aplicar):
    """CLI comum; o wrapper do agent chama cli(agent_module, aplicar(s, cod, status))."""
    sys.argv[:] = [FLAGS.get(x, x) for x in sys.argv]
    s = vg.load_state(); dry = '--dry' in sys.argv
    ids(s)                                      # agent recem-migrado: os <SIGLA>-N nascem aqui para a traducao abaixo
    for _k in ('--verifica', '--link', '--titulo', '--opcoes', '--prazo'):     # AUR-N -> chave interna
        if _k in sys.argv: _i = sys.argv.index(_k); sys.argv[_i + 1] = interno(s, sys.argv[_i + 1])
    if '--junta' in sys.argv:
        _i = sys.argv.index('--junta'); sys.argv[_i + 1] = interno(s, sys.argv[_i + 1]); sys.argv[_i + 2] = interno(s, sys.argv[_i + 2])
    if '--id' in sys.argv:                      # --id AUR-12 | a6  -> mostra as duas formas
        x = sys.argv[sys.argv.index('--id') + 1]; c = interno(s, x); print(c, '=', ids(s).get(c, '?')); vg.save_state(s); sys.exit(0)
    if '--verifica' in sys.argv:                # --verifica aN "comando" (exit 0 = feito; vazio = tira)
        i = sys.argv.index('--verifica'); cod, cmd = sys.argv[i + 1], ' '.join(sys.argv[i + 2:]).strip()
        a = next(x for x in s['acoes'] if f'a{x["id"]}' == cod)
        if cmd: a['verifica'] = cmd
        else: a.pop('verifica', None)
        a.pop('verif_ts', None); print(cod, 'verifica ->', cmd or '(nenhuma)')
    if '--link' in sys.argv:                    # --link aN <url> (vazio = volta ao automatico)
        i = sys.argv.index('--link'); cod, u = sys.argv[i + 1], (sys.argv[i + 2] if len(sys.argv) > i + 2 else '')
        ll = s.setdefault('tarefas_notion', {}).setdefault('links', {})
        if u: ll[cod] = u
        else: ll.pop(cod, None)
        print(cod, 'link ->', u or '(automatico)')
    if '--titulo' in sys.argv:                  # --titulo aN "titulo curto" (vazio = volta ao automatico)
        i = sys.argv.index('--titulo'); cod, t = sys.argv[i + 1], ' '.join(sys.argv[i + 2:]).strip()
        tt = s.setdefault('tarefas_notion', {}).setdefault('titulos', {})
        if t: tt[cod] = t
        else: tt.pop(cod, None)
        print(cod, '->', t or '(automatico)')
    if '--opcoes' in sys.argv:                  # --opcoes aN "A*: texto" "B: texto" (* = recomendada; sem opcoes = limpa)
        i = sys.argv.index('--opcoes'); cod = sys.argv[i + 1]; ops = []
        for x in sys.argv[i + 2:]:
            if x.startswith('--'): break
            k, _, t = x.partition(':'); ops.append({'k': k.strip(' *'), 'texto': t.strip(), 'rec': k.strip().endswith('*')})
        a = next(x for x in s['acoes'] if f'a{x["id"]}' == cod)
        if ops: a['opcoes'] = ops
        else: a.pop('opcoes', None)
        print(cod, 'opcoes ->', [(o['k'], o['rec']) for o in ops])
    if '--top' in sys.argv:                     # --top AUR-1 AUR-7 (ate 3, na ordem) | --top - (hoje sem top, de proposito)
        i = sys.argv.index('--top'); arg = [x for x in sys.argv[i + 1:] if not x.startswith('--')][:3]
        tt = s.setdefault('tarefas_notion', {}).setdefault('top', {}); hoje = datetime.now(BRT).date().isoformat()
        for k in [k for k in tt if k < hoje]: tt.pop(k)
        tt[hoje] = [] if arg == ['-'] else [interno(s, x) for x in arg]
        print('top de hoje ->', [ids(s).get(c, c) for c in tt[hoje]] or '(vazio)')
    if '--hoje' in sys.argv:                    # --hoje AUR-N (poe no plano do dia) | --hoje AUR-N - (tira)
        i = sys.argv.index('--hoje'); cod = interno(s, sys.argv[i + 1]); tira = len(sys.argv) > i + 2 and sys.argv[i + 2] == '-'
        pl = s.setdefault('tarefas_notion', {}).setdefault('plano', {})
        if tira: pl.pop(cod, None)
        else: pl[cod] = datetime.now(BRT).date().isoformat()
        print(cod, 'plano ->', 'fora' if tira else 'hoje')
    if '--detalhe' in sys.argv:                 # --detalhe AUR-N "texto" | "-" (sem) | (nada = automatico)
        i = sys.argv.index('--detalhe'); cod = interno(s, sys.argv[i + 1]); t = ' '.join(sys.argv[i + 2:]).strip()
        dd = s.setdefault('tarefas_notion', {}).setdefault('detalhes', {})
        if t: dd[cod] = t
        else: dd.pop(cod, None)
        print(cod, 'detalhe ->', t or '(automatico)')
    if '--junta' in sys.argv:                   # --junta <duplicado> <o que fica>
        i = sys.argv.index('--junta'); print('junta', sys.argv[i + 1], '->', sys.argv[i + 2], junta(s, sys.argv[i + 1], sys.argv[i + 2]))
    if '--prazo' in sys.argv:                   # --prazo aN dd/mm[/aaaa] | --prazo aN - (sem prazo) | --prazo aN (automatico)
        i = sys.argv.index('--prazo'); cod = sys.argv[i + 1]; v = sys.argv[i + 2] if len(sys.argv) > i + 2 and not sys.argv[i + 2].startswith('--') else None
        pp = s.setdefault('tarefas_notion', {}).setdefault('prazos', {})
        if v is None: pp.pop(cod, None)
        elif v == '-': pp[cod] = ''
        else:
            dd, mm, *aa = v.split('/'); pp[cod] = datetime(int(aa[0]) if aa else MESES_ANO, int(mm), int(dd)).date().isoformat()
        print(cod, 'prazo ->', pp.get(cod, '(automatico)'))
    for ln in (verifica(s, dry) + verifica_links(s, dry) + tick(s, dry, lambda c, st: aplicar(s, c, st))
               + duplicadas(s, dry) + cobrancas(s, dry) + top_cobra(s, dry) + sem_tarefa(s, dry)) or ['   · nada mudou']: print(ln)
    if not dry: vg.save_state(s)
