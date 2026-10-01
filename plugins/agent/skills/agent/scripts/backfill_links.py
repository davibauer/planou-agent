#!/usr/bin/env python3
"""Maintenance: links for pending items created before the sources kept the message (Slack and Teams).

  watch.py <instance> --backfill-links --dry     shows what it would link (nothing is written)
  watch.py <instance> --backfill-links           writes p['msg'] and p['link'] (the next tick sends the link to Planou)

Before 0.22 a pending item kept only the conversation (p['ref']) and the time of the message (p['desde']), so its task
opens the conversation, not the message. This looks the message up again, read only, and links it only when exactly one
message fits:
  same conversation   p['ref'] (Slack channel id, Teams chat id)
  same author         p['autor'] when the item has it; in a DM or 1:1 chat, the other person (p['quem']); never me
  time                within JANELA_S of p['desde'] (the sources store the message time there)
  similar text        p['assunto'] is the start of the message text (80 characters); with no text, the exact time
Two or more candidates, or none: the item stays as it is. Items that already have a link are skipped.

Slack: conversations.history in the time window (top-level messages) plus search.messages on that day (thread
replies, which the history does not list). Teams: the chat's messages, newest first, down to the window.
"""
import difflib, re, sys, unicodedata
from datetime import datetime, timedelta, timezone

import core, permalinks

JANELA_S = 300
SIMILAR = 0.8
PAGINAS_TEAMS = 40            # 50 messages per page: how far back a Teams chat is read
PAGINAS_BUSCA = 5


def _norm(t):
    t = unicodedata.normalize('NFKC', t or '').replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', t).strip().lower()


def parecido(assunto, texto):
    """p['assunto'] is texto[:80] with newlines as spaces: the normalized start must match (a little slack for
    mentions or markup that the source rendered differently)."""
    a, b = _norm(assunto), _norm(texto)
    if not a: return None
    if b.startswith(a): return True
    return difflib.SequenceMatcher(None, a, b[:len(a)]).ratio() >= SIMILAR


def escolhe(p, msgs, dm):
    """The single message that fits p, or None; and how many fitted. msgs: [{'ts', 'de', 'eu', 'texto', ...}]."""
    alvo = core.dt(p['desde']).timestamp()
    autor = p.get('autor') or (p.get('quem') if dm else None)
    cands, vistos = [], set()
    for m in msgs:
        if m['eu'] or abs(m['ts'] - alvo) > JANELA_S: continue
        if autor and _norm(m['de']) != _norm(autor): continue
        ok = parecido(p.get('assunto'), m['texto'])
        if ok is None: ok = abs(m['ts'] - alvo) < 1
        if not ok or m['chave'] in vistos: continue
        vistos.add(m['chave']); cands.append(m)
    return (cands[0] if len(cands) == 1 else None), len(cands)


# ---------------------------------------------------------------- Slack

def _slack_msgs(sp, p, me, users, busca):
    cid, alvo = p['ref'], core.dt(p['desde']).timestamp()
    out = []
    def add(m, permalink=None):
        if not m.get('ts') or m.get('subtype') in ('channel_join', 'channel_leave'): return
        th = permalinks.slack_thread_ts(permalink) or (m.get('thread_ts') if m.get('thread_ts') != m.get('ts') else None)
        uid = m.get('user')
        out.append({'chave': m['ts'], 'ts': float(m['ts']), 'eu': uid == me, 'thread_ts': th, 'permalink': permalink,
                    'de': sp.nome_user(uid, users) if uid else (m.get('username') or '?'),
                    'texto': sp.texto_limpo(m.get('text') or '', users)})
    for m in sp.historico(cid, 200, oldest=f'{alvo - JANELA_S:.6f}', latest=f'{alvo + JANELA_S:.6f}'):
        add(m)
    # thread replies: search.messages on the day of the message (read only, cached per day for this run)
    dia = datetime.fromtimestamp(alvo, timezone.utc).date()
    chave = dia.isoformat()
    if chave not in busca:
        achadas, pagina = [], 1
        q = f'after:{(dia - timedelta(days=2)).isoformat()} before:{(dia + timedelta(days=2)).isoformat()}'
        while pagina <= PAGINAS_BUSCA:
            d = sp.api('search.messages', query=q, sort='timestamp', sort_dir='desc', count=100, page=pagina)['messages']
            achadas += d.get('matches', [])
            if pagina >= (d.get('paging') or {}).get('pages', 1): break
            pagina += 1
        busca[chave] = achadas
    for m in busca[chave]:
        if (m.get('channel') or {}).get('id') == cid and abs(float(m.get('ts') or 0) - alvo) <= JANELA_S:
            add(m, m.get('permalink'))
    return out


def slack(fonte, pendentes):
    sp = fonte.api()
    tcfg = sp.tp.cfg()
    me, base = tcfg.get('slack_user_id'), tcfg.get('slack_url')
    users = sp.cache()['users']
    busca, res = {}, []
    for p in pendentes:
        try:
            msgs = _slack_msgs(sp, p, me, users, busca)
        except SystemExit as e:
            res.append((p, None, f'erro: {str(e.code)[:120]}')); continue
        m, n = escolhe(p, msgs, dm=p['ref'].startswith('D'))
        if not m:
            res.append((p, None, f'{n} candidatas')); continue
        url = permalinks.slack(base, p['ref'], m['chave'], m['thread_ts']) or m.get('permalink')
        res.append((p, url, {'channel': p['ref'], 'ts': m['chave'], 'thread_ts': m['thread_ts']}))
    return res


# ---------------------------------------------------------------- Teams

def _teams_msgs(tp, normaliza, p, me):
    import urllib.parse
    alvo = core.dt(p['desde']).timestamp()
    path = f"/beta/chats/{urllib.parse.quote(p['ref'], safe='')}/messages?$top=50"
    out = []
    for _ in range(PAGINAS_TEAMS):
        d = tp.get(path)
        brutas = d.get('value', [])
        for raw in brutas:
            m = normaliza(raw, raw.get('id', ''))
            if not m.get('quando') or not m.get('id'): continue
            out.append({'chave': m['id'], 'ts': core.dt(m['quando']).timestamp(), 'eu': m['de'] == me, 'de': m['de'],
                        'texto': m['texto'] or ', '.join(m['anexos']), 'web': raw.get('webUrl')})
        mais_velha = min((core.dt(x.get('createdDateTime')).timestamp() for x in brutas if x.get('createdDateTime')), default=None)
        nxt = d.get('@odata.nextLink')
        if not nxt or mais_velha is None or mais_velha < alvo - JANELA_S: break
        path = nxt[nxt.find('/beta/'):]
    return out


def teams(fonte, pendentes):
    from adapters import ms365
    tp, _ = ms365.teams()
    from teams_feed import normaliza
    me = tp.cfg().get('me', '')
    res = []
    for p in pendentes:
        try:
            msgs = _teams_msgs(tp, normaliza, p, me)
        except SystemExit as e:
            res.append((p, None, f'erro: {str(e.code)[:120]}')); continue
        m, n = escolhe(p, msgs, dm=p['ref'].endswith('@unq.gbl.spaces'))
        if not m:
            res.append((p, None, f'{n} candidatas')); continue
        url = permalinks.teams(p['ref'], m['chave'], m.get('web'))
        res.append((p, url, {'chat': p['ref'], 'id': m['chave']}))
    return res


# ---------------------------------------------------------------- command

FAZ = {'slack': slack, 'teams': teams}


def sem_link(s, fonte):
    return [p for p in core.pendencias(s) if p.get('fonte') == fonte and not p.get('link') and p.get('ref') and p.get('desde')]


def roda(s, fontes, dry):
    """Looks the messages up (read only) and returns (lines to print, [(chave, ref, desde, url, ids)] to write)."""
    linhas, achados = [], []
    for fonte in fontes:
        if fonte.tipo not in FAZ: continue
        pend = sem_link(s, fonte.tipo)
        if not pend: continue
        res = FAZ[fonte.tipo](fonte, pend)
        ok = [(p, url, ids) for p, url, ids in res if url]
        achados += [(p['chave'], p['ref'], p['desde'], url, ids) for p, url, ids in ok]
        linhas.append(f'== {fonte.tipo}: {len(ok)} de {len(res)} sem link ' + ('ganhariam link (--dry)' if dry else 'ganharam link'))
        for p, url, como in res:
            linhas.append(f'  {p["chave"]} [{p.get("status")}] {p.get("quem")} {p["desde"][:16]}: '
                          + (url if url else f'sem link ({como})'))
    if not linhas: linhas.append('nenhuma pendencia de Slack ou Teams sem link')
    return linhas, achados


def aplica(s, achados):
    """Writes the links into state s (the same item: same key, conversation and time, still without a link)."""
    n = 0
    for chave, ref, desde, url, ids in achados:
        p = next((x for x in core.pendencias(s) if x.get('chave') == chave), None)
        if not p or p.get('ref') != ref or p.get('desde') != desde or p.get('link'): continue
        permalinks.guarda(p, url, desde, backfill=True, **ids)
        n += 1
    return n


def cli(s, fontes, dry):
    if not core.LIVE: dry = True                 # an instance in test mode never writes its state
    linhas, achados = roda(s, fontes, dry)
    print('\n'.join(linhas))
    if achados and not dry:
        # the lookups take a while and the runner may have written the state meanwhile: apply to a fresh read
        fresco = core.load_state()
        n = aplica(fresco, achados)
        if n: core.save_state(fresco)
        print(f'gravado: {n} link(s) no estado')
    return 0
