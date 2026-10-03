#!/usr/bin/env python3
"""Core of an agent instance: state file, pending queue (pendencias), business hours and reminders.

Taken from work-watch's core.py with the same function names and the same Portuguese output strings (the adapters and
the triage rules key on them), so the adapters move over unchanged. What comes from the instance config (schema.py,
already normalized) through `configura()`:

  STATE          <root>/data/state.json (paths.py)
  BRT            instance timezone ("tz_hours", default -3)
  COMERCIAL      business hours ("business_hours", default [8, 19]), Mon-Fri
  LEMBRAR        hours of business time until the 1st reminder, per queue name (from each source's "lembrar_horas")
  MAX_LEMBRETES  reminders per pending item ("max_reminders", default 2)

Guard: an instance whose config does not say "live": true is in TEST mode. Every tick is dry and hooks with external
effects refuse to run (`exige_live`). State is only ever written inside the instance folder.
"""
import os, re, json
from datetime import datetime, timezone, timedelta

import paths
from watch_core import fileio

STATE = None                 # set by configura()
BRT = timezone(timedelta(hours=-3))
COMERCIAL = (8, 19)
LEMBRAR = {}
MAX_LEMBRETES = 2
LIVE = False


class Teste(SystemExit):
    """Raised when something would leave the instance folder while "live" is not true (test mode)."""


Shadow = Teste               # work-watch's name up to 0.7.0; old instance adapters still catch core.Shadow


def configura(cfg, lembrar=None):
    global STATE, BRT, COMERCIAL, LEMBRAR, MAX_LEMBRETES, LIVE
    STATE = paths.STATE
    LIVE = cfg.get('live') is True
    BRT = timezone(timedelta(hours=float(cfg.get('tz_hours', -3))))
    COMERCIAL = tuple(cfg.get('business_hours') or (8, 19))
    MAX_LEMBRETES = int(cfg.get('max_reminders', 2))
    LEMBRAR = dict(lembrar or {})


def exige_live(efeito):
    """Call before any side effect outside the instance folder (Slack status, Notion, transcription, shared files)."""
    if not LIVE: raise Teste(f'modo teste: "{efeito}" bloqueado (a instancia {paths.NAME} nao tem "live": true)')


# ---------------- estado ----------------
_BASE = {}                   # id(state) -> (state, its actions as they were on disk when it was read); see save_state


def _acoes_json(s):
    return {a['id']: json.dumps(a, sort_keys=True) for a in (s.get('acoes') or []) if isinstance(a, dict) and 'id' in a}


def load_state():
    try:
        with open(STATE) as f: s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): s = {}
    while len(_BASE) >= 8: _BASE.pop(next(iter(_BASE)))
    _BASE[id(s)] = (s, _acoes_json(s))      # the reference keeps the id from being reused
    return s


def state_lock():
    """Exclusive, cross-process and reentrant lock of the state file: hold it for a read, change, save (PLN0256)."""
    return fileio.locked(STATE)


def _merge_acoes(s, disk, base):
    """The runner holds its copy of the state for the whole tick; the CLI (`--acao add/done`) writes the file meanwhile.
    Before saving that copy, take from the file every action someone else added or changed since the copy was read and
    this copy did not touch. Both changed: an action closed by either side stays closed. Never loses a --acao done."""
    mine = {a['id']: a for a in (s.get('acoes') or []) if isinstance(a, dict) and 'id' in a}
    out = list(s.get('acoes') or [])
    for a in disk.get('acoes') or []:
        if not isinstance(a, dict) or 'id' not in a: continue
        j = json.dumps(a, sort_keys=True)
        if a['id'] not in mine:
            if a['id'] not in base: out.append(a)            # added by someone else (one this copy dropped stays dropped)
        elif j != base.get(a['id']):                          # changed by someone else
            cur = mine[a['id']]
            if json.dumps(cur, sort_keys=True) == base.get(a['id']) or (a.get('status') == 'feita' and cur.get('status') != 'feita'):
                out[out.index(cur)] = a
    s['acoes'] = out
    s['acao_seq'] = max(s.get('acao_seq', 0), disk.get('acao_seq', 0), max([a['id'] for a in out if isinstance(a.get('id'), int)] or [0]))


def save_state(s):
    if not paths.dentro(STATE): raise Teste(f'estado fora da pasta da instancia: {STATE}')
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    ent = _BASE.get(id(s))
    base = ent[1] if ent and ent[0] is s else None
    if base is None:                                         # a state that was never read from the file: plain write
        fileio.write_json(STATE, s, indent=1, ensure_ascii=False); return
    with state_lock():
        try:
            with open(STATE) as f: disk = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): disk = {}
        if isinstance(disk, dict) and disk.get('acoes') and _acoes_json(disk) != base: _merge_acoes(s, disk, base)
        fileio.write_json(STATE, s, indent=1, ensure_ascii=False)
        _BASE[id(s)] = (s, _acoes_json(s))

def dt(iso):
    """ISO (GitHub/Jira/Slack) -> datetime aware (tolera fracao longa e offset sem dois-pontos)."""
    if not iso: return None
    iso = iso.replace('Z', '+00:00')
    iso = re.sub(r'\.(\d{1,9})(?=[+-]|$)', lambda m: '.' + m.group(1)[:6].ljust(6, '0'), iso)
    iso = re.sub(r'([+-]\d\d)(\d\d)$', r'\1:\2', iso)
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def hora(iso):
    d = dt(iso); return d.astimezone(BRT).strftime('%d/%m %H:%M') if d else '??'

def parse_since(s):
    """--since: 30m, 4h, 2d, 3du (dias uteis: hoje + 2 anteriores, desde 00:00 local) ou ISO."""
    m = re.fullmatch(r"(\d+)(du|bd)", s)
    if m:
        d, n = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0), int(m.group(1))
        while d.weekday() >= 5: d -= timedelta(days=1)
        for _ in range(n - 1):
            d -= timedelta(days=1)
            while d.weekday() >= 5: d -= timedelta(days=1)
        return d
    m = re.fullmatch(r"(\d+)([mhd])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[u]
        return datetime.now(timezone.utc) - delta
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

# ---------------- Pendencias ----------------
def comercial(d):
    d = d.astimezone(BRT); return d.weekday() < 5 and COMERCIAL[0] <= d.hour < COMERCIAL[1]

def horas_uteis(a, b):
    """Horas dentro do horario comercial entre a e b (passo de 15 min; pendencia nao envelhece a noite nem no fim de semana)."""
    if not a or not b or b <= a: return 0.0
    t, n, passo = a, 0, timedelta(minutes=15)
    while t < b:
        if comercial(t): n += 1
        t += passo
    return n / 4

def pendencias(s):
    return s.setdefault('pendencias', [])

def pend_abertas(s, fonte=None):
    return [p for p in pendencias(s) if p.get('status') == 'aberta' and (fonte is None or p['fonte'] == fonte)]

def enfileira(s, fonte, ref, quem, assunto, desde, agora):
    """Upsert por (fonte, ref). Aberta: mantem o 'desde' original. Resolvida/ignorada: reabre como nova."""
    for p in pendencias(s):
        if p['fonte'] == fonte and p['ref'] == ref:
            if p.get('status') == 'aberta': p['quem'], p['assunto'] = quem, assunto; return p
            p.update(status='aberta', quem=quem, assunto=assunto, desde=desde, lembretes=0, ultimo_lembrete=None, criada=agora.isoformat())
            return p
    s['pend_seq'] = s.get('pend_seq', 0) + 1
    p = {'chave': f'p{s["pend_seq"]}', 'fonte': fonte, 'ref': ref, 'quem': quem, 'assunto': assunto, 'desde': desde,
         'status': 'aberta', 'lembretes': 0, 'ultimo_lembrete': None, 'criada': agora.isoformat()}
    pendencias(s).append(p); return p

def resolve(p, agora, como):
    p['status'] = 'resolvida'; p['resolvida'] = agora.isoformat(); p['como'] = como

def lembretes_devidos(s, agora, fontes_ok):
    """Pendencias abertas cujo prazo (em horas uteis desde a criacao ou o ultimo lembrete) venceu; so em horario comercial."""
    if not comercial(agora): return []
    out = []
    for p in pend_abertas(s):
        # an item from an older state (or written by hand) may lack the reminder keys: never reminded, created when it began
        p.setdefault('lembretes', 0); p.setdefault('ultimo_lembrete', None)
        if p.get('fonte') not in fontes_ok or p.get('fonte') not in LEMBRAR or p['lembretes'] >= MAX_LEMBRETES: continue
        base = dt(p['ultimo_lembrete'] or p.get('criada') or p.get('desde'))
        if horas_uteis(base, agora) >= LEMBRAR[p['fonte']]: out.append(p)
    return out

def imprime_pendencias(s, lembrar, resolvidas):
    if not (lembrar or resolvidas): return False
    agora = datetime.now(timezone.utc)
    print('== PENDENTES')
    for p in lembrar:
        idade = horas_uteis(dt(p['desde']), agora)
        mr = f' · MR {p["mr"]}' if p.get('mr') else ''
        print(f'-- {p["chave"]} [{p["fonte"]}] {p["quem"]} — {p["assunto"]}{mr} · desde {hora(p["desde"])} ({idade:.0f} h uteis) · lembrete {p["lembretes"]}/{MAX_LEMBRETES}')
    for p, como in resolvidas:
        print(f'   ✓ {p["chave"]} [{p["fonte"]}] {p["quem"]} — {p["assunto"]} · resolvida ({como})')
    return True

def linha_ref_padrao(p):
    return f'   ref: {p["ref"]}'

def lista_pendencias(s, linha_ref=None):
    """linha_ref(p) -> linha ou None: cada fonte decide como mostra a referencia (padrao: '   ref: <ref>')."""
    linha_ref = linha_ref or linha_ref_padrao
    abertas = pend_abertas(s)
    if not abertas: print('sem pendencias abertas'); return
    agora = datetime.now(timezone.utc)
    print(f'{len(abertas)} pendencias abertas:')
    for p in sorted(abertas, key=lambda p: p['desde']):
        silen = ' · silenciosa (lembretes esgotados)' if p['lembretes'] >= MAX_LEMBRETES else ''
        nota = f' · nota: {p["nota"]}' if p.get('nota') else ''
        mr = f' · MR {p["mr"]}' if p.get('mr') else ''
        print(f'-- {p["chave"]} [{p["fonte"]}] {p["quem"]} — {p["assunto"]}{mr} · desde {hora(p["desde"])} ({horas_uteis(dt(p["desde"]), agora):.0f} h uteis) · lembretes {p["lembretes"]}{silen}{nota}')
        ln = linha_ref(p)
        if ln: print(ln)
