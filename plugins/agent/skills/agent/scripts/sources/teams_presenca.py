#!/usr/bin/env python3
"""Source teams_presenca (result key 'presenca'): Teams preferred presence through Microsoft Graph
(setUserPreferredPresence), renewed every tick. Live instances only (it writes the user's presence and rotates the
Graph refresh token).

Rule: on a business day from "offline_inicio" until "offline_fim" of the next day (and all weekend) the preferred presence
is Offline/OffWork ("Appear offline"); the rest of the business day it is Busy/Busy. Both are renewed every tick
(expiration PT4H, the Graph maximum). A line only on the transition; if Teams overrides the preference (an OOF in the
calendar becomes Away/OutOfOffice) it is put back silently, with a warning after 3 ticks in a row. Lines start with '·'
(not '--') so they do not become daily evidence.

Credential: a refresh token of a public client consented by the user for Presence.ReadWrite (device code), JSON file
{"tenant", "client_id", "refresh_token"} rotated at each use.

Config ("fontes": [{"tipo": "teams_presenca", ...}]):
  credencial      path of that JSON file (required)
  offline_inicio  "HH:MM" (default "18:00")
  offline_fim     "HH:MM" (default "08:00")
  dica_refresh    hint appended when the refresh fails (default: redo the device code)
"""
import json, os, sys, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta, time as dtime

import core, paths
from watch_core import fileio, slowfs
from adapters import Fonte as Base

CRED = None
GRAPH = 'https://graph.microsoft.com/v1.0'
INICIO_OFFLINE = dtime(18, 0)
FIM_OFFLINE = dtime(8, 0)
EXPIRACAO = 'PT4H'
DICA = 'refazer o device code'


def _token():
    core.exige_live('Graph setUserPreferredPresence (refresh token rotativo)')
    c = slowfs.load_json(CRED)                   # with a deadline on a Windows drive (PLN0245)
    d = urllib.parse.urlencode({'grant_type': 'refresh_token', 'client_id': c['client_id'], 'refresh_token': c['refresh_token'],
                                'scope': 'Presence.ReadWrite offline_access openid'}).encode()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(f'https://login.microsoftonline.com/{c["tenant"]}/oauth2/v2.0/token', data=d), timeout=30).read())
    except urllib.error.HTTPError as e:
        b = e.read()[:300].decode(errors='replace')
        raise RuntimeError(f'refresh do token de presenca falhou ({e.code}): {b} — {DICA}')
    if r.get('refresh_token'):
        c['refresh_token'] = r['refresh_token']; c['renovado'] = datetime.now(core.BRT).isoformat()
        fileio.write_json(CRED, c, mode=0o600)
    return r['access_token']


def _graph(method, path, body=None, tok=None):
    tok = tok or _token()
    req = urllib.request.Request(GRAPH + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(); return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Graph {method} {path}: HTTP {e.code} {e.read()[:300].decode(errors="replace")}')


def atual(tok=None):
    p = _graph('GET', '/me/presence', tok=tok)
    return {'availability': p.get('availability'), 'activity': p.get('activity'), 'status': p.get('statusMessage')}


def offline(tok=None):
    _graph('POST', '/me/presence/setUserPreferredPresence', {'availability': 'Offline', 'activity': 'OffWork', 'expirationDuration': EXPIRACAO}, tok=tok)


def ocupado(tok=None):
    _graph('POST', '/me/presence/setUserPreferredPresence', {'availability': 'Busy', 'activity': 'Busy', 'expirationDuration': EXPIRACAO}, tok=tok)


def janela_offline(agora=None):
    """True quando o usuario deve aparecer offline: dia util apos o inicio ou antes do fim; fim de semana sempre."""
    agora = agora or datetime.now(core.BRT)
    if agora.weekday() >= 5: return True
    t = agora.time()
    return t >= INICIO_OFFLINE or t < FIM_OFFLINE


def tick_presenca(s, desde, dry):
    st = s.setdefault('presenca', {'modo': None, 'ultimo': None, 'repostas': 0})
    if dry or desde is not None: return {'linhas': []}
    agora = datetime.now(core.BRT); quer = 'offline' if janela_offline(agora) else 'busy'
    tok = _token(); antes = atual(tok); linhas = []
    alvo = 'Offline' if quer == 'offline' else 'Busy'
    (offline if quer == 'offline' else ocupado)(tok)      # renova (expira em 4 h)
    fim, ini = FIM_OFFLINE.strftime('%H:%M'), INICIO_OFFLINE.strftime('%H:%M')
    if st.get('modo') != quer:
        dep = atual(tok)
        rot = f'APPEAR OFFLINE ligado · renova a cada tick ate {fim}' if quer == 'offline' else f'BUSY ligado (horario comercial) · renova a cada tick ate {ini}'
        linhas.append(f'   · {rot} (estava {antes["availability"]}/{antes["activity"]}, agora {dep["availability"]}/{dep["activity"]})'); st['repostas'] = 0
    elif antes['availability'] != alvo and antes['activity'] != 'OutOfOffice':   # OOF da agenda vence a preferida: e' esperado, nao avisa
        st['repostas'] = st.get('repostas', 0) + 1
        if st['repostas'] == 3: linhas.append(f'   · AVISO: o Teams sobrescreve a presenca {alvo} ({antes["availability"]}/{antes["activity"]}) — repus 3 vezes seguidas')
    else: st['repostas'] = 0
    st['modo'] = quer; st['ultimo'] = agora.isoformat()
    return {'linhas': linhas}


def imprime_presenca(r):
    if not r['linhas']: return False
    print('== PRESENCA')
    for l in r['linhas']: print(l)
    return True


class Fonte(Base):
    tipo = 'teams_presenca'
    nome_padrao = 'presenca'
    lembrar = {}
    conserto = 'Teams presence — redo the device code consent (see the adapter docstring)'

    def configura(self):
        global CRED, INICIO_OFFLINE, FIM_OFFLINE, DICA
        CRED = paths.expande(self.cfg.get('credencial') or sys.exit('teams_presenca: "credencial" faltando no config.json'))
        h, m = (self.cfg.get('offline_inicio') or '18:00').split(':'); INICIO_OFFLINE = dtime(int(h), int(m))
        h, m = (self.cfg.get('offline_fim') or '08:00').split(':'); FIM_OFFLINE = dtime(int(h), int(m))
        DICA = self.cfg.get('dica_refresh') or DICA

    def tick(self, s, desde, dry): return tick_presenca(s, desde, dry)
    def imprime(self, r): return imprime_presenca(r)
