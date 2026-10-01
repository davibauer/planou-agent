#!/usr/bin/env python3
"""Source google_chat (result key 'chat'): new Google Chat messages from other people (DMs, groups, spaces) of one
Workspace account through the Chat API (read-only scopes: never sends, reacts or marks as read). spaces.list brings
lastActiveTime of every space in one call; only spaces that changed since the cursor are read. Rooms listed in
"salas_so_mencao" only matter when they mention you or answer your thread; otherwise they become one count line.

Pending queue ('chat'): DM, @mention or reply in your thread whose last message is not yours; resolves when the last
message of the space in the window is yours. There is no "read" in the Chat API. The item keeps the message resource name
and a link to it.

Config ("fontes": [{"tipo": "google_chat", ...}]):
  email             the account e-mail (required: resolves your users/<id>)
  google_dir        folder with client_secret.json and token.json (default secrets/google)
  salas_so_mencao   room names that only count on mention / reply in your thread
  sobreposicao_min  default 2
  max_por_chat      default 20
  lembrar_horas     default 4
"""
import os, re, sys, json
from datetime import datetime, timezone, timedelta

import core, paths, compartilhado, permalinks
from adapters import Fonte as Base, google_auth
from core import enfileira, resolve, pend_abertas

EU_EMAIL = None
SALAS_SO_MENCAO = set()
SOBREPOSICAO = timedelta(minutes=2)
MAX_POR_CHAT = 20


def dt(iso): return core.dt(iso)


def hora(iso): return core.hora(iso)


TIPO = {'DIRECT_MESSAGE': 'dm', 'GROUP_CHAT': 'grupo', 'SPACE': 'sala'}


def svc():
    from googleapiclient.discovery import build
    return build('chat', 'v1', credentials=google_auth.creds(), cache_discovery=False)


def espacos(chat):
    """Todos os espacos do usuario (uma pagina de 1000 cobre centenas). Sem bots (singleUserBotDm)."""
    out, tok = [], None
    while True:
        r = chat.spaces().list(pageSize=1000, pageToken=tok).execute()
        out += r.get('spaces', [])
        tok = r.get('nextPageToken')
        if not tok: break
    return [s for s in out if not s.get('singleUserBotDm')]


def meu_id(chat, space):
    """users/<id> do usuario, pela alias de e-mail em members.get (qualquer espaco em que ele esteja)."""
    m = chat.spaces().members().get(name=f'{space}/members/{EU_EMAIL}').execute()
    return m['member']['name']


def nome_dm(chat, space, me):
    """Nome do outro lado de uma DM (ou dos outros de um grupo sem nome)."""
    r = chat.spaces().members().list(parent=space, pageSize=50).execute()
    outros = [x['member'].get('displayName') or x['member'].get('name') for x in r.get('memberships', [])
              if x['member'].get('name') != me]
    return ', '.join(outros) or '(dm)'


def link(space):
    sid = space.split('/')[-1]
    return f'https://mail.google.com/chat/u/0/#chat/space/{sid}'


def norm(x, me):
    ann = x.get('annotations') or []
    mencao = any(a.get('type') == 'USER_MENTION' and ((a.get('userMention') or {}).get('user') or {}).get('name') == me for a in ann)
    todos = any(a.get('type') == 'USER_MENTION' and ((a.get('userMention') or {}).get('user') or {}).get('name') == 'users/all' for a in ann)
    s = x.get('sender') or {}
    anexos = [a.get('contentName') or a.get('contentType') or 'anexo' for a in x.get('attachment') or []]
    texto = (x.get('text') or '').strip()
    if not texto and x.get('cardsV2'): texto = '(card)'
    return {'id': x['name'], 'quando': x.get('createTime'), 'de': s.get('displayName') or s.get('name'), 'de_id': s.get('name'),
            'bot': s.get('type') == 'BOT', 'eu': s.get('name') == me, 'texto': texto, 'anexos': anexos,
            'thread': (x.get('thread') or {}).get('name'), 'reply': bool(x.get('threadReply')), 'mencao': mencao, 'todos': todos}


def mensagens(chat, space, desde, me, max_msgs=200):
    """Mensagens do espaco com createTime > desde, ordem crescente (inclui as suas, para contexto)."""
    out, tok = [], None
    f = f'createTime > "{desde.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")}Z"' if desde else None
    while True:
        kw = {'parent': space, 'pageSize': 100, 'orderBy': 'createTime desc', 'pageToken': tok}
        if f: kw['filter'] = f
        r = chat.spaces().messages().list(**kw).execute()
        out += r.get('messages', [])
        tok = r.get('nextPageToken')
        if not tok or len(out) >= max_msgs: break
    msgs = [norm(x, me) for x in out]
    msgs.sort(key=lambda m: m['quando'] or '')
    return msgs


def raiz_thread(chat, thread):
    """Remetente (users/<id>) da mensagem que abriu a thread: o id da raiz e' <tid>.<tid>."""
    space, tid = thread.split('/threads/')
    x = chat.spaces().messages().get(name=f'{space}/messages/{tid}.{tid}').execute()
    return (x.get('sender') or {}).get('name')


def thread_inteira(chat, thread, me):
    space = thread.split('/threads/')[0]
    out, tok = [], None
    while True:
        r = chat.spaces().messages().list(parent=space, pageSize=100, pageToken=tok, filter=f'thread.name = "{thread}"').execute()
        out += r.get('messages', []); tok = r.get('nextPageToken')
        if not tok: break
    msgs = [norm(x, me) for x in out]; msgs.sort(key=lambda m: m['quando'] or '')
    return msgs


def imprime_msgs(msgs):
    for m in msgs:
        an = f'  [anexos: {", ".join(m["anexos"])}]' if m['anexos'] else ''
        quem = 'eu' if m['eu'] else m['de']
        tag = ' [thread]' if m['reply'] else ''
        tag += ' [@voce]' if m['mencao'] else ''
        print(f'   [{hora(m["quando"])}] {quem}:{tag} {m["texto"]}{an}')


def tick_chat(s, desde, dry):
    chat = svc()
    st = s.setdefault('chat', {'cursor': None, 'me': None, 'nomes': {}, 'vistos': []})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None               # --since: janela fixa, sem dedupe pelos vistos
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:                      # primeira rodada: so planta o cursor
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'chats': []}
    # cursor com folga (msg visivel um instante depois do carimbo, lastActiveTime atrasado) e dedupe por id de mensagem
    vistos = set() if fixo else set(st.get('vistos') or [])
    desde = desde - SOBREPOSICAO
    # spaces.list traz lastActiveTime de todos os espacos numa chamada: so le por inteiro os que mudaram desde o cursor
    todos = espacos(chat)
    mudaram = [x for x in todos if dt(x.get('lastActiveTime')) and dt(x['lastActiveTime']) > desde]
    if mudaram and not st.get('me'):
        st['me'] = meu_id(chat, mudaram[0]['name'])
    me = st.get('me')
    nomes = st.setdefault('nomes', {})
    raizes = {}                                        # thread -> remetente da raiz (uma chamada por thread, por tick)
    ultima_minha = {}                                  # espaco -> a ultima msg da janela e' sua (antes do dedupe: resolve pendencia)
    novos_ids = []
    out = []
    for x in mudaram:
        sid = x['name']
        msgs = mensagens(chat, sid, desde, me)
        msgs = [m for m in msgs if (m['texto'] or m['anexos']) and m['quando']]
        if msgs: ultima_minha[sid] = msgs[-1]['eu']
        msgs = [m for m in msgs if m['id'] not in vistos]
        novos_ids += [m['id'] for m in msgs]
        if not any(not m['eu'] and not m['bot'] for m in msgs): continue
        nome = x.get('displayName') or nomes.get(sid)
        if not nome:
            nome = ', '.join(sorted({m['de'] for m in msgs if not m['eu']})) if x.get('spaceType') == 'DIRECT_MESSAGE' else None
            if not nome or x.get('spaceType') != 'DIRECT_MESSAGE':
                try: nome = nome_dm(chat, sid, me)
                except Exception: nome = nome or sid
            nomes[sid] = nome
        tipo = TIPO.get(x.get('spaceType'), 'sala')
        mencao = any(m['mencao'] and not m['eu'] for m in msgs)
        thread_minha = False
        for m in msgs:
            if m['eu'] or not m['reply'] or not m['thread']: continue
            if m['thread'] not in raizes:
                try: raizes[m['thread']] = raiz_thread(chat, m['thread'])
                except Exception: raizes[m['thread']] = None
            if raizes[m['thread']] == me: thread_minha = True; m['thread_minha'] = True
        respondido = msgs[-1]['eu']
        resumo = tipo == 'sala' and nome in SALAS_SO_MENCAO and not (mencao or thread_minha)
        out.append({'chat': nome, 'id': sid, 'tipo': tipo, 'respondido': respondido, 'mencao': mencao, 'thread_minha': thread_minha,
                    'resumo': resumo, 'total': len(msgs), 'link': link(sid),
                    'msgs': [{'id': m['id'], 'quando': m['quando'], 'de': m['de'], 'eu': m['eu'], 'bot': m['bot'], 'texto': m['texto'], 'anexos': m['anexos'],
                              'reply': m['reply'], 'thread': m['thread'], 'mencao': m['mencao'], 'thread_minha': m.get('thread_minha', False)}
                             for m in msgs[-MAX_POR_CHAT:]],
                    'omitidas': max(0, len(msgs) - MAX_POR_CHAT)})
    if not dry: compartilhado.guarda_chats(out)   # links que pessoas mandaram -> <workspace>/messages/shared
    # ordem: DM/mencao/thread sua sem resposta primeiro, salas resumidas por ultimo
    out.sort(key=lambda c: (c['resumo'], c['respondido'], not (c['mencao'] or c['thread_minha'] or c['tipo'] == 'dm'), c['msgs'][-1]['quando']))
    resolvidas = []
    if not dry:
        # resolve: o espaco mudou neste tick e a ultima msg da janela e' sua (espaco que nao mudou continua como estava)
        for p in pend_abertas(s, 'chat'):
            if ultima_minha.get(p['ref']) is True:
                resolve(p, agora, 'voce respondeu'); resolvidas.append((p, 'voce respondeu'))
        # enfileira: DM, @mencao ou resposta em thread sua, ultima msg nao e' sua (o modelo poda o que nao for demanda com --pendente pN status=ignorar)
        for c in out:
            if c['respondido'] or not (c['tipo'] == 'dm' or c['mencao'] or c['thread_minha']): continue
            ult = [m for m in c['msgs'] if not m['eu']][-1]
            p = enfileira(s, 'chat', c['id'], c['chat'], (ult['texto'] or ', '.join(ult['anexos']))[:80], ult['quando'], agora)
            p['autor'] = ult['de']
            permalinks.guarda(p, permalinks.google_chat(ult['id'], c['tipo'], EU_EMAIL), ult['quando'], name=ult['id'])
        st['vistos'] = (list(vistos) + novos_ids)[-1000:]
        st['cursor'] = agora.isoformat()
    return {'baseline': False, 'chats': out, 'resolvidas': resolvidas}


def imprime_chat(r):
    if r['baseline']:
        print('== CHAT: cursor plantado agora (primeira rodada); a proxima mostra o que chegar depois.'); return True
    if not r['chats']: return False
    print(f'== CHAT ({sum(c["total"] for c in r["chats"])} msgs novas em {len(r["chats"])} conversas)')
    for c in r['chats']:
        if c['resumo']:
            print(f'   +{c["total"]} msgs sem mencao em {c["chat"]} (sala)'); continue
        estado = 'ultima msg e sua' if c['respondido'] else 'SEM RESPOSTA SUA'
        extra = (' · MENCIONA VOCE' if c['mencao'] else '') + (' · RESPONDE SUA THREAD' if c['thread_minha'] else '')
        print(f'-- {c["chat"]} ({c["tipo"]}) · {estado}{extra}')
        if c['omitidas']: print(f'   (… {c["omitidas"]} anteriores omitidas)')
        for m in c['msgs']:
            an = f'  [anexos: {", ".join(m["anexos"])}]' if m['anexos'] else ''
            quem = 'eu' if m['eu'] else (m['de'] + (' (bot)' if m['bot'] else ''))
            tag = (' [thread sua]' if m['thread_minha'] else ' [thread]' if m['reply'] else '') + (' [@voce]' if m['mencao'] else '')
            print(f'   [{hora(m["quando"])}] {quem}:{tag} {m["texto"]}{an}')
            for g in m.get('guardados') or []: print(f'      [guardado: {g}]')
        print(f'   {c["link"]}')
    return True


class Fonte(Base):
    tipo = 'google_chat'
    nome_padrao = 'chat'
    lembrar = {'chat': 4.0}
    conserto = 'Google Chat down — Google OAuth token (adapters/google_auth.py)'

    def configura(self):
        global EU_EMAIL, SALAS_SO_MENCAO, SOBREPOSICAO, MAX_POR_CHAT
        EU_EMAIL = self.cfg.get('email') or sys.exit('google_chat: "email" faltando no config.json')
        google_auth.configura(paths.google_dir(self.cfg.get('google_dir')))
        SALAS_SO_MENCAO = set(self.cfg.get('salas_so_mencao') or [])
        SOBREPOSICAO = timedelta(minutes=float(self.cfg.get('sobreposicao_min', 2)))
        MAX_POR_CHAT = int(self.cfg.get('max_por_chat', 20))

    def tick(self, s, desde, dry): return tick_chat(s, desde, dry)
    def imprime(self, r): return imprime_chat(r)
    def linha_ref(self, p): return f'   ref: {p["ref"]} · {link(p["ref"])}'
