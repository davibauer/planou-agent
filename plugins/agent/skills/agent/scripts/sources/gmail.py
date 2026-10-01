#!/usr/bin/env python3
"""Source gmail (result key 'email'): new Inbox e-mails of one Google Workspace account through the Gmail API
(read-only scope: never marks as read, moves or answers). Automatic senders and Promotions/Social/Forums come grouped
one line per sender+subject. Calendar notification e-mails count as automatic (the calendar source owns meetings).

Optional: Azure Monitor alert e-mails ("Azure: Activated|Deactivated Severity: N rule") become a state machine per rule
(activated / still active for more than "alerta_longo_min" / deactivated with duration) instead of a list; the hook
alertas_azure prints them as == ALERTAS.

Pending queue ('email'): an e-mail from a person still unread when it arrives; it resolves when it is read or deleted.
Each pending item keeps the link to the message (p['link'], permalinks.gmail).

Config ("fontes": [{"tipo": "gmail", ...}]):
  google_dir        folder with client_secret.json and token.json (default secrets/google)
  email             account the message links open in (default: the address of the Gmail profile)
  sobreposicao_min  re-read N minutes before the cursor, dedupe by id (default 2)
  alertas_azure     default false
  alerta_longo_min  default 30
  lembrar_horas     default 11
  remetentes_automaticos  extra automatic senders on top of the built-in ones (list). An entry with "@" matches anywhere
                    in the address ("@supplier.example", "digest@"); one without "@" is a local-part role, like the
                    built-in ones ("boletim" -> boletim@, boletim.br@, loja-boletim@)

Automatic = any of: a built-in sender (only the address, never the display name: noreply, notification, newsletter and
other tokens anywhere in it, notification domains, or a short mailbox like hi@, team@, support@ as the first or last
piece of the local part, so sachi@ and steam@ stay people), a Promotions/Social/Forums label, a calendar subject, a
meeting-notes sender (gemini-notes@, notes@, meeting-recap@), a marketing or system role as the first or last piece of the local part
(corporativo@, comercial.sp@, marketing-news@; not when the message is a reply, In-Reply-To), or a bulk header
(Precedence: bulk|junk, Auto-Submitted other than "no", List-Unsubscribe outside a mailing list, i.e. without List-Id
and without Precedence: list, so a colleague writing to a Google Group still counts as a person).
"""
import os, re, sys, base64
from email.utils import parseaddr
from datetime import datetime, timezone, timedelta

import core, paths, permalinks, remetentes
from adapters import Fonte as Base, google_auth
from core import enfileira, resolve, pend_abertas

SOBREPOSICAO = timedelta(minutes=2)
ALERTAS = False
ALERTA_LONGO = timedelta(minutes=30)
CONTA = None          # account e-mail for the links (config "email", else the Gmail profile)

# built-in automatic senders, on the address only (remetentes.py, shared with outlook_pull): tokens no person's
# name contains anywhere in the address, and short mailbox names (hi, team, sales, rh, jira...) only as the first or last
# piece of the local part split on . _ + -, like PAPEIS_AUTO, but also on a reply
AUTOMATICOS = remetentes.AUTO_GMAIL
CAIXAS_AUTO = remetentes.CAIXAS_GMAIL
CATEGORIAS_RUIDO = {'CATEGORY_PROMOTIONS', 'CATEGORY_SOCIAL', 'CATEGORY_FORUMS'}
# local part of the address split on . _ + -: the first or last piece being one of these = automatic sender
# (marketing and system mailboxes; a reply from one of them, In-Reply-To, still counts as a person)
PAPEIS_AUTO = frozenset((
    'corporativo', 'comercial', 'marketing', 'mkt', 'promo', 'promocao', 'promocoes', 'promotions', 'ofertas', 'offers',
    'novidades', 'news', 'newsletter', 'newsletters', 'boletim', 'informativo', 'noticias', 'eventos', 'events',
    'webinar', 'webinars', 'campanha', 'campanhas', 'campaigns', 'comunicacao', 'comunicados', 'relacionamento',
    'institucional', 'digest', 'notificacao', 'notificacoes', 'avisos', 'alertas', 'sistema', 'system', 'automated',
    'automatico', 'mailer', 'bounce', 'bounces', 'postmaster', 'bot'))
# automatic meeting notes (the notes assistant of the video call); the last piece of the local part, even on a reply
NOTAS_REUNIAO = frozenset(('notes', 'meetingnotes', 'recap', 'recaps', 'minutes', 'transcript', 'transcripts'))
EXTRA_PAPEIS = frozenset()     # config "remetentes_automaticos" without "@"
EXTRA_TRECHOS = ()             # config "remetentes_automaticos" with "@"
# headers the list call asks for (format=metadata returns only these)
CABECALHOS = ['From', 'To', 'Subject', 'In-Reply-To', 'Precedence', 'Auto-Submitted', 'List-Unsubscribe', 'List-Id']
# e-mail do Google Calendar vem em nome do organizador (pessoa), mas reuniao e' assunto da fonte de calendario: aqui vira automatico
CALENDARIO_ASSUNTO = re.compile(
    r"^(re:\s*)?(invitation|updated invitation|accepted|declined|tentatively accepted|canceled event|cancelled event|"
    r"convite|convite atualizado|aceito|recusado|aceito provisoriamente|evento cancelado|nova hora proposta|new time proposed)\b"
    r"|@ (seg|ter|qua|qui|sex|s[aá]b|dom|mon|tue|wed|thu|fri|sat|sun)\b.*\(.*@.*\)\s*$", re.I)


def svc():
    from googleapiclient.discovery import build
    return build('gmail', 'v1', credentials=google_auth.creds(), cache_discovery=False)


def _hdr(m, nome):
    for h in (m.get('payload') or {}).get('headers', []):
        if h['name'].lower() == nome.lower(): return h['value']
    return ''


def _limpa(t):
    """Tira o enchimento invisivel de newsletter (U+034F, ZWNJ, ZWSP, soft hyphen) e colapsa espacos."""
    t = re.sub(r'[\u034f\u200b\u200c\u200d\u00ad\ufeff]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def agrupa_autos(autos):
    """Automaticos agrupados por (remetente, assunto sem Re/Fw e sem numeros variaveis): [(primeiro, ultimo, de, assunto, n)]."""
    grupos = {}
    for m in autos:
        a = re.sub(r"^(re|fw|fwd|enc):\s*", "", m['assunto'] or '(sem assunto)', flags=re.I).strip()
        k = (m['de'], re.sub(r'\d+', '#', a).lower()[:80])
        g = grupos.setdefault(k, [m['quando'], m['quando'], m['de'], a, 0])
        g[0] = min(g[0], m['quando']); g[1] = max(g[1], m['quando']); g[4] += 1
    return sorted(grupos.values(), key=lambda g: g[1])


_local = remetentes.pecas


def remetente_fixo(de):
    """Built-in automatic sender by the address (never the display name): AUTOMATICOS anywhere in it, or a CAIXAS_AUTO
    name as the first or last piece of the local part. Holds on a reply too."""
    return remetentes.gmail(de)


def remetente_auto(de, resposta=False):
    """Automatic by the address (never the display name): meeting notes, a marketing/system role, or a config extra."""
    addr = parseaddr(de or '')[1].lower()
    if '@' not in addr: return False
    if any(t in addr for t in EXTRA_TRECHOS): return True
    pecas = _local(addr)
    if not pecas: return False
    if pecas[-1] in NOTAS_REUNIAO or ''.join(pecas[-2:]) == 'meetingnotes': return True
    if resposta: return False
    papeis = PAPEIS_AUTO | EXTRA_PAPEIS
    return pecas[0] in papeis or pecas[-1] in papeis


def cabecalho_em_massa(m):
    """Bulk mail by its headers. List-Unsubscribe alone is not enough inside a mailing list (List-Id or
    Precedence: list): a person writing to a Google Group carries it too."""
    prec = _hdr(m, 'Precedence').strip().lower()
    if prec in ('bulk', 'junk'): return True
    auto_sub = _hdr(m, 'Auto-Submitted').strip().lower()
    if auto_sub and auto_sub != 'no': return True
    return bool(_hdr(m, 'List-Unsubscribe')) and not _hdr(m, 'List-Id') and prec != 'list'


def norm(m):
    labels = set(m.get('labelIds') or [])
    de = _hdr(m, 'From')
    q = datetime.fromtimestamp(int(m['internalDate']) / 1000, tz=timezone.utc).isoformat()
    partes = (m.get('payload') or {}).get('parts') or []
    anexos = any(p.get('filename') for p in partes)
    assunto = _hdr(m, 'Subject')
    auto = (remetente_fixo(de) or bool(labels & CATEGORIAS_RUIDO) or bool(CALENDARIO_ASSUNTO.search(assunto))
            or remetente_auto(de, resposta=bool(_hdr(m, 'In-Reply-To'))) or cabecalho_em_massa(m))
    return {'id': m['id'], 'thread': m.get('threadId'), 'quando': q, 'de': de, 'para': _hdr(m, 'To'),
            'assunto': assunto, 'preview': _limpa(m.get('snippet') or ''), 'lido': 'UNREAD' not in labels,
            'anexos': anexos, 'auto': auto, 'categoria': next((l for l in labels if l.startswith('CATEGORY_')), None)}


def _batch_get(g, ids, **kw):
    """messages.get em lote (50 por batch). Devolve ({id: msg}, {id: status_http_do_erro})."""
    ok, erro = {}, {}
    def cb(rid, resp, exc):
        if exc:
            erro[rid] = getattr(getattr(exc, 'resp', None), 'status', None) or str(exc)
        else:
            ok[rid] = resp
    for i in range(0, len(ids), 50):
        b = g.new_batch_http_request(callback=cb)
        for mid in ids[i:i + 50]:
            b.add(g.users().messages().get(userId='me', id=mid, **kw), request_id=mid)
        b.execute()
    return ok, erro


def novos(g, desde, max_msgs=300):
    """E-mails da Inbox recebidos a partir de `desde` (datetime aware). Ordem crescente. `after:` aceita epoch em segundos."""
    epoch = int(desde.timestamp())
    ids, tok = [], None
    while True:
        r = g.users().messages().list(userId='me', q=f'in:inbox after:{epoch}', maxResults=100, pageToken=tok).execute()
        ids += [m['id'] for m in r.get('messages', [])]
        tok = r.get('nextPageToken')
        if not tok or len(ids) >= max_msgs: break
    if not ids: return []
    ok, _ = _batch_get(g, ids, format='metadata', metadataHeaders=CABECALHOS)
    out = [norm(m) for m in ok.values()]
    out = [m for m in out if datetime.fromisoformat(m['quando']) >= desde]
    out.sort(key=lambda m: m['quando'])
    return out


def nao_lidos(g, ids):
    """Para resolver pendencias: {id: 'nao_lido'|'lido'|'apagado'} sem tocar nas mensagens."""
    if not ids: return {}
    ok, erro = _batch_get(g, list(ids), format='minimal')
    est = {}
    for mid in ids:
        if mid in ok: est[mid] = 'nao_lido' if 'UNREAD' in (ok[mid].get('labelIds') or []) else 'lido'
        elif erro.get(mid) == 404: est[mid] = 'apagado'
    return est


def _texto(payload):
    """Percorre as partes MIME: prefere text/plain; cai em HTML sem tags."""
    plain, html = [], []
    def walk(p):
        mt = p.get('mimeType', '')
        data = (p.get('body') or {}).get('data')
        if data and mt == 'text/plain': plain.append(base64.urlsafe_b64decode(data).decode('utf-8', 'replace'))
        elif data and mt == 'text/html': html.append(base64.urlsafe_b64decode(data).decode('utf-8', 'replace'))
        for q in p.get('parts') or []: walk(q)
    walk(payload)
    if plain: return '\n'.join(plain)
    h = '\n'.join(html)
    h = re.sub(r'(?is)<(style|script).*?</\1>', '', h)
    h = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', h)
    h = re.sub(r'<[^>]+>', '', h)
    import html as H
    return re.sub(r'\n{3,}', '\n\n', H.unescape(h)).strip()


def corpo(g, mid):
    m = g.users().messages().get(userId='me', id=mid, format='full').execute()
    n = norm(m)
    n['corpo'] = _texto(m['payload'])
    n['anexos_nomes'] = [p['filename'] for p in (m['payload'].get('parts') or []) if p.get('filename')]
    return n


def imprime_autos(autos):
    for ini, fim, de, assunto, n in agrupa_autos(autos):
        faixa = hora(fim) if n == 1 else f'{hora(ini)}–{hora(fim)[6:] if hora(ini)[:5] == hora(fim)[:5] else hora(fim)}'
        print(f'   · [{faixa}] {de} — {assunto}' + (f' (×{n})' if n > 1 else ''))



def dt(iso): return core.dt(iso)


def hora(iso): return core.hora(iso)


def _conta(g, st):
    """The account the links open in: config "email", else the Gmail profile address (kept in the state)."""
    if CONTA: return CONTA
    if not st.get('conta'):
        try: st['conta'] = g.users().getProfile(userId='me').execute().get('emailAddress')
        except Exception: return None
    return st.get('conta')


def tick_email(s, desde, dry):
    g = svc()
    st = s.setdefault('email', {'cursor': None, 'vistos': []})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None               # --since: janela fixa, sem dedupe pelos vistos
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'pessoas': [], 'autos': []}
    vistos = set() if fixo else set(st.get('vistos') or [])
    msgs = [m for m in novos(g, desde - SOBREPOSICAO) if m['id'] not in vistos and dt(m['quando']) >= desde - SOBREPOSICAO]
    pessoas = [m for m in msgs if not m['auto']]
    autos = [m for m in msgs if m['auto']]
    # alertas do Azure Monitor (assunto "Azure: Activated|Deactivated Severity: N regra") viram estado, nao lista
    alertas_ev, autos = _alertas(s, autos, dry or fixo, agora) if ALERTAS else ([], autos)
    resolvidas = []
    if not dry:
        # resolve: lote de messages.get(minimal) so dos ids pendentes; sem UNREAD = lido, 404 = apagado
        abertas = pend_abertas(s, 'email')
        if abertas:
            est = nao_lidos(g, [p['ref'] for p in abertas])
            for p in abertas:
                e = est.get(p['ref'])
                if e in ('lido', 'apagado'): resolve(p, agora, e); resolvidas.append((p, e))
        # enfileira: e-mail de pessoa que ainda esta nao-lido ao chegar
        conta = _conta(g, st)
        for m in pessoas:
            if not m['lido']:
                p = enfileira(s, 'email', m['id'], m['de'], (m['assunto'] or '(sem assunto)')[:80], m['quando'], agora)
                permalinks.guarda(p, permalinks.gmail(m['id'], conta), m['quando'], id=m['id'], thread=m.get('thread'))
        # pending e-mails from before 0.21.2: the ref is the message id, so the link comes from the state alone
        for p in core.pendencias(s):
            if p['fonte'] == 'email' and not p.get('link') and p.get('ref'): p['link'] = permalinks.gmail(p['ref'], conta)
        st['vistos'] = (list(vistos) + [m['id'] for m in msgs])[-500:]
        st['cursor'] = agora.isoformat()
    r = {'baseline': False, 'pessoas': pessoas, 'autos': autos, 'resolvidas': resolvidas}
    if ALERTAS: r['alertas'] = alertas_ev
    return r

ALERTA_RE = re.compile(r'^Azure:\s*(Activated|Deactivated)\s*Severity:\s*(\d)\s+(\S+)', re.I)

def _alertas(s, autos, so_leitura, agora):
    """Maquina de estado por regra: ativou / desativou (com duracao) / ainda ativo ha >30 min (uma vez). Tira esses e-mails dos automaticos."""
    st = s.setdefault('alertas', {})
    resto, eventos, tocados = [], [], {}
    for m in sorted(autos, key=lambda m: m['quando']):
        mm = ALERTA_RE.match(m['assunto'] or '')
        if not mm: resto.append(m); continue
        acao, sev, regra = mm.group(1).lower(), int(mm.group(2)), mm.group(3)
        a = dict(tocados.get(regra) or st.get(regra) or {'ativo': False, 'desde': None, 'sev': sev, 'longo': False})
        if acao == 'activated':
            if not a['ativo']: a.update(ativo=True, desde=m['quando'], sev=sev, longo=False)
        else:
            if a['ativo']:
                dur = int((dt(m['quando']) - dt(a['desde'])).total_seconds() // 60) if a.get('desde') else None
                eventos.append({'tipo': 'desativou', 'regra': regra, 'sev': sev, 'quando': m['quando'], 'min': dur, 'desde': a.get('desde')})
            a.update(ativo=False, desde=None, longo=False)
        tocados[regra] = a
    for regra, a in tocados.items():
        if a['ativo'] and not any(e['regra'] == regra for e in eventos) and not (st.get(regra) or {}).get('ativo'):
            eventos.append({'tipo': 'ativou', 'regra': regra, 'sev': a['sev'], 'quando': a['desde']})
    # ainda ativo ha mais de ALERTA_LONGO (regras que nao mudaram neste tick tambem)
    for regra, a in {**st, **tocados}.items():
        if a.get('ativo') and a.get('desde') and not a.get('longo') and agora - dt(a['desde']) > ALERTA_LONGO:
            eventos.append({'tipo': 'longo', 'regra': regra, 'sev': a.get('sev'), 'quando': a['desde'], 'min': int((agora - dt(a['desde'])).total_seconds() // 60)})
            a = dict(a, longo=True); tocados[regra] = a
    if not so_leitura:
        for regra, a in tocados.items(): st[regra] = a
    return eventos, resto

def imprime_alertas(evs):
    if not evs: return False
    print(f'== ALERTAS (Azure Monitor, {len(evs)} mudancas)')
    for e in evs:
        if e['tipo'] == 'ativou': print(f'-- ATIVOU: {e["regra"]} (sev {e["sev"]}) as {hora(e["quando"])}')
        elif e['tipo'] == 'longo': print(f'-- AINDA ATIVO: {e["regra"]} (sev {e["sev"]}) ha {e["min"]} min, desde {hora(e["quando"])}')
        else: print(f'   ✓ {e["regra"]} desativou as {hora(e["quando"])}' + (f' (ficou {e["min"]} min ativo)' if e.get('min') is not None else ''))
    return True

def imprime_email(r):
    if r['baseline']:
        print('== E-MAIL: cursor plantado agora (primeira rodada).'); return True
    if not (r['pessoas'] or r['autos']): return False
    print(f'== E-MAIL ({len(r["pessoas"])} de pessoas, {len(r["autos"])} automaticos)')
    for m in r['pessoas']:
        flags = ('' if m['lido'] else '● ') + ('📎 ' if m['anexos'] else '')
        print(f'-- [{hora(m["quando"])}] {flags}{m["de"]} — {m["assunto"]}\n   {(m["preview"] or "")[:300]}\n   id: {m["id"]}')
    imprime_autos(r['autos'])
    return True


class Fonte(Base):
    tipo = 'gmail'
    nome_padrao = 'email'
    lembrar = {'email': 11.0}
    conserto = 'Gmail down — Google OAuth token (adapters/google_auth.py)'

    def configura(self):
        global SOBREPOSICAO, ALERTAS, ALERTA_LONGO, CONTA, EXTRA_PAPEIS, EXTRA_TRECHOS
        extras = [str(x).strip().lower() for x in (self.cfg.get('remetentes_automaticos') or []) if str(x).strip()]
        EXTRA_PAPEIS = frozenset(x for x in extras if '@' not in x)
        EXTRA_TRECHOS = tuple(x for x in extras if '@' in x)
        CONTA = self.cfg.get('email') or None
        google_auth.configura(paths.google_dir(self.cfg.get('google_dir')))
        SOBREPOSICAO = timedelta(minutes=float(self.cfg.get('sobreposicao_min', 2)))
        ALERTAS = bool(self.cfg.get('alertas_azure'))
        ALERTA_LONGO = timedelta(minutes=float(self.cfg.get('alerta_longo_min', 30)))

    def tick(self, s, desde, dry): return tick_email(s, desde, dry)
    def imprime(self, r): return imprime_email(r)

    def args(self, ap):
        ap.add_argument('--gmail', metavar='ID', help='corpo completo de um e-mail (so leitura)')

    def cli(self, a, ctx):
        if not a.gmail: return False
        m = corpo(svc(), a.gmail)
        print(f'De: {m["de"]}\nPara: {m["para"]}\nAssunto: {m["assunto"]}\nQuando: {hora(m["quando"])}' +
              (f'\nAnexos: {", ".join(m["anexos_nomes"])}' if m['anexos_nomes'] else '') + f'\n\n{m["corpo"]}')
        return True
