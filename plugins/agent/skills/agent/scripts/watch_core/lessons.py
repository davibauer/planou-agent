#!/usr/bin/env python3
"""Licoes aprendidas — biblioteca compartilhada dos agents (uma copia, todos os agents do time).

Pedido do usuario em 21/09/2026: "ao final de cada dia, levantar as licoes aprendidas — o que voce faria diferente
amanha para depender menos da minha interacao; quero que voce aprenda comigo e faca as coisas de forma cada vez
mais autonoma com o tempo" e, no mesmo dia: "as licoes aprendidas precisa ser algo que funcione igual em todos os
vigias, pra nao termos isso duplicado em cada um" (os vigias de entao sao os agents de hoje).

O que fica aqui (e so aqui):
- o fechamento do dia (`tick`): no primeiro tick >= LICOES_HORA em dia util, reimprime a licao do dia util anterior
  (para conferir se foi aplicada) e, se a de hoje nao foi gravada, pede — com as pistas que o estado tem;
- a gravacao (`set`): o texto do dia vai para o estado do agent E para o historico compartilhado lessons.md
  (`## dd/mm/aaaa · <agent>`), uma linha por licao no formato
  `[aplicada em <onde>|proposta|recusada|a aplicar] <o que exigiu o usuario> → <o que muda>`.
  O cabecalho grava sempre o nome canonico do agent (work-watch-acme, job-scout...: agents.canonical_agent); as
  secoes antigas com o nome curto (acme, linkedin) continuam valendo na leitura;
- as regras gerais que valem para todos os agents ficam em rules.md (a licao que nasce num agent e vale para os
  outros entra la, uma vez); a regra especifica de um agent fica nas instrucoes dele;
- `propostas`: as linhas `[proposta]` ainda abertas de todos os agents (voltam na retro seguinte ate virar
  `[aplicada …]` ou `[recusada]`).

Uso pelo codigo de cada agent:
    from watch_core import lessons as lv
    ... no argparse: ap.add_argument('--licoes', nargs='*')
    ... if a.licoes is not None: lv.cli(a.licoes, s, save_state, AGENT); return
    ... no fechamento (junto com o == DAILY): algo |= lv.tick(s, AGENT, dry)
"""
import os, re
from datetime import datetime, timezone, timedelta

from . import config
from .agents import canonical_agent, same_agent

LICOES_MD = config.file(config.LESSONS_MD)       # historico compartilhado (todos os agents), dado do usuario
REGRAS_MD = config.file(config.RULES_MD)         # regras gerais aprendidas (valem para todos), dado do usuario
BRT = timezone(timedelta(hours=-3))
LICOES_HORA = 18                                # primeiro tick >= 18:00 BRT em dia util
STATUS_RE = re.compile(r'^\s*-\s*\[(aplicada[^\]]*|proposta|recusada|a aplicar|decis[aã]o[^\]]*)\]', re.I)


def _agora(): return datetime.now(BRT)
def _hoje(): return _agora().date().isoformat()
def _br(d): return f'{d[8:10]}/{d[5:7]}/{d[:4]}'


def _agent_da_secao(sec):
    """'27/09/2026 · acme' -> 'acme' (o nome como esta gravado; comparar com same_agent)."""
    return sec.split('·', 1)[1].strip() if '·' in sec else ''


def licoes(s): return s.setdefault('licoes', {})


def licoes_set(s, agent, texto):
    """Grava a licao de hoje no estado do agent e no lessons.md compartilhado (substitui a secao do dia se ja existe,
    tambem a gravada com o nome antigo do mesmo agent)."""
    agent = canonical_agent(agent)
    licoes(s)[_hoje()] = {'texto': texto, 'gravada': datetime.now(timezone.utc).isoformat()}
    cab = f'## {_br(_hoje())} · {agent}'
    corpo = texto.strip()
    if not any(l.lstrip().startswith('-') for l in corpo.splitlines()):   # prosa: vira um item por linha
        corpo = '\n'.join(f'- {l.strip()}' for l in corpo.splitlines() if l.strip())
    try: atual = open(LICOES_MD, encoding='utf-8').read()
    except FileNotFoundError:
        atual = '# Lições aprendidas — todos os agents\n\nUma seção por dia e agent (`## dd/mm/aaaa · agent`). Formato da linha: `[aplicada em <onde>|proposta|recusada|a aplicar] lição → mudança`.\n"aplicada" = já está em SKILL.md/script/memória (dizer onde); "proposta" = amplia o que o agent faz sozinho e espera o ok do usuário; "a aplicar" = dívida que o fechamento seguinte cobra. Regras gerais (valem para todos) em rules.md.\n'
    secoes = re.split(r'(?m)^(?=## )', atual)

    def _do_dia(x):
        c = x.split('\n', 1)[0].strip()
        if not c.startswith(f'## {_br(_hoje())} ·'): return False
        return same_agent(_agent_da_secao(c[3:]), agent)
    secoes = [x for x in secoes if not _do_dia(x)]
    novo = ''.join(secoes).rstrip('\n') + f'\n\n{cab}\n\n{corpo}\n'
    os.makedirs(os.path.dirname(LICOES_MD), exist_ok=True)
    open(LICOES_MD, 'w', encoding='utf-8').write(novo)


def licoes_ultimas(s, n=5):
    return sorted(licoes(s).items())[-n:]


def propostas(agent=None):
    """Linhas [proposta] ainda abertas no lessons.md (de todos os agents, ou so de um — nome novo ou antigo), com a
    data/agent da secao."""
    try: txt = open(LICOES_MD, encoding='utf-8').read()
    except FileNotFoundError: return []
    out, sec = [], ''
    for l in txt.splitlines():
        if l.startswith('## '): sec = l[3:].strip(); continue
        m = STATUS_RE.match(l)
        if m and m.group(1).lower() == 'proposta' and (not agent or same_agent(_agent_da_secao(sec), agent)):
            out.append((sec, l.strip()[len(m.group(0)):].strip()))
    return out


PLANOU_DIAS = 7                 # dias de licoes deste agent que sobem para o Planou (aba Papel, Memoria)
PLANOU_MAX = 32_000             # bytes: o limite do Planou para o texto do arquivo lessons


def recentes(agent, dias=PLANOU_DIAS, max_bytes=PLANOU_MAX, hoje=None):
    """As secoes deste agent (nome novo ou antigo, como em propostas) dos ultimos `dias`, da mais nova para a mais
    antiga, no formato do lessons.md; corta as mais antigas para caber em `max_bytes`. '' quando nao ha nenhuma."""
    try: txt = open(LICOES_MD, encoding='utf-8').read()
    except FileNotFoundError: return ''
    hoje = hoje or _agora().date()
    secs = []
    for bloco in re.split(r'(?m)^(?=## )', txt):
        cab = bloco.split('\n', 1)[0].strip()
        if not cab.startswith('## ') or not same_agent(_agent_da_secao(cab[3:]), agent): continue
        try: d = datetime.strptime(cab[3:].split('·', 1)[0].strip(), '%d/%m/%Y').date()
        except ValueError: continue
        if not 0 <= (hoje - d).days < dias: continue
        corpo = '\n'.join(l for l in bloco.split('\n')[1:] if l.strip())
        if corpo: secs.append((d, f'{cab}\n{corpo}\n'))
    secs.sort(key=lambda x: x[0], reverse=True)
    out = ''
    for _, sec in secs:
        if len((out + sec).encode('utf-8')) > max_bytes: break
        out += sec
    return out


def regras():
    try: return open(REGRAS_MD, encoding='utf-8').read()
    except FileNotFoundError: return '(sem rules.md ainda)'


def _pistas(s, agent=None):
    """O que o estado consegue apontar sozinho como friccao do dia (generico: pendencias podadas, acoes manuais e
    decisoes criadas hoje, fontes quebradas se o agent guardou)."""
    hoje = _hoje(); pistas = []
    podadas = [p for p in s.get('pendencias', []) if p.get('status') == 'ignorar' and (p.get('atualizada') or p.get('criada') or p.get('desde') or '')[:10] == hoje]
    if podadas: pistas.append(f'{len(podadas)} pendencia(s) podada(s) hoje como nao-demanda: ' + '; '.join(f'{p.get("quem")} ({p.get("fonte")})' for p in podadas) + ' — regra para o filtro?')
    acoes = [a for a in (s.get('acoes') or []) if (a.get('desde') or '')[:10] == hoje]
    if acoes:
        dec = [a for a in acoes if a.get('balde') == 'decisao']
        pistas.append(f'{len(acoes)} acao(oes) manual(is) criada(s) hoje' + (f', {len(dec)} decisao(oes) sua(s)' if dec else '') + ' — alguma dava para o agent executar ou decidir sozinho por regra?')
    q = s.get('quebrados_hoje') or []
    if q: pistas.append(f'fonte(s) quebrada(s) hoje: {", ".join(q)} — o agent avisou cedo e com o comando de conserto?')
    try:
        from . import rules_dedupe as cd
        dup = cd.checar()
        if dup: pistas.append(f'{len(dup)} frase(s) repetida(s) entre as instrucoes dos agents (ou com o rules.md) — regra geral vai para rules.md, a copia sai; ver `python3 -m watch_core.rules_dedupe`')
    except Exception as e:
        pistas.append(f'rules_dedupe falhou ({e.__class__.__name__}) — conferir o script')
    props = propostas(agent)             # so as deste agent (24/09/2026: as de uma instancia apareciam no fechamento de outra)
    if props: pistas.append(f'{len(props)} proposta(s) de autonomia deste agent ainda sem resposta no lessons.md — reapresentar em uma linha cada')
    return pistas


def devida(s):
    """Dia util, >= LICOES_HORA BRT e a licao de hoje ainda nao gravada."""
    a = _agora()
    return a.weekday() < 5 and a.hour >= LICOES_HORA and _hoje() not in licoes(s)


def tick(s, agent, dry=False):
    """Fechamento: reimprime a licao do dia util anterior (conferir se foi aplicada) e cobra a de hoje. Uma vez por dia:
    depois de gravada (`--licoes set`) nao imprime mais. Devolve True se imprimiu algo."""
    agent = canonical_agent(agent)
    a = _agora()
    if a.weekday() >= 5 or a.hour < LICOES_HORA: return False
    hoje = _hoje()
    if hoje in licoes(s): return False
    ant = [(d, l) for d, l in licoes_ultimas(s, 2) if d < hoje]
    if ant:
        d, l = ant[-1]
        print(f'== LICOES (de {_br(d)[:5]} — conferir se foi aplicado; "a aplicar" ainda aberto vira divida)'); print(l['texto'].strip()); print()
    print(f'== LICOES ({agent}: fechamento — o que eu faria diferente amanha para depender menos do usuario; gravar com --licoes set "...")')
    for p in _pistas(s, agent): print('  · ' + p)
    for sec, txt in propostas(agent): print(f'  · proposta aberta ({sec}): {txt[:140]}')
    return True


def cli(args, s, save_state, agent):
    """--licoes | --licoes set "texto" | --licoes regras | --licoes propostas | --licoes tudo"""
    agent = canonical_agent(agent)
    if args: args = [{'rules': 'regras', 'proposals': 'propostas', 'all': 'tudo'}.get(args[0], args[0])] + [{'all': 'todas'}.get(x, x) for x in args[1:]]   # English; Portuguese kept
    if args and args[0] == 'set':
        texto = ' '.join(args[1:]).strip()
        if not texto: raise SystemExit('--licoes set "texto"')
        licoes_set(s, agent, texto); save_state(s); print(f'licoes de {_br(_hoje())} gravadas ({agent}) em {LICOES_MD}'); return
    if args and args[0] == 'regras': print(regras()); return
    if args and args[0] == 'propostas':
        ps = propostas(None if args[1:2] == ['todas'] else agent)   # `propostas todas` = de todos os agents
        if not ps: print('sem propostas abertas'); return
        for sec, txt in ps: print(f'- ({sec}) {txt}')
        return
    if args and args[0] == 'tudo':
        try: print(open(LICOES_MD, encoding='utf-8').read())
        except FileNotFoundError: print('sem lessons.md ainda')
        return
    ult = licoes_ultimas(s)
    if not ult: print(f'sem licoes gravadas ({agent}); historico de todos em {LICOES_MD}'); return
    for d, l in ult: print(f'== {_br(d)} · {agent}'); print(l['texto'].strip()); print()
