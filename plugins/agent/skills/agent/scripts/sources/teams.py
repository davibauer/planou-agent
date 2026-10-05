#!/usr/bin/env python3
"""Source teams: new Microsoft Teams chat messages from other people (1:1, groups, meeting chats), through the
`work-inbox` plugin (Power Automate connector; optionally the Teams web chat service).

How it finds chats with news (one call per chat would be ~30 calls): either
  rota "conector"  the connector's listchats (3 routes) + a probe of the latest message of each chat (2 in parallel,
                   chats idle for 30+ days probed every 6 h), or
  rota "chatsvc"   one call to the Teams web chat service (session of the work-inbox `teams_web` module), which already
                   brings the last message of every conversation; falls back to the connector (with at most
                   "limite_parados" idle chats probed per tick) and warns once a day.
Only chats whose last message is newer than the cursor are read in full (50 messages).

Pending queue ('teams'): a 1:1 chat, an ad-hoc group (no name) or an @mention whose last message is not mine; it
resolves when the last message of the chat becomes mine. The item keeps the chat id and message id of the last message
from the other person and its deep link (Graph's webUrl when present, else the documented l/message link).

Config ("fontes": [{"tipo": "teams", ...}]):
  perfil            work-inbox profile (required; the same for every Microsoft source of the instance)
  rota              "conector" (default) | "chatsvc"
  limite_parados    idle chats probed per tick on the connector route (default: no limit)
  sobreposicao_min  re-read N minutes before the cursor and drop already-seen message ids (default 0 = off)
  imagens           download pasted images through teams_web and list their paths (default false)
  ref_regex         regex with two groups (path, number) of a link in a message that names the same demand as another
                    source (e.g. a merge request URL); the pending item gets p['mr'] = "path!number" (cruza_mr hook)
  max_por_chat      default 20
  lembrar_horas     default 4
"""
import os, re, sys, json, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

import core, compartilhado, permalinks
from watch_core import slowfs
from adapters import Fonte as Base, ms365

MAX_POR_CHAT = 20
CHATSVC = 'https://teams.microsoft.com/api/chatsvc/amer/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=100'
TIPOS_CHAT = {'OneToOneChat', 'Chat', 'Meeting'}
CFG = {}


def _baixa_imagens(m, max_por_msg=3):
    """Prints colados na mensagem: baixa pela sessao do Teams web (teams_web.py) e devolve os caminhos —
    o agente le a imagem com Read. Falha (sem credencial, token morto, 4xx) vira um aviso na lista, nunca derruba o tick."""
    ids = (m.get('imagens') or [])[:max_por_msg]
    if not ids: return []
    out = []
    try:
        import teams_web as tw
        for h in ids:
            try: out.append(tw.baixar_imagem(ms365.PERFIL, h))
            except Exception as e: out.append(f'(imagem nao baixada: {type(e).__name__}: {str(e)[:80]})')
    except Exception as e:
        out.append(f'(imagens: {type(e).__name__}: {str(e)[:80]})')
    return out


def _baixa_sharepoint(url):
    """Arquivo anexado no Teams mora no OneDrive/SharePoint de quem mandou (anexo tipo reference). Baixa pela API REST do
    SharePoint com o token da sessao do Teams (teams_web: o mesmo refresh FOCI serve para o host do SharePoint)."""
    import teams_web as tw, urllib.parse
    u = urllib.parse.urlsplit(url)
    host = f'{u.scheme}://{u.netloc}'
    caminho = urllib.parse.unquote(u.path)
    partes = caminho.split('/')
    site = host + '/'.join(partes[:3]) if len(partes) > 3 and partes[1] in ('personal', 'sites', 'teams') else host
    tok = tw.access_token(ms365.PERFIL, host + '/.default', client='broker')
    rel = urllib.parse.quote(caminho.replace("'", "''"))
    req = urllib.request.Request(f"{site}/_api/web/GetFileByServerRelativeUrl('{rel}')/$value",
                                 headers={'Authorization': 'Bearer ' + tok})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _guarda(m, onde):
    """Material que uma PESSOA mandou (arquivo anexado e links) vai para <workspace>/messages/shared (compartilhado.py).
    Devolve o que foi guardado agora, para o tick citar o caminho."""
    g = []
    for a in m.get('arquivos') or []:
        p = compartilhado.guarda_arquivo(m['quando'], m['de'], onde, a['nome'], lambda a=a: _baixa_sharepoint(a['url']),
                                         chave=a['url'])
        if p: g.append(p)
    return g + compartilhado.guarda_links(m['quando'], m['de'], onde, m['texto'])


def _sondas_chatsvc(me):
    """Uma chamada so': o servico de chat do proprio Teams devolve todas as conversas, da mais recente para a mais antiga,
    cada uma com a ultima mensagem (hora e remetente). Devolve [(chat, ultimo_iso, de, None)] no formato das sondas;
    so' chats (1:1, grupo, reuniao) — canais e fluxos 48:* ficam fora, como no conector."""
    import teams_web as tw
    req = urllib.request.Request(CHATSVC, headers={'Authorization': 'Bearer ' + tw.token_asm(ms365.PERFIL)})
    with urllib.request.urlopen(req, timeout=30) as r: d = json.load(r)
    out = []
    for c in d.get('conversations', []):
        tp_ = c.get('threadProperties') or {}; lm = c.get('lastMessage') or {}
        tipo = tp_.get('productThreadType')
        if tipo not in TIPOS_CHAT: continue
        topico = tp_.get('topic') or None
        chat = {'id': c['id'], 'topic': topico, '_ad_hoc': tipo == 'Chat' and not topico}
        ultimo = lm.get('composetime') or lm.get('originalarrivaltime')
        out.append((chat, ultimo, lm.get('imdisplayname') or None, None))
    if not out: raise RuntimeError('chatsvc sem conversas')
    return out


def tick_teams(s, desde, dry):
    dt, enfileira, resolve, pend_abertas = core.dt, core.enfileira, core.resolve, core.pend_abertas
    rota = CFG.get('rota', 'conector')
    sobre = timedelta(minutes=float(CFG.get('sobreposicao_min') or 0))
    dedup = bool(sobre)
    limite = CFG.get('limite_parados')
    imagens = bool(CFG.get('imagens'))
    ref_re = re.compile(CFG['ref_regex']) if CFG.get('ref_regex') else None
    tp, _ = ms365.teams()
    from teams_feed import normaliza
    me = tp.cfg().get('me', '')
    st = s.setdefault('teams', {'cursor': None})
    agora = datetime.now(timezone.utc)
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:                      # primeira rodada: so planta o cursor
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'chats': []}
    vistos = set()
    if dedup:
        fixo = st.get('cursor') is None or desde != dt(st['cursor'])
        desde = desde - sobre
        vistos = set() if fixo else set(st.get('vistos') or [])
    sond = st.setdefault('chats', {})

    def _sondas_conector():
        # listchats nao traz a hora da ultima mensagem (lastUpdatedDateTime e' metadado do chat, nao serve):
        # sonda a mensagem mais recente de cada chat em paralelo e so le por inteiro os que mudaram.
        import threading
        from watch_core import pool
        chats = tp.get('/flowbot/actions/listchats/chattypes/oneOnOne/topic/all/expandmembers/false').get('value', [])
        chats += tp.get('/flowbot/actions/listchats/chattypes/all/topic/isDefined/expandmembers/false').get('value', [])
        # grupo SEM nome (3+ pessoas, topic null) nao vem em nenhuma das duas rotas acima. Tratado como 1:1 nas pendencias.
        ja = {c['id'] for c in chats}                  # a rota isUndefined repete grupos com nome: so entra o que faltava
        for c in tp.get('/flowbot/actions/listchats/chattypes/group/topic/isUndefined/expandmembers/false').get('value', []):
            if c['id'] not in ja: c['_ad_hoc'] = True; chats.append(c)
        # cache por chat: chat parado ha mais de 30 dias so e' sondado a cada 6h
        def precisa(c):
            h = sond.get(c['id'])
            if not h or not h.get('ultimo'): return True
            if agora - dt(h['ultimo']) < timedelta(days=30): return True
            return not h.get('sondado') or agora - dt(h['sondado']) > timedelta(hours=6)
        unicos = [c for c in {c['id']: c for c in chats}.values() if precisa(c)]
        if limite is not None:
            # chats parados (>30 d) vencidos de sondagem: no maximo N por tick, os mais antigos primeiro (sem o teto o runtime
            # do Power Automate responde 429 depois de uma noite parada)
            def _parado(c): h = sond.get(c['id']) or {}; return bool(h.get('ultimo')) and agora - dt(h['ultimo']) >= timedelta(days=30)
            parados = sorted((c for c in unicos if _parado(c)), key=lambda c: (sond.get(c['id']) or {}).get('sondado') or '')
            unicos = [c for c in unicos if not _parado(c)] + parados[:int(limite)]
        parar = threading.Event()                   # set when the source's time budget cuts the probes (PLN0385)
        def sonda(c):
            for tent in range(4):                       # o runtime devolve 429 com paralelismo alto
                if parar.is_set(): return c, None, None, 'interrompida'
                try: m = tp.mensagens(c['id'], 1)
                except SystemExit as e:
                    if '429' in str(e.code) and tent < 3 and not parar.wait(2 * (tent + 1)): continue
                    return c, None, None, str(e.code)
                except urllib.error.URLError as e:          # 'Connection refused' transitorio do NAT do WSL em paralelo
                    if tent < 3 and not parar.wait(2 * (tent + 1)): continue
                    return c, None, None, f'URLError: {e.reason}'
                de = normaliza(m[0], '')['de'] if m else None          # remetente da ultima msg: resolve pendencia sem chamada extra
                return c, (m[0].get('createdDateTime') if m else None), de, None
        sondas = pool.mapa(sonda, unicos, 2, parar)    # the budget's interrupt never waits for the queued probes
        for c, ultimo, de, e in sondas:
            if not e: sond[c['id']] = {'ultimo': ultimo, 'sondado': agora.isoformat(), 'de': de}
        erros = [(c['id'], e) for c, _, _, e in sondas if e]
        if erros and len(erros) == len(sondas): raise RuntimeError(f'todas as sondas falharam: {erros[0][1]}')
        return sondas

    cache = slowfs.load_json(f'{tp.CFG_DIR}/chats.json')       # with a deadline on a Windows drive (PLN0245)
    nomes = {c['id']: c['nome'] for k in ('oneOnOne', 'grupos') for c in cache[k]}
    aviso_fonte = []
    if rota == 'chatsvc':
        try:
            sondas = _sondas_chatsvc(me)
            st.pop('chatsvc_falha', None)
        except Exception as e:
            # rota interna do Teams fora (login do desktop vencido, mudanca de API): volta ao conector e avisa uma vez por dia
            if st.get('chatsvc_falha') != agora.date().isoformat():
                aviso_fonte.append(f'chatsvc indisponivel ({type(e).__name__}: {str(e)[:100]}); tick pelo conector (~30 chamadas)')
                if not dry: st['chatsvc_falha'] = agora.date().isoformat()
            sondas = _sondas_conector()
        for c, ultimo, de, e in sondas:
            if not e: sond[c['id']] = {'ultimo': ultimo, 'sondado': agora.isoformat(), 'de': de}
    else:
        sondas = _sondas_conector()
    erros = [(c['id'], e) for c, _, _, e in sondas if e]
    out = []
    for c, ultimo, de, e in sondas:
        if e or not ultimo or dt(ultimo) < desde: continue
        cid = c['id']
        brutas = tp.mensagens(cid, 50)
        mencionado = {m.get('id') for m in brutas
                      if any(((x.get('mentioned') or {}).get('user') or {}).get('displayName') == me for x in m.get('mentions') or [])}
        web = {m.get('id'): m.get('webUrl') for m in brutas if m.get('webUrl')}     # Graph fills it for channel posts
        msgs = [normaliza(m, m.get('id', '')) for m in brutas]
        msgs = [m for m in msgs if (m['texto'] or m['anexos']) and m['quando']]
        msgs.sort(key=lambda m: m['quando'])
        ultimo_meu = bool(msgs) and msgs[-1]['de'] == me
        novas = [m for m in msgs if dt(m['quando']) >= desde and m['id'] not in vistos]   # inclui as minhas, para dar contexto
        if not any(m['de'] != me for m in novas): continue
        nome = c.get('topic') or nomes.get(cid) or ', '.join(sorted({m['de'] for m in novas if m['de'] != me}))
        mencao = any(m['de'] != me and m['id'] in mencionado for m in novas)   # @menção real, não o nome no texto
        chat = {'chat': nome, 'id': cid, 'grupo': bool(c.get('topic')) or bool(c.get('_ad_hoc')), 'ad_hoc': bool(c.get('_ad_hoc')),
                'respondido': ultimo_meu, 'mencao': mencao}
        if ref_re:
            refs = [f'{a}!{b}' for m in novas if m['de'] != me for a, b in ref_re.findall(m['texto'] or '')]
            chat['mr'] = refs[0] if refs else None
        def _msg(m):
            x = {'quando': m['quando'], 'de': m['de'], 'eu': m['de'] == me, 'texto': m['texto'], 'anexos': m['anexos'],
                 'id': m['id'], 'url': permalinks.teams(cid, m['id'], web.get(m['id']))}
            if imagens: x['imagens'] = _baixa_imagens(m) if m['de'] != me else []
            if not dry and m['de'] != me:
                try: x['guardados'] = _guarda(m, nome)
                except Exception as e: x['guardados'] = [f'(material nao guardado: {type(e).__name__}: {str(e)[:80]})']
            return x
        chat['msgs'] = [_msg(m) for m in novas[-MAX_POR_CHAT:]]
        chat['omitidas'] = max(0, len(novas) - MAX_POR_CHAT)
        if dedup: chat['ids'] = [m['id'] for m in novas]
        out.append(chat)
    # ordem: 1:1 e mencoes sem resposta primeiro, grupos ja respondidos por ultimo
    out.sort(key=lambda c: (c['respondido'], not (c['mencao'] or not c['grupo'] or c['ad_hoc']), c['msgs'][-1]['quando']))
    resolvidas = []
    if not dry:
        # resolve: a ultima msg do chat passou a ser minha (sonda deste tick; sem 'de' = nao sondado ainda, deixa quieto)
        for p in pend_abertas(s, 'teams'):
            h = sond.get(p['ref']) or {}
            if h.get('sondado') == agora.isoformat() and h.get('de') == me:
                resolve(p, agora, 'voce respondeu'); resolvidas.append((p, 'voce respondeu'))
        # enfileira: 1:1 ou @mencao, ultima msg nao e minha (o modelo poda o que nao for demanda com --pendente pN status=ignorar)
        for c in out:
            if c['respondido'] or (c['grupo'] and not c['mencao'] and not c['ad_hoc']): continue
            ult = [m for m in c['msgs'] if not m['eu']][-1]
            p = enfileira(s, 'teams', c['id'], c['chat'], (ult['texto'] or ', '.join(ult['anexos']))[:80], ult['quando'], agora)
            if c.get('mr'): p['mr'] = c['mr']          # msg cita a ref de outra fonte: mesma demanda (hook cruza_mr)
            p['autor'] = ult['de']                     # quem escreveu (o chat de grupo sem nome tem varios nomes)
            permalinks.guarda(p, ult.get('url'), ult['quando'], chat=c['id'], id=ult.get('id'))   # the message link
        if dedup:
            st['vistos'] = (list(vistos) + [i for c in out for i in c['ids']])[-500:]
            st['cursor'] = agora.isoformat()
        else:
            # mensagem que chega DURANTE o tick (depois de `agora`) ja foi relatada aqui; sem isto ela voltava no tick seguinte
            vistas = [dt(m['quando']) for c in out for m in c['msgs']]
            st['cursor'] = max([agora] + [v + timedelta(milliseconds=1) for v in vistas]).isoformat()
    r = {'baseline': False, 'chats': out, 'resolvidas': resolvidas}
    if rota == 'chatsvc': r['avisos_fonte'] = aviso_fonte
    r['erros'] = [f'{len(erros)} chats nao sondados ({erros[0][1][:80]})'] if erros else []
    return r


def imprime_teams(r):
    hora = core.hora
    if r['baseline']:
        print('== TEAMS: cursor plantado agora (primeira rodada); a proxima mostra o que chegar depois.'); return True
    for e in r.get('avisos_fonte', []): print(f'AVISO (teams-chatsvc): {e}')
    for e in r.get('erros', []): print(f'AVISO (teams): {e}')
    if not r['chats']: return bool(r.get('erros') or r.get('avisos_fonte'))
    print(f'== TEAMS ({sum(len(c["msgs"]) for c in r["chats"])} msgs novas em {len(r["chats"])} chats)')
    for c in r['chats']:
        tipo = 'grupo sem nome' if c.get('ad_hoc') else ('grupo' if c['grupo'] else '1:1')
        estado = 'ultima msg e sua' if c['respondido'] else 'SEM RESPOSTA SUA'
        print(f'-- {c["chat"]} ({tipo}) · {estado}' + (' · MENCIONA VOCE' if c.get('mencao') else ''))
        if c['omitidas']: print(f'   (… {c["omitidas"]} anteriores omitidas)')
        for m in c['msgs']:
            an = f'  [anexos: {", ".join(m["anexos"])}]' if m['anexos'] else ''
            if m.get('imagens'): an += ''.join(f'\n      [imagem: {i}]' for i in m['imagens'])
            if m.get('guardados'): an += ''.join(f'\n      [guardado: {g}]' for g in m['guardados'])
            quem = 'eu' if m.get('eu') else m['de']
            print(f'   [{hora(m["quando"])}] {quem}: {m["texto"]}{an}')
    return True


class Fonte(Base):
    tipo = 'teams'
    lembrar = {'teams': 4.0}
    conserto = 'Teams down — refresh the work-inbox session / connector'

    def configura(self):
        global MAX_POR_CHAT, CFG
        CFG = dict(self.cfg)
        MAX_POR_CHAT = int(self.cfg.get('max_por_chat', 20))
        ms365.configura(self.cfg.get('perfil') or sys.exit('teams: "perfil" faltando no config.json'), self.cfg.get('teams_chat_dir'))

    def tick(self, s, desde, dry): return tick_teams(s, desde, dry)

    def imprime(self, r): return imprime_teams(r)
    def linha_ref(self, p): return None
