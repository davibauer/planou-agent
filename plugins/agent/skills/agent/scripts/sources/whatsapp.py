#!/usr/bin/env python3
"""Source whatsapp: messages of the allowed WhatsApp groups and contacts, READ ONLY, through a linked device.

The instance joins the person's account as a linked device (like WhatsApp Web, QR scanned on the phone) with a small
Go bridge on whatsmeow (whatsapp_bridge/, pinned by commit in its go.mod/go.sum). There is no send path at all: no
message, reaction, status, typing state, read receipt or presence; the device never shows as online. The bridge
connects once per tick, receives what the server queued since the last connection, writes the allowed messages and
leaves: no daemon, no service, no resident process. It is an unofficial client and WhatsApp may block an account that
uses one; read only and short connections keep that risk low, never at zero.

Allowlist: only the chats listed in the instance config are read; everything else is dropped by the bridge before it
writes anything (and checked again here). An empty list reads nothing and says what is missing. The real names live
only in the instance config, never in this repository.

Files of the instance:
  secrets/whatsapp/        the device session (session.db, 0600; folder 0700). Never printed nor logged
  data/whatsapp/spool.jsonl  the allowed messages, one JSON line each (0600), written BEFORE the server gets the ack, so
                           a dry tick (test mode) or a crash loses nothing: the cursors here only say what was shown
  cache/whatsapp/anexos/   attachments of the allowed chats (up to max_anexo_mb)
  cache/whatsapp/bin/      the bridge binary, built once per bridge version with `go build` (Go and gcc needed)
  cache/whatsapp/qr.png    the QR while pairing (removed after)

Pairing (once, by the person): `agent.py <instance> --whatsapp-pair` shows the QR in the terminal and saves the PNG;
the history of the allowed chats comes once, at pairing, so fill the list first (the command refuses an empty one).

Config ("sources": [{"type": "whatsapp", ...}]):
  chats          groups: the group id (...@g.us, recommended) or the exact subject as the phone shows it (found once,
                 then only that group's id counts). Any chat id also works here
  contatos       1:1 chats: exact name as saved in the phone's address book (never the sender's own profile name),
                 phone number with country code (digits; spaces, + and dashes ignored)
                 or the id (...@s.whatsapp.net / ...@lid)
  anexos         download attachments of the allowed chats (default true)
  max_anexo_mb   bigger attachments are listed, not downloaded (default 16)
  timeout_s      most a tick's connection lasts (default 60); what was not acked comes on the next one
  historico_dias days of history asked at pairing (default 30)
  guardar_dias   spool lines older than this are pruned on live ticks (default 90)
  max_por_chat   messages shown per chat (default 20)
"""
import fcntl, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Fonte as Base

PONTE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'whatsapp_bridge')
PONTE_ARQS = ('main.go', 'allow.go', 'go.mod', 'go.sum')
BUILD_TIMEOUT = 600
CFG = {}
MAX_POR_CHAT = 20


def lista(v):
    """A config list of names/ids: a bare string is one entry; blanks are dropped."""
    if isinstance(v, str): v = [v]
    return [x.strip() for x in v or [] if isinstance(x, str) and x.strip()]


def opcoes(cfg):
    """What the bridge gets on stdin (never argv: no names in a process listing)."""
    return {'chats': lista(cfg.get('chats')), 'contatos': lista(cfg.get('contatos')),
            'media': bool(cfg.get('anexos', True)),
            'max_media_bytes': int(float(cfg.get('max_anexo_mb', 16)) * 1024 * 1024),
            'history_days': int(cfg.get('historico_dias', 30))}


def locais():
    return {'sessao': os.path.join(paths.SECRETS_DIR, 'whatsapp'),
            'spool': os.path.join(paths.DATA_DIR, 'whatsapp', 'spool.jsonl'),
            'anexos': os.path.join(paths.CACHE_DIR, 'whatsapp', 'anexos'),
            'qr': os.path.join(paths.CACHE_DIR, 'whatsapp', 'qr.png'),
            'bin': os.path.join(paths.CACHE_DIR, 'whatsapp', 'bin')}


def _pasta(p):
    """A private folder (0700), made on the way."""
    os.makedirs(p, mode=0o700, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def prepara(loc):
    _pasta(paths.SECRETS_DIR); _pasta(loc['sessao']); _pasta(os.path.dirname(loc['spool']))
    _pasta(os.path.dirname(loc['bin'])); _pasta(loc['anexos']); _pasta(loc['bin'])


def _digitos(s): return re.sub(r'\D', '', s or '')


def _telefone(s): return bool(re.fullmatch(r'\+?[0-9 ()\-.]{8,}', s or ''))


def permitido(r, op):
    """The second check of the allowlist, on a spool line (the bridge already dropped the rest): a group by its id or
    exact subject in chats; a 1:1 chat by its id, phone number or exact name in contatos (or its id in chats)."""
    chats, contatos = set(op['chats']), op['contatos']
    if r.get('grupo'):
        return r.get('chat') in chats or (bool(r.get('chat_nome')) and r.get('chat_nome') in chats)
    chat = r.get('chat') or ''
    if not chat.endswith(('@s.whatsapp.net', '@lid')): return False
    ids = {chat, r.get('chat_pn') or ''} - {''}
    if ids & (chats | {c for c in contatos if '@' in c}): return True
    fones = {_digitos(c) for c in contatos if '@' not in c and _telefone(c)}
    if (r.get('chat_pn') or '').split('@')[0] in fones: return True
    nomes = {c for c in contatos if '@' not in c and not _telefone(c)}
    return (r.get('chat_nome') or '').strip() in nomes


def versao_ponte():
    h = hashlib.sha256()
    for a in PONTE_ARQS:
        with open(os.path.join(PONTE_DIR, a), 'rb') as f: h.update(a.encode() + b'\0' + f.read())
    return h.hexdigest()[:12]


def binario(loc, timeout=BUILD_TIMEOUT, avisa=None):
    """The bridge for this version of its code, built once into cache/ (go build -mod=readonly: only the pinned
    versions of go.sum). Old builds are removed."""
    alvo = os.path.join(loc['bin'], f'whatsapp-bridge-{versao_ponte()}')
    if os.path.isfile(alvo) and os.access(alvo, os.X_OK): return alvo
    go = shutil.which('go')
    if not go: raise SystemExit('Go nao instalado (a ponte do WhatsApp compila com go build; precisa de Go e gcc)')
    _pasta(loc['bin'])
    if avisa: avisa('compilando a ponte do WhatsApp (so na primeira vez de cada versao; pode levar uns minutos)...')
    fd, tmp = tempfile.mkstemp(dir=loc['bin'], prefix='.build-')
    os.close(fd)
    cmd = (['nice', '-n', '10'] if shutil.which('nice') else []) + [go, 'build', '-mod=readonly', '-trimpath', '-o', tmp, '.']
    try:
        try:
            p = subprocess.run(cmd, cwd=PONTE_DIR, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, 'CGO_ENABLED': '1'})
        except subprocess.TimeoutExpired:
            raise SystemExit(f'compilacao da ponte passou de {timeout}s; rode --whatsapp-pair para compilar sem pressa')
        if p.returncode != 0 or not os.path.getsize(tmp):
            ult = [l for l in (p.stderr or '').splitlines() if l.strip()][-1:] or ['sem saida']
            raise SystemExit(f'compilacao da ponte falhou: {ult[0][:150]}')
        os.chmod(tmp, 0o700)
        os.replace(tmp, alvo)
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    for velho in glob.glob(os.path.join(loc['bin'], 'whatsapp-bridge-*')):
        if velho != alvo:
            try: os.remove(velho)
            except OSError: pass
    return alvo


def roda_ponte(cmd, loc, op, timeout, extra=(), terminal=False):
    """Runs the bridge once: options on stdin, one JSON object back on stdout. terminal=True leaves stderr on the
    terminal (the QR while pairing); otherwise stderr is dropped (it may name chats). Returns (exit code, result)."""
    # a tick builds within the source budget (half the tick ceiling); the pairing, run by hand, takes its time
    exe = binario(loc, timeout=BUILD_TIMEOUT if terminal else 240, avisa=(lambda m: print(m, file=sys.stderr)) if terminal else None)
    args = [exe, cmd, '-session', loc['sessao'], '-spool', loc['spool'], '-media', loc['anexos'] if op['media'] else '',
            '-timeout', f'{int(timeout)}s', *extra]
    try:
        p = subprocess.run(args, input=json.dumps(op), stdout=subprocess.PIPE, stderr=None if terminal else subprocess.DEVNULL,
                           text=True, timeout=timeout + 30 if cmd == 'pull' else None)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'ponte sem resposta em {int(timeout) + 30}s')
    linhas = [l for l in (p.stdout or '').splitlines() if l.strip().startswith('{')]
    try: r = json.loads(linhas[-1]) if linhas else {}
    except ValueError: r = {}
    if not r: r = {'ok': False, 'erro': f'ponte saiu com codigo {p.returncode} sem resultado'}
    return p.returncode, r


def puxa(op, loc, timeout):
    """One pull. Errors become a short SystemExit (FONTE QUEBRADA (whatsapp): ...), never the bridge's stderr."""
    prepara(loc)
    cod, r = roda_ponte('pull', loc, op, timeout)
    if cod == 0 and r.get('ok'): return r
    erro = str(r.get('erro') or f'codigo {cod}')[:200]
    if cod == 3: raise SystemExit(f'nao pareado: rode agent.py {paths.NAME} --whatsapp-pair')
    if cod == 4: raise SystemExit(f'{erro} (agent.py {paths.NAME} --whatsapp-pair)')
    raise SystemExit(erro)


def le_spool(arq):
    out = []
    try:
        with open(arq, encoding='utf-8') as f:
            for l in f:
                try: out.append(json.loads(l))
                except ValueError: pass
    except FileNotFoundError: pass
    return out


def unicos(regs):
    """The first copy of each (chat, id): a message delivered again (history and tick, or no ack before a drop) keeps
    the place of its first write, so the cursor never shows it twice."""
    vistos, out = set(), []
    for r in regs:
        k = (r.get('chat'), r.get('id'))
        if k in vistos: continue
        vistos.add(k); out.append(r)
    return out


def poda(loc, op, agora, dias):
    """Live ticks: drop spool lines older than `dias` and lines of chats no longer allowed (same lock as the bridge).
    The lock is tried, never waited for: with a pairing or another pull holding it, the pruning waits for the next tick."""
    arq = loc['spool']
    if not os.path.exists(arq): return 0
    limite = agora - timedelta(days=dias)
    with open(os.path.join(loc['sessao'], 'session.lock'), 'a') as trava:
        try: fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 0
        regs = le_spool(arq)
        fica = [r for r in unicos(regs) if permitido(r, op) and (core.dt(r.get('quando')) or agora) >= limite]
        if len(fica) == len(regs): return 0
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(arq), prefix='.spool-')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for r in fica: f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, arq)
    return len(regs) - len(fica)


def _hora(v):
    """A cursor or a `recebido` as a datetime (the bridge writes Go's format, the cursor may be Python's isoformat):
    compared as dates, never as text. Empty or broken values are the oldest."""
    try: return core.dt(v) or datetime.min.replace(tzinfo=timezone.utc)
    except ValueError: return datetime.min.replace(tzinfo=timezone.utc)


def tick_whatsapp(s, desde, dry):
    op = opcoes(CFG)
    if not op['chats'] and not op['contatos']:
        return {'vazia': True, 'chats': []}
    loc = locais()
    if not os.path.isfile(os.path.join(loc['sessao'], 'session.db')):
        raise SystemExit(f'nao pareado: rode agent.py {paths.NAME} --whatsapp-pair')
    resumo = puxa(op, loc, float(CFG.get('timeout_s', 60)))
    agora = datetime.now(timezone.utc)
    st = s.setdefault('whatsapp', {})
    regs = [r for r in unicos(le_spool(loc['spool'])) if permitido(r, op)]
    avisos = list(resumo.get('avisos') or [])
    if resumo.get('sem_par'):
        avisos.append('na lista mas nao achados na conta (confira o nome exato): ' + ', '.join(resumo['sem_par']))
    if desde is None and not st.get('plantado'):        # no pairing mark in the state: plant the cursor now
        if not dry: st['plantado'] = agora.isoformat()
        return {'baseline': True, 'guardadas': len(regs), 'spool': loc['spool'], 'chats': [], 'avisos': avisos}
    cur = st.get('cursores', {}) if dry else st.setdefault('cursores', {})
    por_chat = {}
    for r in regs:
        if desde is not None: novo = (core.dt(r.get('quando')) or agora) >= desde
        else: novo = _hora(r.get('recebido')) > _hora(cur.get(r['chat'], st['plantado']))
        if novo: por_chat.setdefault(r['chat'], []).append(r)
    out = []
    for chat, ms in por_chat.items():
        unicas = sorted(ms, key=lambda r: r.get('quando') or '')
        if not any(not m.get('eu') for m in unicas): continue
        nome = next((m.get('chat_nome') for m in reversed(unicas) if m.get('chat_nome')), chat)
        msgs = [{'quando': m.get('quando'), 'de': m.get('de_nome') or m.get('de') or '?', 'eu': bool(m.get('eu')),
                 'texto': m.get('texto') or '', 'anexo': m.get('anexo')} for m in unicas]
        out.append({'chat': nome, 'id': chat, 'grupo': bool(unicas[-1].get('grupo')), 'respondido': msgs[-1]['eu'],
                    'msgs': msgs[-MAX_POR_CHAT:], 'omitidas': max(0, len(msgs) - MAX_POR_CHAT),
                    'cursor': max((m.get('recebido', '') for m in ms), key=_hora)})
    out.sort(key=lambda c: (c['respondido'], c['grupo'], c['msgs'][-1]['quando'] or ''))
    if not dry and desde is None:
        for c in out: cur[c['id']] = c['cursor']
        for r in regs:                                   # chats with only my messages: move on too
            if _hora(r.get('recebido')) > _hora(cur.get(r['chat'], st['plantado'])): cur[r['chat']] = r['recebido']
        podadas = poda(loc, op, agora, float(CFG.get('guardar_dias', 90)))
        if podadas: st['podadas'] = st.get('podadas', 0) + podadas
    return {'baseline': False, 'chats': out, 'avisos': avisos, 'resolvidas': []}


def _anexo(a):
    if not a: return ''
    if a.get('caminho'): return f'\n      [guardado: {a["caminho"]}]'
    nome = a.get('nome') or a.get('tipo') or 'anexo'
    mb = f'{(a.get("bytes") or 0) / 1048576:.1f} MB'
    return f'\n      [anexo nao baixado: {nome} ({mb}): {a.get("erro") or "sem motivo"}]'


def imprime_whatsapp(r):
    hora = core.hora
    if r.get('vazia'):
        print(f'AVISO (whatsapp): lista de permissao vazia, nada lido. Liste os grupos em "chats" e as pessoas em '
              f'"contatos" na fonte whatsapp do config da instancia (nome exato como no celular, numero ou id) e pareie '
              f'com agent.py {paths.NAME} --whatsapp-pair.')
        return True
    for a in r.get('avisos') or []: print(f'AVISO (whatsapp): {a}')
    if r.get('baseline'):
        print(f'== WHATSAPP: cursor plantado agora; {r["guardadas"]} msgs ja guardadas em {r["spool"]} '
              '(historico do pareamento); a proxima rodada mostra o que chegar depois.')
        return True
    if not r['chats']: return bool(r.get('avisos'))
    print(f'== WHATSAPP ({sum(len(c["msgs"]) for c in r["chats"])} msgs novas em {len(r["chats"])} chats)')
    for c in r['chats']:
        estado = 'ultima msg e sua' if c['respondido'] else 'SEM RESPOSTA SUA'
        print(f'-- {c["chat"]} ({"grupo" if c["grupo"] else "1:1"}) · {estado}')
        if c['omitidas']: print(f'   (… {c["omitidas"]} anteriores omitidas)')
        for m in c['msgs']:
            quem = 'eu' if m['eu'] else m['de']
            print(f'   [{hora(m["quando"])}] {quem}: {m["texto"]}{_anexo(m.get("anexo"))}')
    return True


def pareia(s):
    """--whatsapp-pair: the one pairing, run by the person in a terminal. Refuses an empty list: the history of the
    allowed chats comes only now."""
    op = opcoes(CFG)
    if not op['chats'] and not op['contatos']:
        sys.exit('whatsapp: lista de permissao vazia. Antes de parear, liste os grupos em "chats" e as pessoas em '
                 '"contatos" na fonte whatsapp do config da instancia (nome exato como aparece no celular, numero ou id): '
                 'o historico dos chats permitidos so vem uma vez, no pareamento.')
    loc = locais()
    prepara(loc)
    print(f'Fonte whatsapp, somente leitura. O QR aparece abaixo e tambem em {loc["qr"]}.', file=sys.stderr)
    cod, r = roda_ponte('pair', loc, op, 180, extra=('-qr-png', loc['qr'], '-history-wait', '180s'), terminal=True)
    if cod != 0 or not r.get('ok'):
        sys.exit(f'whatsapp: {r.get("erro") or f"codigo {cod}"}')
    st = s.setdefault('whatsapp', {})
    st['plantado'] = st['pareado'] = datetime.now(timezone.utc).isoformat()
    core.save_state(s)
    print(f'Pareado. Historico guardado: {r.get("historico", 0)} msgs dos chats permitidos, {r.get("anexos", 0)} anexos; '
          f'{r.get("descartadas", 0)} msgs de outros chats descartadas sem gravar.')
    if r.get('sem_par'):
        print('Na lista mas nao achados na conta (confira o nome exato): ' + ', '.join(r['sem_par']))
    for a in r.get('avisos') or []: print(f'Aviso: {a}')
    if r.get('grupos'): print('Grupos da conta, nome (id) (so nesta tela, nada gravado): ' + ' | '.join(sorted(r['grupos'])))
    print(f'Daqui em diante cada tick conecta, le o que chegou e desconecta (agent.py {paths.NAME} --so whatsapp).')


class Fonte(Base):
    tipo = 'whatsapp'
    lembrar = {}
    conserto = 'WhatsApp fora: se a sessao caiu, pareie de novo (agent.py <instancia> --whatsapp-pair)'

    def configura(self):
        global CFG, MAX_POR_CHAT
        CFG = dict(self.cfg)
        MAX_POR_CHAT = int(self.cfg.get('max_por_chat', 20))

    def tick(self, s, desde, dry): return tick_whatsapp(s, desde, dry)
    def imprime(self, r): return imprime_whatsapp(r)
    def linha_ref(self, p): return None

    def args(self, ap):
        if '--whatsapp-pair' not in ap._option_string_actions:
            ap.add_argument('--whatsapp-pair', action='store_true',
                            help='pareia a fonte whatsapp (QR no terminal e em cache/whatsapp/qr.png); somente leitura')

    def cli(self, a, ctx):
        if not getattr(a, 'whatsapp_pair', False): return False
        pareia(ctx.s)
        return True
