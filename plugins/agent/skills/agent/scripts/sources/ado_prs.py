#!/usr/bin/env python3
"""Source ado_prs (result key 'prs'): active Azure DevOps pull requests where I am reviewer or author. New PR to review,
movement on mine (votes, new reviewer, comments from others, merge conflict, left draft), PR that left the list (and how
it ended), my PRs with no vote for 5+ business days (reminded at most every 5 business days).

Pending queue ('pr'): new PR to review (not draft); my PR rejected / waiting for author / commented / in conflict. Resolves
when I vote (reviewer), when the last comment is mine and nobody is at -5/-10 (author), or when the PR leaves.

Config: see adapters/ado.py. "lembrar_horas" default 11.
"""
from datetime import datetime, timezone

import core
from adapters import Fonte as Base, ado
from core import dt, horas_uteis, pend_abertas, resolve, enfileira


PARADA_PR_H = 55.0     # 5 dias uteis sem nenhum voto numa PR minha -> cobra (uma vez a cada 5 dias uteis)

def tick_prs(s, dry):
    st = s.setdefault('prs', {'itens': {}, 'cursor': None, 'eu': None})
    agora = datetime.now(timezone.utc)
    eu = st.get('eu') or ado.me()
    atual = ado.ativas(eu)
    antes = st['itens']
    out = {'baseline': not st.get('cursor'), 'revisar': [], 'movimento': [], 'sumidas': [], 'paradas': [], 'resolvidas': []}
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
        except Exception as e:
            fim, como = old, f'nao consultada ({type(e).__name__})'
        out['sumidas'].append({'pr': {**old, 'status': fim['status']}, 'como': como})
    # PR minha parada: sem nenhum voto (ou sem revisor) ha mais de 5 dias uteis; cobra no maximo a cada 5 dias uteis
    cobradas = st.setdefault('cobradas', {})
    for k, pr in atual.items():
        if not pr['minha'] or pr['draft'] or any(v != 0 for v in pr['votos'].values()): continue
        base = dt(cobradas.get(k) or pr['criada'])
        if horas_uteis(base, agora) >= PARADA_PR_H:
            out['paradas'].append(pr)
            if not dry: cobradas[k] = agora.isoformat()
    if not dry:
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
    algo = bool(r['baseline'])
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

    def configura(self): ado.configura(self.cfg)
    def tick(self, s, desde, dry): return tick_prs(s, dry)
    def imprime(self, r): return imprime_prs(r)
    def linha_ref(self, p): return None
