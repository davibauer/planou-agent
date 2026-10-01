#!/usr/bin/env python3
"""Source outlook_email (result key 'email'): new Inbox e-mails through the `work-inbox` plugin (outlook_pull, Office 365
connector of Power Automate). The time cut goes to the server (KQL received>=); automatic senders (remetentes.outlook, by the address)
come as one line each.

Pending queue ('email'): an e-mail from a person that is still unread when it arrives; it resolves when it stops being
unread in the Inbox (read, deleted or moved). The item links to the message in Outlook on the web (the connector sends
no webLink, so it is built from the message id; pending items from older versions get it from their ref).

Config ("fontes": [{"tipo": "outlook_email", ...}]):
  perfil            work-inbox profile (required)
  sobreposicao_min  re-read N minutes before the cursor and drop already-seen ids (default 0 = off)
  lembrar_horas     default 11
"""
import sys, base64, urllib.parse
from datetime import datetime, timezone, timedelta

import core, compartilhado, permalinks, remetentes
from adapters import Fonte as Base, ms365

SOBREPOSICAO = timedelta(0)


def tick_email(s, desde, dry):
    dt, enfileira, resolve, pend_abertas = core.dt, core.enfileira, core.resolve, core.pend_abertas
    _, op = ms365.teams()
    st = s.setdefault('email', {'cursor': None})
    agora = datetime.now(timezone.utc)
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'pessoas': [], 'autos': []}
    dedup = bool(SOBREPOSICAO)
    vistos = set()
    if dedup:
        fixo = st.get('cursor') is None or desde != dt(st['cursor'])
        desde = desde - SOBREPOSICAO
        vistos = set() if fixo else set(st.get('vistos') or [])
    # o KQL aceita timestamp, entao o corte vai ao servidor; pagina enquanto vier pagina cheia
    q = {'folderPath': 'Inbox', 'top': 100, 'fetchOnlyUnread': 'false', 'includeAttachments': 'false',
         'searchQuery': f"received>={desde.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"}
    msgs, skip = [], 0
    while True:
        pag = op.get('/v3/Mail?' + urllib.parse.urlencode({**q, 'skip': skip})).get('value', [])
        msgs += [op.norm(m) for m in pag]
        if len(pag) < q['top'] or skip >= 900: break
        skip += q['top']
    msgs = [m for m in msgs if m['quando'] and dt(m['quando']) >= desde and m['id'] not in vistos]
    msgs.sort(key=lambda m: m['quando'])
    pessoas = [m for m in msgs if not remetentes.outlook(m['de'])]
    autos = [m for m in msgs if remetentes.outlook(m['de'])]
    resolvidas = []
    if not dry:
        # resolve: uma consulta so dos nao-lidos desde a pendencia mais antiga; id ausente = lido, apagado ou movido
        abertas = pend_abertas(s, 'email')
        if abertas:
            q2 = {**q, 'fetchOnlyUnread': 'true',
                  'searchQuery': f"received>={dt(min(p['desde'] for p in abertas)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"}
            nao_lidos, skip, completo = set(), 0, False
            while True:
                pag = op.get('/v3/Mail?' + urllib.parse.urlencode({**q2, 'skip': skip})).get('value', [])
                nao_lidos |= {m.get('id') for m in pag}
                if len(pag) < q2['top']: completo = True; break
                if skip >= 900: break            # lista truncada: nao da para afirmar "ausente = lido"
                skip += q2['top']
            for p in abertas:
                if completo and p['ref'] not in nao_lidos: resolve(p, agora, 'lido'); resolvidas.append((p, 'lido'))
        # enfileira: e-mail de pessoa que ainda esta nao-lido ao chegar
        for m in pessoas:
            if not m['lido']:
                p = enfileira(s, 'email', m['id'], str(m['de']), (m['assunto'] or '(sem assunto)')[:80], m['quando'], agora)
                permalinks.guarda(p, permalinks.outlook(m['id'], m.get('link')), m['quando'], id=m['id'])
        # pending e-mails from before 0.21.2: the ref is the message id, so the link comes from the state alone
        for p in core.pendencias(s):
            if p['fonte'] == 'email' and not p.get('link') and p.get('ref'): p['link'] = permalinks.outlook(p['ref'])
        for m in pessoas:
            try: m['guardados'] = _guarda(op, m)
            except Exception as e: m['guardados'] = [f'(material nao guardado: {type(e).__name__}: {str(e)[:80]})']
        if dedup: st['vistos'] = (list(vistos) + [m['id'] for m in msgs])[-500:]
        st['cursor'] = agora.isoformat()
    return {'baseline': False, 'pessoas': pessoas, 'autos': autos, 'resolvidas': resolvidas}


def _guarda(op, m):
    """E-mail de PESSOA: links do corpo e anexos (menos convite .ics e imagem embutida na assinatura) vao para
    <workspace>/messages/shared (compartilhado.py). Uma leitura do e-mail inteiro por e-mail de pessoa."""
    full = op.get(f"/v2/Mail/{urllib.parse.quote(m['id'], safe='')}?includeAttachments={'true' if m['anexos'] else 'false'}")
    onde = f'e-mail "{(m["assunto"] or "(sem assunto)")[:60]}"'
    g = []
    for a in full.get('attachments') or []:
        nome = a.get('name') or 'anexo'
        if a.get('isInline') or nome.lower().endswith('.ics') or not a.get('contentBytes'): continue
        p = compartilhado.guarda_arquivo(m['quando'], str(m['de']), onde, nome,
                                         lambda a=a: base64.b64decode(a['contentBytes']), chave=a.get('id'), tamanho=a.get('size'))
        if p: g.append(p)
    corpo = (full.get('body') or '') if isinstance(full.get('body'), str) else ((full.get('body') or {}).get('content') or '')
    texto = compartilhado.links_html(corpo) if '<' in corpo else corpo
    return g + compartilhado.guarda_links(m['quando'], str(m['de']), onde, texto, contexto=m.get('preview') or '')


def imprime_email(r):
    hora = core.hora
    if r['baseline']:
        print('== E-MAIL: cursor plantado agora (primeira rodada).'); return True
    if not (r['pessoas'] or r['autos']): return False
    print(f'== E-MAIL ({len(r["pessoas"])} de pessoas, {len(r["autos"])} automaticos)')
    for m in r['pessoas']:
        flags = ('' if m['lido'] else '● ') + ('📎 ' if m['anexos'] else '')
        print(f'-- [{hora(m["quando"])}] {flags}{m["de"]} — {m["assunto"]}\n   {(m["preview"] or "")[:300].replace(chr(10), " ")}\n   id: {m["id"]}')
        for g in m.get('guardados') or []: print(f'   [guardado: {g}]')
    for m in r['autos']:
        print(f'   · [{hora(m["quando"])}] {m["de"]} — {m["assunto"]}')
    return True


class Fonte(Base):
    tipo = 'outlook_email'
    nome_padrao = 'email'
    lembrar = {'email': 11.0}
    conserto = 'Outlook down — Fix connection of the Office 365 connection in Power Automate'

    def configura(self):
        global SOBREPOSICAO
        SOBREPOSICAO = timedelta(minutes=float(self.cfg.get('sobreposicao_min') or 0))
        ms365.configura(self.cfg.get('perfil') or sys.exit('outlook_email: "perfil" faltando no config.json'), self.cfg.get('teams_chat_dir'))

    def tick(self, s, desde, dry): return tick_email(s, desde, dry)
    def imprime(self, r): return imprime_email(r)
    def linha_ref(self, p): return f'   id: {p["ref"]}'
