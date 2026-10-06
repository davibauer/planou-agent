#!/usr/bin/env python3
"""Source ado_workitems (result key 'ado'): Azure DevOps work items assigned to me in the configured projects (open
states only). Delta against the last run: new item (not eligible type or created before the baseline = notice only),
state change, item that left the list (closed, removed or reassigned, confirmed by Azure DevOps or missing from 3
reads in a row, sources/sumidos.py). No time window: --since only makes it dry.

Config: see adapters/ado.py (org, projetos, credencial, tipos, fechados, mensagens), plus:
  prefixos_tarefa   task code prefixes that mirror a work item, <prefix><id> (default ["wi"])
  descartados       closed states that close the Planou task as discarded, not done (default ["Removed"])
Extra CLI: --ado all | show ID.

Closing the Planou task (PLN0391): a row built only from open items (wi<id>) used to stay in progress on Planou after the
item closed. Each item that leaves is kept in s['ado']['fechados'] with how it ended, and the heavy tick closes its task
(tarefas_fechadas): Done/Closed/Resolved = done, Removed or deleted = discarded, reassigned to someone else (or nobody)
or moved out of the projects = no_action (not done by me, no longer mine). completed_at: for a closed state, ClosedDate
(else StateChangeDate, else ChangedDate); for no_action, ChangedDate (the reassignment or move, never the older state
change). Tasks stuck from before this (the item left earlier) are recovered on the tick: the wi<id> Planou still has
open that are neither in the list nor known closed are asked by id in one batch. In that recovery a null slot (deleted,
or no permission to read it) confirms nothing: the item is asked again after RECONFERE and its task is not closed.
"""
import re, sys, json, urllib.error
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Fonte as Base, ado, sumidos
from adapters.ado import atribuidos, delta, linha

LOTE = 200                       # ids per batch call (the API limit); also the most recovered in one tick
CAMPOS_SAIDA = ['System.Id', 'System.State', 'System.AssignedTo', 'System.TeamProject', 'System.Title', 'System.ChangedDate']
# when it closed: a process without them answers 400 to the whole call; then read without them for the rest of the run
CAMPOS_DATA = ['Microsoft.VSTS.Common.ClosedDate', 'Microsoft.VSTS.Common.StateChangeDate']
_SEM_DATA = []
PREFIXOS = ['wi']                # task codes <prefix><id> that mirror a work item (config "prefixos_tarefa")
DESCARTADOS = ['Removed']        # closed states that mean "dropped", not "done" (config "descartados")
GUARDA = timedelta(days=30)      # how long a closed item is kept in s['ado']['fechados'] once Planou has it closed
RECONFERE = timedelta(hours=6)   # an item that left with no confirmation is asked again only after this


def consulta(ids):
    """Work items by id, in batches of LOTE: ({id: fields}, {ids removed}). Removed = null in the position of an
    `errorPolicy=omit` answer of full length (a short or empty answer confirms nothing)."""
    achados, removidos = {}, set()
    for i in range(0, len(ids), LOTE):
        lote = ids[i:i+LOTE]
        def pede(campos):
            return ado.call(ado.ORG + f"/_apis/wit/workitems?ids={','.join(lote)}&fields={','.join(campos)}"
                                      f"&errorPolicy=omit&api-version=7.0").get('value') or []
        try:
            valor = pede(CAMPOS_SAIDA + ([] if _SEM_DATA else CAMPOS_DATA))
        except urllib.error.HTTPError as e:
            if e.code != 400 or _SEM_DATA: raise
            _SEM_DATA.append(True)
            valor = pede(CAMPOS_SAIDA)
        for w in valor:
            if w: achados[str(w['id'])] = w.get('fields') or {}
        if len(valor) == len(lote):
            removidos |= {k for k, w in zip(lote, valor) if w is None}
    return achados, removidos


def desfecho(f, eu, agora):
    """How an item that left my open ones ended, from its fields: {fim, status, quando, titulo} or None (still open,
    mine and in the projects: nothing confirmed). status is the task engine's label, which the Planou sync maps:
      closed state (ado.FECHADOS)              -> Feito (done), or Descartada (discarded) for DESCARTADOS (Removed)
      deleted (f None)                         -> Descartada (discarded)
      reassigned (someone else or nobody)      -> Sem ação (no_action): not done by me, and no longer mine to do
      moved out of the configured projects     -> Sem ação (no_action)
    quando: closed state = ClosedDate, else StateChangeDate, else ChangedDate; Sem ação = ChangedDate (the reassignment
    or move; StateChangeDate there is an older, unrelated state change); deleted or no date = now."""
    if f is None:
        return {'fim': 'removido', 'status': 'Descartada', 'quando': agora.isoformat(), 'titulo': None}
    quando = f.get('System.ChangedDate') or agora.isoformat()
    dono = f.get('System.AssignedTo') or {}
    est = f.get('System.State')
    if est in ado.FECHADOS:
        fim, status = est, ('Descartada' if est in DESCARTADOS else 'Feito')
        quando = f.get('Microsoft.VSTS.Common.ClosedDate') or f.get('Microsoft.VSTS.Common.StateChangeDate') or quando
    elif dono.get('id') != eu:
        fim, status = f"reatribuido ({dono.get('displayName') or 'ninguem'})", 'Sem ação'
    elif ado.PROJETOS and f.get('System.TeamProject') not in ado.PROJETOS:
        fim, status = f"movido para {f.get('System.TeamProject')}", 'Sem ação'
    else:
        return None
    return {'fim': fim, 'status': status, 'quando': quando, 'titulo': f.get('System.Title')}


def saiu_como(itens, guarda=None, agora=None):
    """Work items missing from my open ones that Azure DevOps confirms are out: closed state (ado.FECHADOS), assigned to
    someone else or nobody, moved out of the configured projects, or removed (omitted by the batch). One call for all.
    {id: how it left}; the rest stays in the list (sumidos.confirma). `guarda` receives {id: desfecho} of the confirmed."""
    agora = agora or datetime.now(timezone.utc)
    ids = [str(it['id']) for it in itens]
    eu = ado.me()
    achados, removidos = consulta(ids)
    out = {}
    for k in ids:
        if k not in achados and k not in removidos: continue
        d = desfecho(achados.get(k), eu, agora)
        if not d: continue
        out[k] = d['fim']
        if guarda is not None: guarda[k] = d
    return out


def ids_abertos_no_planou(prefixos=None):
    """Work item ids of the task codes (<prefix><id>) Planou still has open for this instance (its local state.json)."""
    from watch_core import planou
    pre = '|'.join(re.escape(p) for p in (prefixos or PREFIXOS) if p)
    if not pre or not paths.ROOT: return set()
    rx = re.compile(rf'^(?:{pre})(\d+)$')
    return {m.group(1) for c in planou.open_codes(paths.ROOT) if (m := rx.match(c))}


def recupera(st, atual, agora, avisos):
    """Items Planou still has open (wi<id>) that are neither in my open list nor known closed: the ones that left before
    this was recorded (PLN0391), or that left with no confirmation (sumidos.FORA_DA_LISTA). One batch (LOTE at most),
    each asked again only after RECONFERE, also after an error (a warning, never a broken source, and not one per tick).
    A null slot is not confirmed here (it also comes from an item I may not read): it goes to `conferidos`, never closed
    as discarded, so an item really deleted before this existed keeps its task open (closed by hand on Planou)."""
    fechados, conferidos = st.setdefault('fechados', {}), st.setdefault('conferidos', {})
    corte = agora - RECONFERE
    ids = sorted((k for k in ids_abertos_no_planou() if k not in atual and k not in fechados
                  and not (conferidos.get(k) and core.dt(conferidos[k]) > corte)), key=int)[:LOTE]
    if not ids: return
    try:
        eu = ado.me()
        achados, _ = consulta(ids)
    except (Exception, SystemExit) as e:
        avisos.append(f'AVISO (ado): nao deu para conferir {len(ids)} itens que sairam da lista: {str(e)[:160]}')
        for k in ids: conferidos[k] = agora.isoformat()      # also waits RECONFERE: no call nor warning per tick
        return
    for k in ids:
        d = desfecho(achados[k], eu, agora) if k in achados else None
        if d: fechados[k] = {**d, 'visto': agora.isoformat()}
        else: conferidos[k] = agora.isoformat()


def poda(st, agora):
    """Keeps s['ado']['fechados'] bounded: an item Planou no longer has open, seen more than GUARDA ago, goes."""
    abertos = ids_abertos_no_planou()
    corte = agora - GUARDA
    st['fechados'] = {k: v for k, v in (st.get('fechados') or {}).items()
                      if k in abertos or not v.get('visto') or core.dt(v['visto']) > corte}
    st['conferidos'] = {k: v for k, v in (st.get('conferidos') or {}).items()
                        if k in abertos and core.dt(v) > agora - RECONFERE}


def tick_ado(s, dry):
    st = s.setdefault('ado', {'itens': {}, 'ultima': None, 'primeira': None})
    agora_dt = datetime.now(timezone.utc)
    atual = atribuidos()
    novos, mudados, saidos = delta(atual, st['itens'])
    # an item only leaves when Azure DevOps confirms it (sumidos.py): an empty answer is not "all closed"
    desfechos = {}
    saidos = sumidos.confirma(saidos, atual, lambda it: str(it['id']), lambda its: saiu_como(its, desfechos, agora_dt))
    ultima = core.dt(st['ultima']) if st.get('ultima') else None
    novos, recompostos = sumidos.recompoe(st, st['itens'], novos, lambda it: core.dt(it.get('created')), ultima)
    for k, it in atual.items():
        for extra in ('pr', 'status', 'nota'):
            if k in st['itens'] and extra in st['itens'][k]: it[extra] = st['itens'][k][extra]
    avisos = []
    if not dry:
        agora = agora_dt.isoformat()
        st['itens'] = atual; st['ultima'] = agora; st['primeira'] = st.get('primeira') or agora; sumidos.marca(st)
        # how each item that left ended, for the close of its Planou task (Fonte.tarefas_fechadas, PLN0391)
        fechados = st.setdefault('fechados', {})
        for k in atual: fechados.pop(k, None)                 # back in my open list: reopened or given back to me
        for it in saidos:
            k = str(it['id'])
            if k in desfechos:
                fechados[k] = {**desfechos[k], 'titulo': desfechos[k].get('titulo') or it.get('titulo'), 'visto': agora}
        recupera(st, atual, agora_dt, avisos)
        poda(st, agora_dt)
    out = {'novos': [], 'mudados': [], 'sumidos': saidos, 'recompostos': recompostos, 'avisos': avisos}
    for it in novos:
        obs = '' if it['elegivel'] else 'tipo nao elegivel: so aviso'
        if it['elegivel'] and it['created'] < (st.get('primeira') or datetime.now(timezone.utc).isoformat()):
            obs = 'criado ANTES da baseline: backlog reatribuido, so aviso'
        out['novos'].append({**it, 'obs': obs})
    out['mudados'] = [{'antes': o, 'agora': n} for o, n in mudados]
    return out

def imprime_ado(r):
    for a in r.get('avisos') or []: print(a)
    if not (r['novos'] or r['mudados'] or r['sumidos']):
        if not r.get('recompostos'): return bool(r.get('avisos'))
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

    def configura(self):
        global PREFIXOS, DESCARTADOS
        ado.configura(self.cfg)
        PREFIXOS = [str(p) for p in (self.cfg.get('prefixos_tarefa') or PREFIXOS) if p]
        DESCARTADOS = list(self.cfg.get('descartados') or DESCARTADOS)

    def tarefas_fechadas(self, s, codigos, agora):
        """The Planou close of each wi<id> whose item left my open ones closed, reassigned, moved or deleted
        (s['ado']['fechados'], filled by the tick). An id still in my open list is never closed here."""
        st = s.get('ado') or {}
        abertos, fechados = st.get('itens') or {}, st.get('fechados') or {}
        pre = '|'.join(re.escape(p) for p in PREFIXOS if p)
        if not pre: return {}
        rx = re.compile(rf'^(?:{pre})(\d+)$')
        out = {}
        for cod in codigos:
            m = rx.match(cod)
            d = fechados.get(m.group(1)) if m and m.group(1) not in abertos else None
            if not d or d.get('status') not in ('Feito', 'Descartada', 'Sem ação'): continue
            out[cod] = {'status': d['status'], 'concluida': d.get('quando'), 'origem': 'ado',
                        'titulo': f"#{m.group(1)} {d.get('titulo') or ''}".strip(), 'fim': d.get('fim')}
        return out
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
