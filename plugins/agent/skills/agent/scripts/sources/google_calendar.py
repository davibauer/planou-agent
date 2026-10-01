#!/usr/bin/env python3
"""Source google_calendar (result key 'calendar'): changes in the primary Google Calendar of one Workspace account
(read-only scope). Events changed since the cursor (updatedMin, showDeleted) against the last photo: CONVITE, MOVIDO,
CANCELADO, ALTERADO (--since only), RESPOSTAS (answers to your own event), EM BREVE (starts within 30 min).

Pending queue ('calendar'): an invite you did not answer that has not started; resolves on answer, cancel or when it passes.

Config ("fontes": [{"tipo": "google_calendar", ...}]):
  google_dir        folder with client_secret.json and token.json (default secrets/google)
  sobreposicao_min  default 2
  em_breve_min      default 30
  horizonte_dias    default 60
  lembrar_horas     default 4
"""
import os, re, sys, json
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Fonte as Base, google_auth
from core import enfileira, resolve, pend_abertas

SOBREPOSICAO = timedelta(minutes=2)
EM_BREVE = timedelta(minutes=30)
BRT = timezone(timedelta(hours=-3))


def dt(iso): return core.dt(iso)


def hora(iso): return core.hora(iso)


CAL = 'primary'
IGNORAR_TIPOS = {'workingLocation', 'birthday'}   # "Home" todo dia e aniversarios: nao sao reuniao
HORIZONTE = timedelta(days=60)   # so eventos que comecam ate N dias a frente (o resto nao e' demanda agora)


def svc():
    from googleapiclient.discovery import build
    return build('calendar', 'v3', credentials=google_auth.creds(), cache_discovery=False)


def _quando(x):
    """start/end do evento -> (iso, dia_inteiro). Evento de dia inteiro vem como 'date' (sem hora)."""
    if not x: return None, False
    if x.get('dateTime'): return dt(x['dateTime']).isoformat(), False
    if x.get('date'): return datetime.fromisoformat(x['date']).replace(tzinfo=BRT).isoformat(), True
    return None, False


def _nome(p):
    return (p.get('displayName') or p.get('email') or '?').strip()


def norm(e):
    ini, dia = _quando(e.get('start'))
    fim, _ = _quando(e.get('end'))
    conv = e.get('attendees') or []
    eu = next((a for a in conv if a.get('self')), None)
    org = e.get('organizer') or {}
    outros = [a for a in conv if not a.get('self') and not a.get('resource')]
    return {'id': e['id'], 'titulo': e.get('summary') or '(sem titulo)', 'status': e.get('status'),
            'ini': ini, 'fim': fim, 'dia_inteiro': dia, 'criado': e.get('created'), 'atualizado': e.get('updated'),
            'organizador': _nome(org), 'org_eu': bool(org.get('self')),
            'minha_resposta': (eu or {}).get('responseStatus') if eu else ('organizador' if org.get('self') else None),
            'convidados': {_nome(a): a.get('responseStatus') for a in outros},
            'meet': e.get('hangoutLink') or (e.get('conferenceData') or {}).get('entryPoints', [{}])[0].get('uri'),
            'local': e.get('location'), 'descricao': e.get('description'), 'link': e.get('htmlLink'),
            'recorrente': bool(e.get('recurringEventId'))}


def _lista(cal, **kw):
    out, tok = [], None
    while True:
        r = cal.events().list(calendarId=CAL, singleEvents=True, maxResults=250, pageToken=tok, **kw).execute()
        out += [e for e in r.get('items', []) if e.get('eventType') not in IGNORAR_TIPOS]
        tok = r.get('nextPageToken')
        if not tok: break
    return out


def mudados(cal, desde, agora=None):
    """Eventos criados/alterados/cancelados desde `desde` que ainda nao terminaram (ou comecam ate o horizonte). Ordem por 'updated'."""
    agora = agora or datetime.now(timezone.utc)
    itens = _lista(cal, updatedMin=desde.isoformat(), showDeleted=True,
                   timeMin=(agora - timedelta(hours=1)).isoformat(), timeMax=(agora + HORIZONTE).isoformat())
    out = [norm(e) for e in itens]
    return sorted(out, key=lambda e: e['atualizado'] or '')


def proximos(cal, ate, agora=None):
    agora = agora or datetime.now(timezone.utc)
    itens = _lista(cal, timeMin=agora.isoformat(), timeMax=ate.isoformat(), orderBy='startTime')
    return [norm(e) for e in itens if e.get('status') != 'cancelled']


def janela(ini, fim, cal=None):
    """Eventos entre ini e fim (datetimes aware), ordem por inicio — mesma assinatura do calendario do Outlook
    (usada pelo motor compartilhado de gravacoes, watch_core.recordings)."""
    itens = _lista(cal or svc(), timeMin=ini.isoformat(), timeMax=fim.isoformat(), orderBy='startTime')
    return [norm(e) for e in itens if e.get('status') != 'cancelled']


def evento(cal, eid):
    return norm(cal.events().get(calendarId=CAL, eventId=eid).execute())


def estado(cal, ids):
    """Para resolver pendencias: {id: 'needsAction'|'accepted'|'declined'|'tentative'|'cancelado'|'apagado'|'passou'}."""
    est = {}
    agora = datetime.now(timezone.utc)
    for eid in ids:
        try:
            e = evento(cal, eid)
        except Exception as ex:
            st = getattr(getattr(ex, 'resp', None), 'status', None)
            if st in (404, 410): est[eid] = 'apagado'
            continue
        if e['status'] == 'cancelled': est[eid] = 'cancelado'
        elif e['ini'] and dt(e['ini']) < agora: est[eid] = 'passou'
        else: est[eid] = e['minha_resposta'] or 'needsAction'
    return est


RESP = {'needsAction': 'sem resposta', 'accepted': 'aceito', 'declined': 'recusado', 'tentative': 'talvez', 'organizador': 'organizador'}


def faixa(e):
    if not e['ini']: return '??'
    if e['dia_inteiro']:
        a, b = dt(e['ini']).astimezone(BRT), dt(e['fim']).astimezone(BRT) - timedelta(days=1) if e['fim'] else None
        return a.strftime('%d/%m') + (f'–{b.strftime("%d/%m")}' if b and b.date() != a.date() else '') + ' (dia inteiro)'
    a = dt(e['ini']).astimezone(BRT); b = dt(e['fim']).astimezone(BRT) if e['fim'] else None
    dia = {0: 'seg', 1: 'ter', 2: 'qua', 3: 'qui', 4: 'sex', 5: 'sab', 6: 'dom'}[a.weekday()]
    return f'{dia} {a.strftime("%d/%m %H:%M")}' + (f'–{b.strftime("%H:%M")}' if b else '')


def linha(e, tag=''):
    quem = '' if e['org_eu'] else f' · de {e["organizador"]}'
    resp = f' · {RESP.get(e["minha_resposta"], e["minha_resposta"])}' if e['minha_resposta'] and not e['org_eu'] else ''
    return f'-- {tag}{e["titulo"]} · {faixa(e)}{quem}{resp}'


def imprime_evento(e):
    print(linha(e))
    if e['local']: print(f'   local: {e["local"]}')
    if e['meet']: print(f'   meet: {e["meet"]}')
    if e['convidados']:
        print('   convidados: ' + ', '.join(f'{n} ({RESP.get(s, s)})' for n, s in e['convidados'].items()))
    if e['descricao']:
        d = re.sub(r'<[^>]+>', '', e['descricao']); d = re.sub(r'\n{3,}', '\n\n', d).strip()
        print('   ' + d[:1500].replace('\n', '\n   '))
    print(f'   id: {e["id"]}' + (f' · {e["link"]}' if e['link'] else ''))


def _marca(e):
    """Impressao digital do evento guardada no estado: so mudanca aqui vira relato (RSVP de terceiro bumpa 'updated' o tempo todo)."""
    return {'ini': e['ini'], 'fim': e['fim'], 'status': e['status'], 'titulo': e['titulo'],
            'resposta': e['minha_resposta'], 'convidados': e['convidados']}

def tick_calendar(s, desde, dry):
    cal = svc()
    st = s.setdefault('calendar', {'cursor': None, 'eventos': {}, 'avisados': []})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:                      # primeira rodada: planta o cursor e fotografa a agenda (base do diff)
        foto = {e['id']: _marca(e) for e in proximos(cal, agora + HORIZONTE, agora)}   # antes do cursor: se a API falhar, nao planta
        if not dry:
            st['cursor'] = agora.isoformat(); st['eventos'] = foto
        return {'baseline': True, 'itens': []}
    desde = desde - SOBREPOSICAO
    conhecidos = st.setdefault('eventos', {})
    itens = []
    for e in mudados(cal, desde, agora):
        ant = None if fixo else conhecidos.get(e['id'])   # --since: sem diff contra a foto, mostra o estado atual do que mudou na janela
        nova = _marca(e)
        if e['status'] == 'cancelled':
            if fixo or (ant and ant.get('status') != 'cancelled'): itens.append({'tipo': 'cancelado', 'ev': e})
        elif ant is None:
            if dt(e['criado']) and dt(e['criado']) >= desde:
                if not e['org_eu']: itens.append({'tipo': 'convite', 'ev': e})
            elif fixo: itens.append({'tipo': 'alterado', 'ev': e})
            # ant None e criado antes do cursor fora do --since: instancia que entrou no horizonte agora; so fotografa
            # (limitacao: se ela entrou no horizonte ja movida, essa 1a mudanca nao e' reportada; a proxima sera)
        else:
            if (ant['ini'], ant['fim']) != (e['ini'], e['fim']) or ant.get('titulo') != e['titulo']:
                itens.append({'tipo': 'movido', 'ev': e, 'de': ant})
            elif e['org_eu'] and ant.get('convidados') != e['convidados']:
                resp = {n: r for n, r in e['convidados'].items() if (ant.get('convidados') or {}).get(n) != r and r != 'needsAction'}
                if resp: itens.append({'tipo': 'respostas', 'ev': e, 'resp': resp})
            # so a sua resposta / descricao / RSVP repetido: silencioso, atualiza a foto
        if not (dry or fixo): conhecidos[e['id']] = nova
    # reuniao comecando em ate 30 min (uma vez por evento; recusada nao conta)
    avisados = set() if fixo else set(st.get('avisados') or [])
    breve = []
    for e in proximos(cal, agora + EM_BREVE, agora):
        if e['id'] in avisados or e['minha_resposta'] == 'declined' or e['dia_inteiro'] or not e['ini']: continue
        if dt(e['ini']) < agora: continue
        breve.append({'tipo': 'em_breve', 'ev': e, 'min': int((dt(e['ini']) - agora).total_seconds() // 60)})
    itens += breve
    resolvidas = []
    if not (dry or fixo):
        abertas = pend_abertas(s, 'calendar')
        if abertas:
            est = estado(cal, [p['ref'] for p in abertas])
            for p in abertas:
                x = est.get(p['ref'])
                if x in ('accepted', 'declined', 'tentative'): resolve(p, agora, 'voce respondeu'); resolvidas.append((p, 'voce respondeu'))
                elif x in ('cancelado', 'apagado', 'passou'): resolve(p, agora, x); resolvidas.append((p, x))
        for it in itens:
            e = it['ev']
            if it['tipo'] == 'convite' and e['minha_resposta'] == 'needsAction' and e['ini'] and dt(e['ini']) > agora:
                p = enfileira(s, 'calendar', e['id'], e['organizador'], f'{e["titulo"]} · {faixa(e)}'[:80], e['criado'] or agora.isoformat(), agora)
                p['link'] = e['link']
        st['avisados'] = (list(avisados) + [it['ev']['id'] for it in breve])[-200:]
        # poda a foto: evento que ja terminou ha mais de 1 dia nao muda mais nada
        lim = (agora - timedelta(days=1)).isoformat()
        for k in [k for k, v in conhecidos.items() if v.get('fim') and v['fim'] < lim]: del conhecidos[k]
        st['cursor'] = agora.isoformat()
    return {'baseline': False, 'itens': itens, 'resolvidas': resolvidas}

def imprime_calendar(r):
    if r['baseline']:
        print('== CALENDARIO: cursor plantado agora (primeira rodada); agenda fotografada para o diff.'); return True
    if not r['itens']: return False
    print(f'== CALENDARIO ({len(r["itens"])} mudancas)')
    for it in r['itens']:
        e = it['ev']
        if it['tipo'] == 'convite':
            print(linha(e, 'CONVITE: '))
            if e['convidados']: print('   convidados: ' + ', '.join(f'{n} ({RESP.get(s, s)})' for n, s in e['convidados'].items()))
            if e['descricao']: print('   ' + (e['descricao'] or '').replace('\n', ' ')[:300])
        elif it['tipo'] == 'movido':
            de = it['de']
            print(f'-- MOVIDO: {e["titulo"]} · de {faixa(dict(e, ini=de["ini"], fim=de["fim"]))} → {faixa(e)}'
                  + ('' if e['org_eu'] else f' · de {e["organizador"]}') + (f' · era "{de["titulo"]}"' if de.get('titulo') != e['titulo'] else ''))
        elif it['tipo'] == 'cancelado': print(linha(e, 'CANCELADO: '))
        elif it['tipo'] == 'alterado': print(linha(e, 'ALTERADO: ') + f' · atualizado {hora(e["atualizado"])}')
        elif it['tipo'] == 'respostas':
            print(f'-- RESPOSTAS (seu evento): {e["titulo"]} · {faixa(e)} · ' + ', '.join(f'{n}: {RESP.get(r, r)}' for n, r in it['resp'].items()))
        elif it['tipo'] == 'em_breve':
            print(f'-- EM BREVE: {e["titulo"]} · {faixa(e)} (em {it["min"]} min)' + ('' if e['org_eu'] else f' · de {e["organizador"]}'))
        if e['meet'] and it['tipo'] in ('convite', 'em_breve', 'movido'): print(f'   meet: {e["meet"]}')
        if it['tipo'] != 'em_breve': print(f'   id: {e["id"]}' + (f' · {e["link"]}' if e['link'] else ''))
    return True


class Fonte(Base):
    tipo = 'google_calendar'
    nome_padrao = 'calendar'
    lembrar = {'calendar': 4.0}
    conserto = 'Google Calendar down — Google OAuth token (adapters/google_auth.py)'

    def configura(self):
        global SOBREPOSICAO, EM_BREVE, HORIZONTE, BRT
        google_auth.configura(paths.google_dir(self.cfg.get('google_dir')))
        SOBREPOSICAO = timedelta(minutes=float(self.cfg.get('sobreposicao_min', 2)))
        EM_BREVE = timedelta(minutes=float(self.cfg.get('em_breve_min', 30)))
        HORIZONTE = timedelta(days=float(self.cfg.get('horizonte_dias', 60)))
        BRT = core.BRT

    def tick(self, s, desde, dry): return tick_calendar(s, desde, dry)
    def imprime(self, r): return imprime_calendar(r)
    def modulo_agenda(self): return sys.modules[__name__]

    def args(self, ap):
        ap.add_argument('--agenda-google', nargs='*', metavar='proximos DIAS|evento ID', help='Google Calendar (so leitura)')

    def cli(self, a, ctx):
        if a.agenda_google is None: return False
        cal = svc(); args = a.agenda_google
        if args and args[0] == 'evento': imprime_evento(evento(cal, args[1])); return True
        dias = float(args[1]) if len(args) > 1 else 7
        for e in proximos(cal, datetime.now(timezone.utc) + timedelta(days=dias)): print(linha(e) + (f'\n   {e["meet"]}' if e['meet'] else ''))
        return True
