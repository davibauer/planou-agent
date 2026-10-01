#!/usr/bin/env python3
"""Source slack: new messages from other people in DMs, group DMs and channels, through ONE search.messages call.

Transport is the `work-inbox` skill's slack_pull.py (session token, no Slack app), with the profile fixed by config.
search.messages brings DMs, group DMs and every channel the user sees; `after:` works by day, the fine cut is local.
The search index lags: the window goes back 5 min before the cursor and what was already shown is dropped by ts
(state 'vistos').

Pending queue ('slack'): a DM, an @mention or a reply in a thread of mine whose last message is not mine. It resolves
when I send anything in that chat after it (thread replies included) or the chat's last message becomes mine. The item
keeps the message that triggered it (channel, ts, thread_ts) and its permalink, so the task origin opens that message.

Config ("fontes": [{"tipo": "slack", ...}]):
  perfil          work-inbox profile name (required)
  teams_chat_dir  where slack_pull.py lives (default: paths.WORK_INBOX, the work-inbox skill of this plugin)
  bots_ignorar    usernames of apps that DM previews/reminders and are never a demand
  max_por_chat    messages shown per chat (default 20)
  lembrar_horas   business hours until the 1st reminder (default 4)
"""
import os, re, sys
from datetime import datetime, timezone, timedelta

import core, paths, compartilhado, permalinks
from adapters import Fonte as Base

PERFIL = None
TEAMS_DIR = paths.WORK_INBOX
BOTS_IGNORAR = set()
MAX_POR_CHAT = 20


def _slack():
    """Importa slack_pull fixando o perfil (ele resolve perfil no import)."""
    if TEAMS_DIR not in sys.path: sys.path.insert(0, TEAMS_DIR)
    os.environ['TEAMS_CHAT_PROFILE'] = os.environ['WORK_INBOX_PROFILE'] = PERFIL
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try: import slack_pull as sp
    finally: sys.argv = argv
    return sp


def tick_slack(s, desde, dry):
    dt, hora, enfileira, resolve, pend_abertas = core.dt, core.hora, core.enfileira, core.resolve, core.pend_abertas
    sp = _slack()
    me = sp.tp.cfg().get('slack_user_id')
    st = s.setdefault('slack', {'cursor': None})
    agora = datetime.now(timezone.utc)
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:                      # primeira rodada: so planta o cursor
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'chats': []}
    cache = sp.cache(); users = cache['users']
    nomes = {c['id']: c for c in cache['conversas']}
    # uma busca so: search.messages traz DMs, grupos e canais que eu vejo; after: e por dia, o corte fino e local.
    # O indice da busca atrasa: recua 5 min alem do cursor e descarta pelo ts o que ja foi mostrado (st['vistos']).
    dia = (desde - timedelta(days=2)).strftime('%Y-%m-%d')
    corte = desde.timestamp() - 300
    vistos = set(st.get('vistos') or [])
    brutas, pagina = [], 1
    while True:
        d = sp.api('search.messages', query=f'after:{dia}', sort='timestamp', sort_dir='desc', count=100, page=pagina)['messages']
        lote = d.get('matches', [])
        brutas += [m for m in lote if float(m.get('ts', 0)) > corte and m.get('ts') not in vistos]
        if not lote or float(lote[-1].get('ts', 0)) <= corte or pagina >= d.get('paging', {}).get('pages', 1): break
        pagina += 1
    por_chat = {}
    for m in brutas: por_chat.setdefault((m.get('channel') or {}).get('id', '?'), []).append(m)
    meus = {}                              # ts das minhas msgs por chat, inclusive respostas em thread
    for m in brutas:
        if m.get('user') == me: meus.setdefault((m.get('channel') or {}).get('id', '?'), []).append(float(m['ts']))
    pais = {}
    def autor_pai(cid, ts):
        if (cid, ts) not in pais:
            try: h = sp.historico(cid, 1, latest=ts); pais[(cid, ts)] = (h[0].get('user') if h else None)
            except SystemExit: pais[(cid, ts)] = None
        return pais[(cid, ts)]
    out = []
    for cid, ms in por_chat.items():
        ch = ms[0].get('channel') or {}
        ms.sort(key=lambda m: float(m['ts']))
        ms = [m for m in ms if m.get('user') != 'USLACKBOT' and not m.get('bot_id') and (m.get('username') or '') not in BOTS_IGNORAR]
        if not any(m.get('user') != me for m in ms): continue
        c = nomes.get(cid) or {}
        if ch.get('is_im'): nome, tipo = sp.nome_user(ch.get('user') or c.get('nome') or '?', users), 'dm'
        elif ch.get('is_mpim'): nome, tipo = c.get('nome') or 'grupo', 'grupo'
        else: nome, tipo = '#' + (ch.get('name') or c.get('nome', cid).lstrip('#')), 'canal'
        msgs = []
        for m in ms:
            th = re.search(r'thread_ts=(\d+\.\d+)', m.get('permalink') or '')
            texto = sp.texto_limpo(m.get('text') or '', users)
            msgs.append({'id': m['ts'], 'thread_ts': th.group(1) if th else None, 'quando': datetime.fromtimestamp(float(m['ts']), timezone.utc).isoformat(),
                         'de': sp.nome_user(m['user'], users) if m.get('user') else (m.get('username') or '?'), 'eu': m.get('user') == me,
                         'texto': texto, 'mencao': f'<@{me}>' in (m.get('text') or ''),
                         'thread_minha': bool(th) and autor_pai(cid, th.group(1)) == me, 'url': m.get('permalink')})
        ultimo_meu = msgs[-1]['eu']
        mencao = any(x['mencao'] and not x['eu'] for x in msgs)
        thread_minha = any(x['thread_minha'] and not x['eu'] for x in msgs)
        out.append({'chat': nome, 'id': cid, 'tipo': tipo, 'respondido': ultimo_meu, 'mencao': mencao, 'thread_minha': thread_minha,
                    'msgs': msgs[-MAX_POR_CHAT:], 'omitidas': max(0, len(msgs) - MAX_POR_CHAT)})
    if not dry: compartilhado.guarda_chats(out)   # links que pessoas mandaram -> <workspace>/messages/shared
    # ordem: DMs, mencoes e respostas nas suas threads sem resposta primeiro; canais ja respondidos por ultimo
    out.sort(key=lambda c: (c['respondido'], not (c['mencao'] or c['thread_minha'] or c['tipo'] == 'dm'), c['msgs'][-1]['quando']))
    resolvidas = []
    if not dry:
        # resolve: mandei qualquer msg no chat depois da pendencia (inclusive em thread — a busca traz as respostas
        # de thread, o historico do canal nao), ou a ultima msg do chat passou a ser minha (uma chamada por pendencia)
        for p in pend_abertas(s, 'slack'):
            desde_p = (dt(p['desde']) or agora).timestamp()
            if any(t > desde_p for t in meus.get(p['ref'], [])):
                resolve(p, agora, 'voce respondeu'); resolvidas.append((p, 'voce respondeu')); continue
            try: h = sp.historico(p['ref'], 1)
            except SystemExit: continue
            if h and h[0].get('user') == me:
                resolve(p, agora, 'voce respondeu'); resolvidas.append((p, 'voce respondeu'))
        # enfileira: DM, @mencao ou resposta em thread sua, ultima msg nao e minha
        for c in out:
            if c['respondido'] or not (c['tipo'] == 'dm' or c['mencao'] or c['thread_minha']): continue
            outros = [m for m in c['msgs'] if not m['eu']]
            gatilho = [m for m in outros if m['mencao'] or m['thread_minha']] if c['tipo'] != 'dm' else outros
            ult = (gatilho or outros)[-1]
            p = enfileira(s, 'slack', c['id'], c['chat'], ult['texto'][:80].replace('\n', ' '), ult['quando'], agora)
            p['autor'] = ult['de']
            # the message itself, as chat.getPermalink gives it (thread replies carry thread_ts and cid; the search result's
            # permalink leaves cid out), from the workspace URL of the profile (auth.test); the search permalink otherwise
            url = permalinks.slack(sp.tp.cfg().get('slack_url'), c['id'], ult['id'], ult.get('thread_ts')) or ult.get('url')
            permalinks.guarda(p, url, ult['quando'], channel=c['id'], ts=ult['id'], thread_ts=ult.get('thread_ts'))
        st['cursor'] = agora.isoformat()
        st['vistos'] = (list(vistos) + [m['ts'] for m in brutas])[-2000:]
    return {'baseline': False, 'chats': out, 'resolvidas': resolvidas}


def imprime_slack(r):
    hora = core.hora
    if r['baseline']:
        print('== SLACK: cursor plantado agora (primeira rodada); a proxima mostra o que chegar depois.'); return True
    if not r['chats']: return False
    print(f'== SLACK ({sum(len(c["msgs"]) for c in r["chats"])} msgs novas em {len(r["chats"])} conversas)')
    for c in r['chats']:
        estado = 'ultima msg e sua' if c['respondido'] else 'SEM RESPOSTA SUA'
        extra = (' · MENCIONA VOCE' if c['mencao'] else '') + (' · RESPONDE SUA THREAD' if c['thread_minha'] else '')
        print(f'-- {c["chat"]} ({c["tipo"]}) · {estado}{extra}')
        if c['omitidas']: print(f'   (… {c["omitidas"]} anteriores omitidas)')
        for m in c['msgs']:
            quem = 'eu' if m['eu'] else m['de']
            tag = ' [thread]' if m['thread_minha'] else ''
            print(f'   [{hora(m["quando"])}] {quem}{tag}: {m["texto"]}')
            for g in m.get('guardados') or []: print(f'      [guardado: {g}]')
        print(f'   {c["msgs"][-1]["url"]}')
    return True


class Fonte(Base):
    tipo = 'slack'
    lembrar = {'slack': 4.0}
    conserto = 'Slack down — refresh the session token (slack_pull.py --token --cookie, work-inbox skill)'

    def configura(self):
        global PERFIL, TEAMS_DIR, BOTS_IGNORAR, MAX_POR_CHAT
        PERFIL = self.cfg.get('perfil') or sys.exit('slack: "perfil" faltando no config.json')
        TEAMS_DIR = paths.expande(self.cfg['teams_chat_dir']) if self.cfg.get('teams_chat_dir') else paths.WORK_INBOX
        BOTS_IGNORAR = set(self.cfg.get('bots_ignorar') or [])
        MAX_POR_CHAT = int(self.cfg.get('max_por_chat', 20))

    def tick(self, s, desde, dry): return tick_slack(s, desde, dry)
    def imprime(self, r): return imprime_slack(r)
    def api(self): return _slack()
