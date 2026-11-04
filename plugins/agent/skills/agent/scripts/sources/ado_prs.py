#!/usr/bin/env python3
"""Source ado_prs (result key 'prs'): active Azure DevOps pull requests where I am reviewer or author. New PR to review,
movement on mine (votes, new reviewer, comments from others, merge conflict, left draft), PR that left the list (and how
it ended), my PRs with no vote for 5+ business days (reminded at most every 5 business days).

Pending queue ('pr'): new PR to review (not draft); my PR rejected / waiting for author / commented / in conflict. Resolves
when I vote (reviewer), when the last comment is mine and nobody is at -5/-10 (author), or when the PR leaves.

Config: see adapters/ado.py. "lembrar_horas" default 11, plus:
  prefixos_tarefa   task code prefixes that mirror a PR, <prefix><id> (default []: nothing is recorded and nothing closes)

Closing the Planou task (PLN0529): a row built only from active PRs stayed open on Planou after the PR left. With
prefixos_tarefa set, each PR that leaves is kept in s['prs']['fechados'] with how it ended and the heavy tick closes its
task (tarefas_fechadas): completed = done, abandoned = discarded, still active but I left the reviewers = no_action. A PR
that could not be asked ("nao consultada") records nothing. Tasks whose PR left earlier are recovered on the tick: the
codes Planou still has open that are neither watched nor known closed are asked by id, at most LOTE_FECHADAS per tick and
each one again only after RECONFERE, in the project of the task link, or in each configured project when the task has no
link. A task whose links name something else (another organization, GitHub, another PR) is never asked and never closes.
The first failure ends the tick's batch with one warning naming the PR: that PR waits RECONFERE and the next tick moves on
to the following ones. A 404 in every project or a PR that is still mine confirms nothing: the task stays open. When the
task links cannot be read (sent.json missing or broken) nothing is asked and nothing closes on that tick, with a warning.
"""
import re, urllib.error
from datetime import datetime, timedelta, timezone

import core, paths
from adapters import Fonte as Base, ado
from core import dt, horas_uteis, pend_abertas, resolve, enfileira


PARADA_PR_H = 55.0     # 5 dias uteis sem nenhum voto numa PR minha -> cobra (uma vez a cada 5 dias uteis)
PREFIXOS = []                    # task codes <prefix><id> that mirror a PR (config "prefixos_tarefa")
LOTE_FECHADAS = 20               # PRs asked by id per tick (each may take one call per configured project)
RECONFERE = timedelta(hours=6)   # a PR with nothing confirmed, or caught by a failure, is asked again only after this
GUARDA = timedelta(days=30)      # how long a closed PR is kept in s['prs']['fechados'] once Planou has it closed
OUTRO_LINK = False               # the task has links and none names this PR in this organization: it never closes

def desfecho(fim, agora):
    """How a PR read by id ended, for the close of its task: {fim, status, quando, titulo}, or None when nothing is
    confirmed. status is the task engine's label, which the Planou sync maps: Feito (done), Descartada (discarded),
    Sem ação (no_action). An active PR ended only when it is no longer mine to review or author."""
    status = fim.get('status')
    if status == 'completed': label = 'Feito'
    elif status == 'abandoned': label = 'Descartada'
    elif status == 'active' and not (fim.get('revisor') or fim.get('minha')): label = 'Sem ação'
    else: return None
    return {'fim': 'voce saiu dos revisores' if status == 'active' else status, 'status': label,
            'quando': fim.get('fechada') or agora.isoformat(), 'titulo': fim.get('titulo')}

def regex_codigo():
    """The compiled <prefix><id> task code, or None without prefixes. fullmatch only: pr<N> never reads prf<N>."""
    pre = '|'.join(re.escape(p) for p in PREFIXOS if p)
    return re.compile(rf'(?:{pre})(\d+)') if pre else None

def projeto_do_link(urls, pid):
    """The project of this PR in one of the task's origin links, as written in the URL; None when the task has no link;
    OUTRO_LINK when it has links and none of them is this PR in this organization."""
    if not urls: return None
    for u in urls:
        m = re.match(re.escape(ado.ORG) + r'/([^/?#]+)/_git/[^/?#]+/pullrequest/(\d+)(?:[/?#]|$)', u)
        if m and m.group(2) == pid: return m.group(1)
    return OUTRO_LINK

def ids_abertos_no_planou():
    """{PR id: projeto_do_link} of the task codes Planou still has open for this instance. With two codes for one PR
    (pr<N>, rev<N>) the task that can be asked decides: tarefas_fechadas still checks each code's own link. None when
    there are such codes and their links cannot be read."""
    from watch_core import planou
    rx = regex_codigo()
    if not rx or not paths.ROOT: return {}
    codigos = [(cod, m) for cod in planou.open_codes(paths.ROOT) for m in [rx.fullmatch(cod)] if m]
    if not codigos: return {}
    urls = planou.read_origin_urls(paths.ROOT)
    if urls is None: return None
    out = {}
    for cod, m in codigos:
        v = projeto_do_link(urls.get(cod), m.group(1))
        if v or out.get(m.group(1), OUTRO_LINK) is OUTRO_LINK: out[m.group(1)] = v
    return out

def pr_por_id(pid, projeto, eu):
    """A PR by id, in the project of its link, or in each configured one when projeto is None (a task without link);
    None on a 404 in all. Failures raise."""
    for p in [projeto] if projeto else ado.PROJETOS:
        try: return ado.por_id(p, pid, eu)
        except urllib.error.HTTPError as e:
            if e.code != 404: raise
    return None

def recupera(st, abertos, atual, eu, agora, avisos):
    """Tasks Planou still has open whose PR is neither watched nor known closed: it left before this was recorded, or it
    could not be asked when it left. A task whose links name something else is left out. LOTE_FECHADAS at most, each asked
    again only after RECONFERE. The first failure ends this tick's batch with one warning, never a broken source, and
    closes nothing: only that PR waits RECONFERE, so the next tick moves on to the following ones."""
    fechados, conferidos = st.setdefault('fechados', {}), st.setdefault('conferidos', {})
    corte = agora - RECONFERE
    lote = sorted((k for k, v in abertos.items() if v is not OUTRO_LINK and k not in atual and k not in fechados
                   and not (conferidos.get(k) and dt(conferidos[k]) > corte)), key=int)[:LOTE_FECHADAS]
    for k in lote:
        try:
            fim = pr_por_id(k, abertos[k], eu)
        except (Exception, SystemExit) as e:
            avisos.append(f'AVISO (prs): nao deu para conferir a PR {k}, o lote segue no proximo tick: {str(e)[:160]}')
            conferidos[k] = agora.isoformat()
            return
        d = desfecho(fim, agora) if fim else None
        if d: fechados[k] = {**d, 'visto': agora.isoformat()}
        else: conferidos[k] = agora.isoformat()

def poda(st, abertos, agora):
    """Keeps s['prs']['fechados'] bounded: a PR Planou no longer has open, seen more than GUARDA ago, goes."""
    corte = agora - GUARDA
    st['fechados'] = {k: v for k, v in (st.get('fechados') or {}).items()
                      if k in abertos or not v.get('visto') or dt(v['visto']) > corte}
    st['conferidos'] = {k: v for k, v in (st.get('conferidos') or {}).items()
                        if k in abertos and dt(v) > agora - RECONFERE}

def tick_prs(s, dry):
    st = s.setdefault('prs', {'itens': {}, 'cursor': None, 'eu': None})
    agora = datetime.now(timezone.utc)
    eu = st.get('eu') or ado.me()
    atual = ado.ativas(eu)
    antes = st['itens']
    out = {'baseline': not st.get('cursor'), 'revisar': [], 'movimento': [], 'sumidas': [], 'paradas': [], 'resolvidas': [], 'avisos': []}
    cursor = dt(st['cursor']) if st.get('cursor') else None
    for k, pr in atual.items():
        old = antes.get(k)
        if old is None:
            if pr['revisor'] and not pr['minha']: out['revisar'].append(pr)
            elif out['baseline'] and pr['minha']: out['movimento'].append({'pr': pr, 'eventos': ['ja aberta na baseline']})
            continue
        ev = []
        for nome, v in pr['votos'].items():
            if old['votos'].get(nome, 0) != v and v != 0: ev.append(f"{nome.split()[0]} {ado.VOTO.get(v, v)}")
        novos_nomes = set(pr['votos']) - set(old['votos'])
        if novos_nomes and pr['minha']: ev.append('revisor adicionado: ' + ', '.join(n.split()[0] for n in sorted(novos_nomes)))
        vistos = {c['id'] for c in old.get('comentarios', [])}
        for c in pr['comentarios']:
            if c['id'] not in vistos and not c['eu']: ev.append(f"{c['de'].split()[0]} comentou: \"{c['texto'][:120]}\"")
        if old.get('merge') != pr['merge'] and pr['merge'] == 'conflicts': ev.append('CONFLITO de merge')
        if old.get('draft') and not pr['draft']: ev.append('saiu de draft')
        if ev: out['movimento'].append({'pr': pr, 'eventos': ev})
    # sumiu da lista de ativas: completed/abandoned, ou fui tirado de revisor — busca por id para dizer como terminou
    for k, old in antes.items():
        if k in atual: continue
        try:
            fim = ado.por_id(old['projeto'], k, eu); como = fim['status'] if fim['status'] != 'active' else 'voce saiu dos revisores'
            fecho = desfecho(fim, agora)
        except Exception as e:
            fim, como, fecho = old, f'nao consultada ({type(e).__name__})', None
        out['sumidas'].append({'pr': {**old, 'status': fim['status']}, 'como': como, 'desfecho': fecho})
    # PR minha parada: sem nenhum voto (ou sem revisor) ha mais de 5 dias uteis; cobra no maximo a cada 5 dias uteis
    cobradas = st.setdefault('cobradas', {})
    for k, pr in atual.items():
        if not pr['minha'] or pr['draft'] or any(v != 0 for v in pr['votos'].values()): continue
        base = dt(cobradas.get(k) or pr['criada'])
        if horas_uteis(base, agora) >= PARADA_PR_H:
            out['paradas'].append(pr)
            if not dry: cobradas[k] = agora.isoformat()
    if not dry:
        if PREFIXOS:          # how each PR that left ended, for the close of its Planou task (Fonte.tarefas_fechadas, PLN0529)
            fechados = st.setdefault('fechados', {})
            for k in atual: fechados.pop(k, None)             # watched and active again: reactivated or back to me
            for x in out['sumidas']:
                d = x['desfecho']
                if d: fechados[str(x['pr']['id'])] = {**d, 'titulo': d.get('titulo') or x['pr'].get('titulo'), 'visto': agora.isoformat()}
            abertos = ids_abertos_no_planou()
            if abertos is None:
                out['avisos'].append('AVISO (prs): nao deu para ler os links das tarefas (sent.json): nada fecha nem e consultado neste tick')
            else:
                recupera(st, abertos, atual, eu, agora, out['avisos'])
                poda(st, abertos, agora)
        # resolve: revisor -> votei ou a PR saiu; minha -> ultimo comentario e meu e ninguem esta em -5/-10, ou a PR saiu
        for p in pend_abertas(s, 'pr'):
            pr = atual.get(p['ref'])
            if pr is None: resolve(p, agora, 'PR encerrada'); out['resolvidas'].append((p, 'PR encerrada')); continue
            if pr['minha']:
                ult = pr['comentarios'][-1] if pr['comentarios'] else None
                if (ult is None or ult['eu']) and not any(v < 0 for v in pr['votos'].values()):
                    resolve(p, agora, 'voce respondeu'); out['resolvidas'].append((p, 'voce respondeu'))
            elif pr['meu_voto'] != 0:
                resolve(p, agora, 'voce votou'); out['resolvidas'].append((p, 'voce votou'))
        # enfileira: PR nova para revisar (nao draft); minha com reprovacao/aguardando autor/comentario de outro
        for pr in out['revisar']:
            if not pr['draft']: enfileira(s, 'pr', str(pr['id']), pr['autor'], f"revisar PR {pr['id']} {pr['repo']}: {pr['titulo'][:60]}", pr['criada'], agora)
        for m in out['movimento']:
            pr = m['pr']
            if pr['minha'] and any(('reprovou' in e) or ('aguardando autor' in e) or ('comentou' in e) or ('CONFLITO' in e) for e in m['eventos']):
                enfileira(s, 'pr', str(pr['id']), pr['autor'], f"sua PR {pr['id']} {pr['repo']}: {m['eventos'][-1][:60]}", agora.isoformat(), agora)
        st['itens'] = atual; st['cursor'] = agora.isoformat(); st['eu'] = eu
    return out

def imprime_prs(r):
    if r['baseline']:
        print(f'== PRs: baseline plantada agora ({len(r["movimento"])} suas ativas, {len(r["revisar"])} para revisar); a proxima mostra so o que mudar.')
        for m in r['movimento']: print('  ' + ado.linha_pr(m['pr']))
    for a in r.get('avisos') or []: print(a)
    algo = bool(r['baseline'] or r.get('avisos'))        # a quiet tick still shows the warning
    if not (r['revisar'] or r['movimento'] or r['sumidas'] or r['paradas']): return algo
    if not r['baseline']: print('== PRs')
    if r['revisar']:
        print(f'PARA REVISAR ({len(r["revisar"])}):')
        for pr in r['revisar']: print('+ ' + ado.linha_pr(pr))
    if r['movimento'] and not r['baseline']:
        print(f'MOVIMENTO NAS SUAS ({len(r["movimento"])}):')
        for m in r['movimento']: print('~ ' + ado.linha_pr(m['pr']) + '\n   ' + ' | '.join(m['eventos']))
    if r['sumidas']:
        print(f'SAIRAM DA LISTA ({len(r["sumidas"])}):')
        for x in r['sumidas']: print(f"- PR {x['pr']['id']} [{x['pr']['repo']}] {x['pr']['titulo'][:60]} · {x['como']}")
    if r['paradas']:
        print(f'PARADAS ({len(r["paradas"])}) — suas, sem nenhum voto ha 5+ dias uteis:')
        for pr in r['paradas']:
            print(f"! PR {pr['id']} [{pr['repo']}] {pr['origem']} -> {pr['destino']} · desde {pr['criada'][:10]} · {'sem revisor' if not pr['votos'] else 'revisores sem votar'} · {pr['url']}")
    return True


class Fonte(Base):
    tipo = 'ado_prs'
    nome_padrao = 'prs'
    lembrar = {'pr': 11.0}
    conserto = 'Azure DevOps down — PAT in the git credential store'
    pend = ('pr',)

    def pronta(self, p, s):
        """A PR asking for my review, or my PR with a new review event: already happening at the source."""
        if str(p.get('assunto') or '').startswith('revisar PR'):
            return f'PR {p.get("ref")} aberta pedindo a sua revisão', 'PR aberta pedindo a sua revisão'
        return f'PR {p.get("ref")} sua com retorno de revisão', 'PR sua com retorno de revisão'

    def configura(self):
        global PREFIXOS
        ado.configura(self.cfg)
        PREFIXOS = [str(p) for p in (self.cfg.get('prefixos_tarefa') or []) if p]

    def tarefas_fechadas(self, s, codigos, agora):
        """The Planou close of each <prefix><id> whose PR left closed or left my reviews (s['prs']['fechados'], filled by
        the tick). Reads only the state and the task links. A PR still in my active list is never closed here, nor a task
        whose links name something else, however its PR number reads. Links that cannot be read close nothing (the
        tick warns)."""
        rx = regex_codigo()
        st = s.get('prs') or {}
        abertas, fechados = st.get('itens') or {}, st.get('fechados') or {}
        if not rx or not fechados: return {}
        from watch_core import planou
        urls = planou.read_origin_urls(paths.ROOT)
        if urls is None: return {}
        out = {}
        for cod in codigos:
            m = rx.fullmatch(cod)
            d = fechados.get(m.group(1)) if m and m.group(1) not in abertas else None
            if not d or d.get('status') not in ('Feito', 'Descartada', 'Sem ação'): continue
            if projeto_do_link(urls.get(cod), m.group(1)) is OUTRO_LINK: continue
            out[cod] = {'status': d['status'], 'concluida': d.get('quando'), 'origem': 'ado', 'fim': d.get('fim'),
                        'titulo': f"PR {m.group(1)} {d.get('titulo') or ''}".strip()}
        return out

    def tick(self, s, desde, dry): return tick_prs(s, dry)
    def imprime(self, r): return imprime_prs(r)
    def linha_ref(self, p): return None
