#!/usr/bin/env python3
"""Source ado_workitems (result key 'ado'): Azure DevOps work items assigned to me in the configured projects (open
states only). Delta against the last run: new item (not eligible type or created before the baseline = notice only),
state change, item that left the list (closed, removed or reassigned, confirmed by Azure DevOps or missing from 3
reads in a row, sources/sumidos.py). No time window: --since only makes it dry.

Config: see adapters/ado.py (org, projetos, credencial, tipos, fechados, mensagens). Extra CLI: --ado all | show ID.
"""
import re, sys, json
from datetime import datetime, timezone

import core
from adapters import Fonte as Base, ado, sumidos
from adapters.ado import atribuidos, delta, linha


def saiu_como(itens):
    """Work items missing from my open ones that Azure DevOps confirms are out: closed state (ado.FECHADOS), assigned to
    someone else or nobody, moved out of the configured projects, or removed (omitted by the batch). One call for all.
    {id: how it left}; the rest stays in the list (sumidos.confirma)."""
    ids = [str(it['id']) for it in itens]
    eu = ado.me()
    campos = 'System.Id,System.State,System.AssignedTo,System.TeamProject'
    achados, removidos = {}, set()
    for i in range(0, len(ids), 200):
        lote = ids[i:i+200]
        valor = ado.call(ado.ORG + f"/_apis/wit/workitems?ids={','.join(lote)}&fields={campos}&errorPolicy=omit&api-version=7.0").get('value') or []
        for w in valor:
            if w: achados[str(w['id'])] = w.get('fields') or {}
        if len(valor) == len(lote):              # omit answers null in the position of a removed item; a short or
            removidos |= {k for k, w in zip(lote, valor) if w is None}   # empty answer confirms nothing
    out = {}
    for k in ids:
        f = achados.get(k)
        if f is None:
            if k in removidos: out[k] = 'removido'
            continue
        dono = f.get('System.AssignedTo') or {}
        if f.get('System.State') in ado.FECHADOS: out[k] = f['System.State']
        elif dono.get('id') != eu: out[k] = f"reatribuido ({dono.get('displayName') or 'ninguem'})"
        elif ado.PROJETOS and f.get('System.TeamProject') not in ado.PROJETOS: out[k] = f"movido para {f.get('System.TeamProject')}"
    return out


def tick_ado(s, dry):
    st = s.setdefault('ado', {'itens': {}, 'ultima': None, 'primeira': None})
    atual = atribuidos()
    novos, mudados, saidos = delta(atual, st['itens'])
    # an item only leaves when Azure DevOps confirms it (sumidos.py): an empty answer is not "all closed"
    saidos = sumidos.confirma(saidos, atual, lambda it: str(it['id']), saiu_como)
    ultima = core.dt(st['ultima']) if st.get('ultima') else None
    novos, recompostos = sumidos.recompoe(st, st['itens'], novos, lambda it: core.dt(it.get('created')), ultima)
    for k, it in atual.items():
        for extra in ('pr', 'status', 'nota'):
            if k in st['itens'] and extra in st['itens'][k]: it[extra] = st['itens'][k][extra]
    if not dry:
        agora = datetime.now(timezone.utc).isoformat()
        st['itens'] = atual; st['ultima'] = agora; st['primeira'] = st.get('primeira') or agora; sumidos.marca(st)
    out = {'novos': [], 'mudados': [], 'sumidos': saidos, 'recompostos': recompostos}
    for it in novos:
        obs = '' if it['elegivel'] else 'tipo nao elegivel: so aviso'
        if it['elegivel'] and it['created'] < (st.get('primeira') or datetime.now(timezone.utc).isoformat()):
            obs = 'criado ANTES da baseline: backlog reatribuido, so aviso'
        out['novos'].append({**it, 'obs': obs})
    out['mudados'] = [{'antes': o, 'agora': n} for o, n in mudados]
    return out

def imprime_ado(r):
    if not (r['novos'] or r['mudados'] or r['sumidos']):
        if not r.get('recompostos'): return False
        print('== ADO')
        print(f"lista recomposta: {len(r['recompostos'])} itens de antes do ultimo tick voltaram (ja eram conhecidos, nada novo)")
        return True
    print('== ADO')
    if r['novos']:
        print(f'NOVOS ({len(r["novos"])}):')
        for it in r['novos']: print(linha(it, '+ ') + (f'   ({it["obs"]})' if it['obs'] else ''))
    if r['mudados']:
        print(f'MUDARAM ({len(r["mudados"])}):')
        for m in r['mudados']: print(linha(m['agora'], f'~ [{m["antes"]["estado"]} -> {m["agora"]["estado"]}] '))
    if r['sumidos']:
        print(f'SAIRAM DA LISTA ({len(r["sumidos"])}) (fechado, removido ou reatribuido):')
        for it in r['sumidos']: print(f'- #{it["id"]} [{it.get("fim", "?")}] {it.get("titulo", "(sem titulo no estado)")}')
    return True


class Fonte(Base):
    tipo = 'ado_workitems'
    nome_padrao = 'ado'
    lembrar = {}
    conserto = 'Azure DevOps down — PAT in the git credential store'

    def configura(self): ado.configura(self.cfg)
    def prazo(self, it, p, s):
        m = re.search(r'/_workitems/edit/(\d+)', (it or {}).get('link') or '')
        return (((s.get('ado') or {}).get('itens') or {}).get(m.group(1)) or {}).get('vence') if m else None
    def tick(self, s, desde, dry): return tick_ado(s, dry)
    def imprime(self, r): return imprime_ado(r)

    def args(self, ap):
        ap.add_argument('--ado', nargs='+', metavar='all|show ID', help='Azure DevOps (so leitura): all | show ID')

    def cli(self, a, ctx):
        if not a.ado: return False
        if a.ado[0] == 'show' and len(a.ado) > 1:
            print(json.dumps(ado.show(int(a.ado[1])), ensure_ascii=False, indent=1)); return True
        atual = atribuidos()
        print(f'{len(atual)} itens atribuidos a mim (abertos):')
        for it in sorted(atual.values(), key=lambda x: x['changed'], reverse=True): print(linha(it, '- '))
        return True
