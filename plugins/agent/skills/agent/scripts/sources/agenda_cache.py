#!/usr/bin/env python3
"""Calendar as a local cache, for a calendar with no headless credential.

Typical case: the work calendar is shared with a personal account only as free/busy, so the Google Calendar connector
lists events WITHOUT title, attendees or link (only id, start, end, status). The tick does not fetch it: the model calls
the connector in the session (once a day, or when `precisa_atualizar` says the cache expired) and stores the answer:

    watch.py <instance> --agenda set '<connector JSON, or the list of events>'
    watch.py <instance> --agenda nomear <id> "Daily"      # give a block a title (free/busy has none)
    watch.py <instance> --agenda                         # blocks of the last/next 24 h and the cache age

As a library: `janela(ini, fim)` returns events as the recordings engine expects ({'id', 'titulo', 'ini', 'fim'},
ini/fim ISO in UTC); an empty title becomes "<titulo_padrao> HH:MM".

Config (inside the gravacoes source: "agenda": {"tipo": "cache", ...}):
  arquivo         cache file (default cache/agenda.json of the instance). A file outside the instance folder is read
                  but only written when the instance is live
  titulo_padrao   prefix of the default title (default "Reuniao")
  validade_horas  older than this -> expired (default 12)
  cobertura_horas the cache must reach at least now + this (default 8)
"""
import os, json
from datetime import datetime, timezone, timedelta

import core, paths
from watch_core import fileio, slowfs

CACHE = None
TITULO = 'Reuniao'
VALIDADE = timedelta(hours=12)
COBERTURA = timedelta(hours=8)


def configura(cfg):
    global CACHE, TITULO, VALIDADE, COBERTURA
    CACHE = paths.expande(cfg.get('arquivo') or 'cache/agenda.json')
    TITULO = cfg.get('titulo_padrao', 'Reuniao')
    VALIDADE = timedelta(hours=float(cfg.get('validade_horas', 12)))
    COBERTURA = timedelta(hours=float(cfg.get('cobertura_horas', 8)))


def _carrega():
    try:
        return slowfs.load_json(CACHE)            # with a deadline on a Windows drive (PLN0245)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'atualizada': None, 'ate': None, 'blocos': []}


def _grava(d):
    if not paths.dentro(CACHE): core.exige_live(f'gravar a agenda em {CACHE}')
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    fileio.write_json(CACHE, d, ensure_ascii=False, indent=1)


def _iso(x):
    """{'dateTime': ...} ou {'date': ...} do Google -> ISO em UTC; dia inteiro devolve None (nao e' reuniao)."""
    if not isinstance(x, dict): return None
    s = x.get('dateTime')
    if not s: return None
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if d.tzinfo is None: d = d.replace(tzinfo=core.BRT)
    return d.astimezone(timezone.utc).isoformat()


def normaliza(payload):
    """Resposta do conector (ou lista de eventos) -> [{'id', 'titulo', 'ini', 'fim'}], titulos preservados quando existem."""
    evs = payload.get('events') or payload.get('items') or [] if isinstance(payload, dict) else payload
    out = []
    for e in evs:
        if e.get('status') == 'cancelled': continue
        ini, fim = _iso(e.get('start')), _iso(e.get('end'))
        if not (ini and fim): continue          # dia inteiro: bloco, nao reuniao
        out.append({'id': e.get('id') or f'{ini}', 'titulo': (e.get('summary') or '').strip(), 'ini': ini, 'fim': fim})
    return sorted(out, key=lambda e: e['ini'])


def set_agenda(texto):
    """Grava o cache a partir do JSON do conector; mantem titulos ja' dados a mao (nomear) para o mesmo id."""
    payload = json.loads(texto)
    novos = normaliza(payload)
    antes = {b['id']: b.get('titulo') for b in _carrega()['blocos'] if b.get('titulo')}
    serie = {i.split('_')[0]: t for i, t in antes.items()}      # evento recorrente: um id por ocorrencia, mesmo prefixo
    for b in novos:
        if not b['titulo']: b['titulo'] = antes.get(b['id']) or serie.get(b['id'].split('_')[0], '')
    ate = max((b['fim'] for b in novos), default=None)
    _grava({'atualizada': datetime.now(timezone.utc).isoformat(), 'ate': ate, 'blocos': novos})
    return novos


def nomear(bid, titulo):
    d = _carrega()
    achou = False
    for b in d['blocos']:
        if b['id'] == bid or b['id'].startswith(bid): b['titulo'] = titulo; achou = True
    if achou: _grava(d)
    return achou


def janela(ini, fim):
    """Blocos que cruzam [ini, fim] (datetimes com tz). Titulo vazio vira '<titulo_padrao> HH:MM'."""
    d = _carrega()
    out = []
    for b in d['blocos']:
        b_ini = datetime.fromisoformat(b['ini']); b_fim = datetime.fromisoformat(b['fim'])
        if b_fim < ini or b_ini > fim: continue
        e = dict(b)
        if not e['titulo']: e['titulo'] = f'{TITULO} ' + b_ini.astimezone(core.BRT).strftime('%H:%M')
        out.append(e)
    return out


def precisa_atualizar(agora=None):
    """True quando o modelo precisa buscar a agenda no conector: sem cache, velho demais ou curto demais."""
    agora = agora or datetime.now(timezone.utc)
    d = _carrega()
    if not d.get('atualizada'): return True
    if agora - datetime.fromisoformat(d['atualizada']) > VALIDADE: return True
    return not d.get('ate') or datetime.fromisoformat(d['ate']) < agora + COBERTURA


def cli(args, horas=24):
    """--agenda | --agenda set JSON | --agenda nomear ID TITULO | --agenda precisa"""
    if args and args[0] == 'set':
        b = set_agenda(' '.join(args[1:])); print(f'agenda gravada: {len(b)} bloco(s)' + (f', ate {b[-1]["fim"]}' if b else '')); return
    if args and args[0] == 'nomear':
        print('nomeado' if nomear(args[1], ' '.join(args[2:])) else 'id nao encontrado (ver --agenda)'); return
    if args and args[0] == 'precisa':
        p = precisa_atualizar(); print('precisa atualizar (o modelo busca no conector)' if p else 'cache em dia'); raise SystemExit(1 if p else 0)
    agora = datetime.now(timezone.utc)
    d = _carrega()
    print(f'cache: {d.get("atualizada") or "vazio"}' + (' · VENCIDO' if precisa_atualizar(agora) else ''))
    for e in janela(agora - timedelta(hours=horas), agora + timedelta(hours=horas)):
        i = datetime.fromisoformat(e['ini']).astimezone(core.BRT); f = datetime.fromisoformat(e['fim']).astimezone(core.BRT)
        print(f'-- {e["titulo"]} · {i.strftime("%a %d/%m %H:%M")}–{f.strftime("%H:%M")} · {e["id"][:24]}')
