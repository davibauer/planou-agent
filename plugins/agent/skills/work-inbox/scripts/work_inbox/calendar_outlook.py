#!/usr/bin/env python3
"""Calendario do Outlook (Office 365) da conta do perfil, pelo mesmo runtime/conexao Office 365 do outlook_pull.py.

Biblioteca do adapter de agenda do work-watch (janela / evento / estado) e CLI de consulta:
  calendar_outlook.py --next 7d        # agenda: o que vem nos proximos N dias (default 7d); --proximos = apelido
  calendar_outlook.py --event <id>     # um evento por inteiro (descricao, convidados, link do Teams); --evento = apelido
  calendar_outlook.py --json ...

So leitura: nao aceita, nao recusa, nao cria nada. Rota do conector: GetEventsCalendarViewV3
(`/datasets/calendars/v3/tables/items/calendarview?calendarId=…&startDateTimeUtc=…&endDateTimeUtc=…`) — devolve as
INSTANCIAS (serie recorrente expandida, cada ocorrencia com id proprio), que e' o que o diff precisa; `…/tables/{cal}/items`
com $filter devolve a serie-mae (start = primeira ocorrencia) e nao serve. Nao ha campo de status nem showDeleted:
evento cancelado/apagado simplesmente some da janela — o agent detecta por ausencia contra a foto do estado.
Copia UNICA (23/09/2026): dois agents (entao vigias) tinham o mesmo arquivo com so' o PERFIL diferente; agora quem usa fixa
CALENDAR_PERFIL (o adapter ms365 do work-watch faz isso) e importa este modulo.
"""
import os, sys, re, json, argparse, html, urllib.parse
from datetime import datetime, timezone, timedelta

PERFIL = os.environ.get('CALENDAR_PERFIL') or sys.exit('calendar_outlook: defina CALENDAR_PERFIL (o adapter ms365 do work-watch faz isso)')
TEAMS_DIR = os.path.dirname(os.path.abspath(__file__))
BRT = timezone(timedelta(hours=-3))
HORIZONTE = timedelta(days=21)   # ate onde a agenda e' fotografada (2 blocos de OOF por dia util enchem rapido; 21 d ~ 100 linhas)
PAGINA = 250

os.environ.setdefault('WORK_INBOX_PROFILE', PERFIL)
sys.path.insert(0, TEAMS_DIR)
_argv, sys.argv = sys.argv, [sys.argv[0]]        # teams_pull/outlook_pull resolvem perfil no import e leem sys.argv
try:
    import teams_pull as tp, outlook_pull as op
finally:
    sys.argv = _argv


def dt(iso):
    if not iso: return None
    iso = iso.replace('Z', '+00:00')
    iso = re.sub(r'\.(\d{1,9})(?=[+-]|$)', lambda m: '.' + m.group(1)[:6].ljust(6, '0'), iso)
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hora(iso):
    d = dt(iso); return d.astimezone(BRT).strftime('%d/%m %H:%M') if d else '??'


def parse_since(s):
    from teams_feed import parse_since as ps
    d = ps(s)
    return d if d.tzinfo else d.astimezone()


_CAL = None
def cal_id():
    """Id da tabela 'Calendar' (ou 'Calendário'); ignora Birthdays/feriados. Uma chamada por processo."""
    global _CAL
    if _CAL: return _CAL
    tabs = op.get('/datasets/calendars/tables').get('value', [])
    esc = next((t for t in tabs if (t.get('DisplayName') or '').lower() in ('calendar', 'calendário', 'calendario')), None)
    if not esc: esc = next((t for t in tabs if not re.search(r'birthday|anivers|holiday|feriado', t.get('DisplayName') or '', re.I)), None)
    if not esc: sys.exit(f'nenhum calendario na conexao Office 365 do perfil {PERFIL}')
    _CAL = esc['Name']; return _CAL


def _q(iso):
    return dt(iso).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if isinstance(iso, str) else iso.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


TEAMS_LINK = re.compile(r'https://teams\.microsoft\.com/(?:meet|l/meetup-join)/[^\s"<>]+')
CANCELADO = re.compile(r'^\s*(canceled|cancelled|cancelad[ao])\s*:', re.I)
RESP_MAP = {'notResponded': 'needsAction', 'accepted': 'accepted', 'declined': 'declined', 'tentativelyAccepted': 'tentative',
            'organizer': 'organizador', 'none': None}


def norm(e):
    conta = (tp.cfg().get('conta') or '').lower()
    org = (e.get('organizer') or '').strip()
    ini = e.get('startWithTimeZone') or e.get('start')
    fim = e.get('endWithTimeZone') or e.get('end')
    if e.get('timeZone') and not e.get('startWithTimeZone') and e['timeZone'] != 'UTC':
        ini = fim = None                       # sem offset e fuso nao-UTC: nao chutar a hora
    body = e.get('body') or ''
    m = TEAMS_LINK.search(body)
    conv = [x.strip() for x in (e.get('requiredAttendees') or '').split(';') if x.strip()]
    opc = [x.strip() for x in (e.get('optionalAttendees') or '').split(';') if x.strip()]
    return {'id': e['id'], 'serie': e.get('seriesMasterId'), 'titulo': e.get('subject') or '(sem titulo)',
            'cancelado_no_titulo': bool(CANCELADO.match(e.get('subject') or '')),
            'ini': dt(ini).isoformat() if ini else None, 'fim': dt(fim).isoformat() if fim else None,
            'dia_inteiro': bool(e.get('isAllDay')), 'criado': e.get('createdDateTime'), 'atualizado': e.get('lastModifiedDateTime'),
            'organizador': org, 'org_eu': bool(conta) and org.lower() == conta,
            'minha_resposta': RESP_MAP.get(e.get('responseType'), e.get('responseType')),
            'convidados': [c for c in conv + opc if c.lower() != conta], 'opcionais': [c for c in opc if c.lower() != conta],
            'meet': m.group(0) if m else None, 'local': e.get('location'), 'descricao': body if e.get('isHtml') is False else op.limpa(body),
            'link': e.get('webLink'), 'recorrente': bool(e.get('recurrence') and e['recurrence'] != 'none'),
            'recorrencia': e.get('recurrence'), 'showAs': e.get('showAs'), 'importancia': e.get('importance')}


def _get_retry(path, tent=3):
    """op.get com nova tentativa em URLError ('Connection refused' transitorio do NAT do WSL, visto em 18/09/2026)."""
    import time, urllib.error
    for i in range(tent):
        try: return op.get(path)
        except urllib.error.URLError:
            if i == tent - 1: raise
            time.sleep(2 * (i + 1))


def janela(ini, fim):
    """Instancias entre ini e fim (datetimes aware), paginadas; ordem por inicio (o conector manda o corpo junto)."""
    out, skip = [], 0
    while True:
        q = urllib.parse.urlencode({'calendarId': cal_id(), 'startDateTimeUtc': _q(ini), 'endDateTimeUtc': _q(fim), '$top': PAGINA, '$skip': skip})
        pag = _get_retry('/datasets/calendars/v3/tables/items/calendarview?' + q).get('value', [])
        out += [norm(e) for e in pag]
        if len(pag) < PAGINA or skip >= 2000: break
        skip += PAGINA
    return sorted(out, key=lambda e: e['ini'] or '')


def proximos(ate, agora=None):
    agora = agora or datetime.now(timezone.utc)
    return janela(agora, ate)


def evento(eid):
    """Um evento por id (instancia ou serie). SystemExit com 'HTTP 400/404' quando nao existe mais."""
    return norm(op.get(f"/datasets/calendars/v3/tables/{urllib.parse.quote(cal_id(), safe='')}/items/{urllib.parse.quote(eid, safe='')}"))


def estado(ids):
    """Para resolver pendencias: {id: 'needsAction'|'accepted'|'declined'|'tentative'|'cancelado'|'apagado'|'passou'}."""
    est = {}
    agora = datetime.now(timezone.utc)
    for eid in ids:
        try:
            e = evento(eid)
        except SystemExit as ex:
            if re.search(r'HTTP (400|404)', str(ex.code)): est[eid] = 'apagado'
            continue
        if e['cancelado_no_titulo']: est[eid] = 'cancelado'
        elif e['ini'] and dt(e['ini']) < agora: est[eid] = 'passou'
        else: est[eid] = e['minha_resposta'] or 'needsAction'
    return est


RESP = {'needsAction': 'sem resposta', 'accepted': 'aceito', 'declined': 'recusado', 'tentative': 'talvez', 'organizador': 'organizador'}
DIAS = {0: 'seg', 1: 'ter', 2: 'qua', 3: 'qui', 4: 'sex', 5: 'sab', 6: 'dom'}


def faixa(e):
    if not e['ini']: return '??'
    a = dt(e['ini']).astimezone(BRT); b = dt(e['fim']).astimezone(BRT) if e['fim'] else None
    if e['dia_inteiro']:
        b2 = b - timedelta(days=1) if b else None
        return a.strftime('%d/%m') + (f'–{b2.strftime("%d/%m")}' if b2 and b2.date() != a.date() else '') + ' (dia inteiro)'
    return f'{DIAS[a.weekday()]} {a.strftime("%d/%m %H:%M")}' + (f'–{b.strftime("%H:%M")}' if b else '')


def quem(email):
    return email.split('@')[0].replace('ext.', '') if email else '?'


def linha(e, tag=''):
    q = '' if e['org_eu'] else f' · de {quem(e["organizador"])}'
    resp = f' · {RESP.get(e["minha_resposta"], e["minha_resposta"])}' if e['minha_resposta'] and not e['org_eu'] else ''
    rec = f' · {e["recorrencia"]}' if e['recorrente'] and e['recorrencia'] else ''
    return f'-- {tag}{e["titulo"]} · {faixa(e)}{q}{resp}{rec}'


def imprime_evento(e):
    print(linha(e))
    if e['local'] and e['local'] != 'Reuniões do Microsoft Teams': print(f'   local: {e["local"]}')
    if e['meet']: print(f'   teams: {e["meet"]}')
    if e['convidados']: print('   convidados: ' + ', '.join(quem(c) + (' (opc)' if c in e['opcionais'] else '') for c in e['convidados']))
    if e['descricao']:
        d = re.sub(r'(_{10,}|aviso legal:|this message).*', '', e['descricao'], flags=re.S | re.I).strip()   # rodape do Teams / disclaimer
        if d: print('   ' + d[:1500].replace('\n', '\n   '))
    print(f'   id: {e["id"]}' + (f' · {e["link"]}' if e['link'] else ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--next', '--proximos', dest='proximos', nargs='?', const='7d', metavar='7d')
    ap.add_argument('--event', '--evento', dest='evento', metavar='id'); ap.add_argument('--json', action='store_true')
    ap.add_argument('--all', '--todos', dest='todos', action='store_true', help='inclui os blocos seus de OOF/livre, que a agenda esconde')
    a = ap.parse_args()
    if a.evento:
        e = evento(a.evento)
        print(json.dumps(e, ensure_ascii=False, indent=1)) if a.json else imprime_evento(e); return
    n = re.fullmatch(r'(\d+)d', a.proximos or '7d')
    ate = datetime.now(timezone.utc) + timedelta(days=int(n.group(1)) if n else 7)
    evs = proximos(ate)
    if not a.todos: evs = [e for e in evs if not (e['org_eu'] and e['showAs'] in ('oof', 'free'))]
    if a.json: print(json.dumps(evs, ensure_ascii=False, indent=1)); return
    print(f'{len(evs)} eventos ate {ate.astimezone(BRT).strftime("%d/%m")}')
    for e in evs: print(linha(e) + (f'\n   {e["meet"]}' if e['meet'] else ''))


if __name__ == '__main__':
    main()
