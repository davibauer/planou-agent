#!/usr/bin/env python3
"""LinkedIn messages (inbox) through the internal Voyager API, with the browser's session cookie.

There is no official API for your own inbox: the path is the same as the logged-in site — cookie `li_at` + `JSESSIONID`
(the latter also goes in the `csrf-token` header). Session in ~/.config/job-scout/session.json (0600), outside any repo.

Routes (validated on 21/09/2026; the legacy `messaging/conversations` returns 500):
  conversations  voyagerMessagingDashMessengerConversations?q=syncToken&mailboxUrn=<fsd_profile>   (20 most recent, urns only)
  messages       voyagerMessagingDashMessengerMessages?q=syncToken&conversationUrn=<conv>           (20 most recent, text)
  participants   voyagerMessagingDashMessengerMessagingParticipants?q=conversation&conversationUrn= (name + headline)
Names are cached (names.json) by profile urn: the list has no names, and one call per conversation per tick would be too much.

  linkedin_msgs.py --session <li_at> <JSESSIONID>    # saves the session (JSESSIONID with or without the quotes)
  linkedin_msgs.py --curl file.txt                   # or: paste a "Copy as cURL" of any voyager request into a file
  linkedin_msgs.py --me                              # tests the session: who am I
  linkedin_msgs.py --since 24h                       # conversations with a message from someone else in the last 24h (30m, 4h, 2d)
  linkedin_msgs.py --all                             # 20 most recent conversations, read or not (1 + N calls for new names)
  linkedin_msgs.py --show <thread>                   # last 20 msgs of the conversation (id = the 2-... part of the url)
  linkedin_msgs.py --json ...

Read only: does not mark as read, does not reply, does not accept invitations.
"""
import os, sys, re, json, html, time, argparse, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

import criteria, paths
from watch_core import fileio
CFG = paths.SESSION
NAMES_FILE = paths.NAMES
BRT = criteria.tz()   # historical name: it is the user's timezone (tz_hours in the config, default -3)
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36'
HOST = 'https://www.linkedin.com'
API = '/voyager/api'
PAUSE = 1.0            # s between Voyager calls (never a burst: personal account, 999 = blocked)
MAX_DETAILS = 10       # conversations detailed per run; the rest waits for the next tick
ENDPOINTS = {
    'me': f'{API}/me',
    'conversations': f'{API}/voyagerMessagingDashMessengerConversations?q=syncToken&mailboxUrn={{mailbox}}',
    'messages': f'{API}/voyagerMessagingDashMessengerMessages?q=syncToken&conversationUrn={{conv}}',
    'participants': f'{API}/voyagerMessagingDashMessengerMessagingParticipants?q=conversation&conversationUrn={{conv}}',
}

def cfg():
    try: return json.load(open(CFG))
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save(c):
    os.makedirs(paths.SECRETS_DIR, mode=0o700, exist_ok=True)
    fd = os.open(CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, json.dumps(c, indent=1).encode()); os.close(fd)

def session():
    c = cfg()
    if not c.get('li_at') or not c.get('jsessionid'):
        sys.exit('Sem sessao: rode `linkedin_msgs.py --session <li_at> <JSESSIONID>` (F12 > Application > Cookies > www.linkedin.com)')
    return c

_last = 0.0

def api(path, raw=False):
    c = session()
    js = c['jsessionid'].strip('"')
    h = {'User-Agent': c.get('ua') or UA, 'Accept': 'application/json', 'x-restli-protocol-version': '2.0.0',
         'x-li-lang': 'en_US', 'csrf-token': js, 'Cookie': f'li_at={c["li_at"]}; JSESSIONID="{js}"',
         'Referer': HOST + '/messaging/'}
    global _last
    wait = _last + PAUSE - time.monotonic()
    if wait > 0: time.sleep(wait)
    _last = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(HOST + path, headers=h), timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f'Sessao recusada ({e.code}): o li_at venceu ou o LinkedIn pediu login de novo. Copie o li_at e o JSESSIONID de novo e rode `linkedin_msgs.py --session <li_at> <JSESSIONID>`')
        if e.code == 999: sys.exit('HTTP 999: o LinkedIn bloqueou a chamada (anti-automacao). Parar o job-scout por umas horas e conferir a conta no navegador.')
        if e.code == 429: sys.exit('HTTP 429: rate limit; tentar no proximo tick')
        sys.exit(f'HTTP {e.code} em {path[:80]}: {e.read()[:200]!r}')
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f'rede: {e}')
    if raw: return body
    try: return json.loads(body)
    except json.JSONDecodeError:
        sys.exit(f'resposta nao-JSON em {path[:60]} (redirect para login?): {body[:120]!r}')

def _q(s): return urllib.parse.quote(s, safe='')
def _iso(ms): return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None
def fmt_time(iso): return datetime.fromisoformat(iso).astimezone(BRT).strftime('%d/%m %H:%M') if iso else '??'
def _profile(urn): return re.sub(r'^urn:li:msg_messagingParticipant:', '', urn or '')   # -> urn:li:fsd_profile:...
def _thread(conv_urn):
    m = re.search(r',(2-[^)]+)\)$', conv_urn or ''); return m.group(1) if m else conv_urn

# ---------------- names (cache) ----------------
def _names():
    try: n = json.load(open(NAMES_FILE))
    except (FileNotFoundError, json.JSONDecodeError): return {}
    for e in n.values():                   # cache written up to 0.8.x: {'nome', 'headline'} -> {'name', 'headline'}
        if isinstance(e, dict) and 'nome' in e: e.setdefault('name', e.pop('nome'))
    return n

def _save_names(n):
    os.makedirs(paths.CACHE_DIR, exist_ok=True)
    fileio.write_json(NAMES_FILE, n, ensure_ascii=False, indent=0)

def participants(conv_urn, cache):
    """Fills the cache {profile_urn: {name, headline}} with the conversation's participants; returns the urns."""
    d = api(ENDPOINTS['participants'].format(conv=_q(conv_urn)))
    urns = []
    for e in d.get('elements') or []:
        m = (e.get('participantTypeUnion') or {}).get('member') or {}
        p = e.get('hostIdentityUrn') or _profile(e.get('entityUrn'))
        if not m:   # organization/bot
            o = (e.get('participantTypeUnion') or {}).get('organization') or (e.get('participantTypeUnion') or {}).get('custom') or {}
            cache[p] = {'name': (o.get('name') or {}).get('text') or 'org', 'headline': ''}
        else:
            cache[p] = {'name': f"{(m.get('firstName') or {}).get('text', '')} {(m.get('lastName') or {}).get('text', '')}".strip() or '?',
                        'headline': (m.get('headline') or {}).get('text', '')}
        urns.append(p)
    return urns

def name(profile_urn, cache):
    return (cache.get(profile_urn) or {}).get('name') or profile_urn.split(':')[-1][:12]

# ---------------- API ----------------
def me():
    d = api(ENDPOINTS['me'])
    mp = d.get('miniProfile') or {}
    return {'urn': mp.get('dashEntityUrn') or '', 'name': f"{mp.get('firstName', '')} {mp.get('lastName', '')}".strip(),
            'public': mp.get('publicIdentifier', ''), 'plainId': d.get('plainId')}

def conversations(me_urn):
    """20 most recent conversations, metadata only (one call)."""
    d = api(ENDPOINTS['conversations'].format(mailbox=_q(me_urn)))
    out = []
    for c in d.get('elements') or []:
        others = [_profile(u) for u in c.get('conversationParticipantsUrns') or [] if _profile(u) != me_urn]
        out.append({'urn': c.get('entityUrn'), 'id': _thread(c.get('entityUrn')), 'when': _iso(c.get('lastActivityAt')),
                    'is_read': bool(c.get('read', True)), 'unread': c.get('unreadCount', 0), 'group': bool(c.get('groupChat')),
                    'inmail': 'INMAIL' in (c.get('categories') or []), 'others': others, 'n_msgs': c.get('countableMessageCount'),
                    'url': c.get('conversationUrl') or f'{HOST}/messaging/thread/{_thread(c.get("entityUrn"))}/'})
    out.sort(key=lambda c: c['when'] or '', reverse=True)
    return out

def messages(conv_urn, cache):
    """20 most recent msgs of the conversation, oldest to newest; names from the cache (fetches participants if missing)."""
    d = api(ENDPOINTS['messages'].format(conv=_q(conv_urn)))
    evs = []
    for m in d.get('elements') or []:
        sender = _profile(m.get('senderUrn') or m.get('actorUrn'))
        txt = ((m.get('body') or {}).get('text') or '').strip()
        rc = m.get('renderContent') or []
        if not txt and rc: txt = '[anexo/conteudo: ' + ','.join(sorted(k for r in rc for k in r.keys())) + ']'
        if m.get('subject'): txt = f"[{m['subject']}] {txt}"
        evs.append({'when': _iso(m.get('deliveredAt')), 'sender_urn': sender, 'text': re.sub(r'\s+', ' ', html.unescape(txt))})
    missing = {e['sender_urn'] for e in evs} - set(cache)
    if missing: participants(conv_urn, cache)
    for e in evs: e['sender'] = name(e['sender_urn'], cache)
    evs.sort(key=lambda e: e['when'] or '')
    return evs

def detail(c, me_urn, cache, since=None):
    """Enriches a conversation from the list: name/headline of the others, last msgs (since `since`, if given), who sent the last one."""
    evs = messages(c['urn'], cache)
    if set(c['others']) - set(cache): participants(c['urn'], cache)
    c['with'] = ', '.join(name(u, cache) for u in c['others']) or '?'
    c['headline'] = '; '.join((cache.get(u) or {}).get('headline', '') for u in c['others'] if (cache.get(u) or {}).get('headline'))[:140]
    c['msgs'] = [e for e in evs if not since or (e['when'] and datetime.fromisoformat(e['when']) > since)] or evs[-1:]
    c['my_last'] = bool(evs) and evs[-1]['sender_urn'] == me_urn
    c['sender'] = evs[-1]['sender'] if evs else '?'; c['text'] = evs[-1]['text'] if evs else ''
    return c

def new_conversations(since, me_urn, cache):
    """Conversations with activity after `since` whose last message is from someone else (1 + 1..2 calls per active conversation)."""
    out, n = [], 0
    for c in conversations(me_urn):
        if not c['when'] or datetime.fromisoformat(c['when']) <= since: continue
        if n >= MAX_DETAILS: print(f'   (… mais conversas ativas alem de {MAX_DETAILS}; ficam para a proxima rodada)'); break
        detail(c, me_urn, cache, since); n += 1
        if not c['my_last']: out.append(c)
    return out

def parse_since(s):
    m = re.fullmatch(r'(\d+)\s*(m|h|d)', s.strip())
    if not m: sys.exit('--since: use 30m, 4h, 2d')
    return datetime.now(timezone.utc) - timedelta(seconds=int(m.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[m.group(2)])

def print_block(cs, me_urn, title='== MENSAGENS'):
    if not cs: return False
    print(f'{title} ({len(cs)} conversas)')
    for c in cs:
        state = 'lida' if c['is_read'] else 'NAO LIDA'
        kind = ('grupo' if c['group'] else 'dm') + (' · InMail' if c['inmail'] else '')
        hl = f' · {c["headline"]}' if c.get('headline') else ''
        print(f'-- {c["with"]} ({kind}){hl} · {state}')
        for m in c.get('msgs') or []:
            who = 'eu' if m['sender_urn'] == me_urn else m['sender']
            print(f'   [{fmt_time(m["when"])}] {who}: {m["text"][:400]}')
        print(f'   {c["url"]}')
    return True

def import_curl(path):
    t = open(path).read()
    li = re.search(r'li_at=([^;"\'\s]+)', t); js = re.search(r'JSESSIONID=\\?"?(ajax:-?\d+)', t)
    ua = re.search(r"-H ['\"]user-agent: ([^'\"]+)['\"]", t, re.I)
    if not (li and js): sys.exit('nao achei li_at e JSESSIONID no cURL (precisa ser um request de www.linkedin.com/voyager/api/, com -b ou cookie:)')
    return li.group(1), js.group(1), (ua.group(1) if ua else None)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--session', nargs=2, metavar=('LI_AT', 'JSESSIONID')); ap.add_argument('--curl', metavar='FILE')
    ap.add_argument('--me', action='store_true'); ap.add_argument('--all', action='store_true')
    ap.add_argument('--since'); ap.add_argument('--show', metavar='THREAD'); ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if a.session or a.curl:
        c = cfg()
        if a.curl: c['li_at'], c['jsessionid'], ua = import_curl(a.curl); c['ua'] = ua or c.get('ua')
        else: c['li_at'], c['jsessionid'] = a.session[0].split('=', 1)[-1].strip(), a.session[1].strip().strip('"')
        c.pop('gravada', None); c['saved_at'] = datetime.now(timezone.utc).isoformat(); save(c)
        print(f'sessao gravada em {CFG}; sondando /me…')
        try: m = me(); print(f'OK: {m["name"]} ({m["public"]})')
        except SystemExit as e: print(f'sonda falhou (a sessao ficou gravada): {e.code}\nSe for 401/403 o cookie esta errado/vencido; se for nao-JSON ou 404, a rota em ENDPOINTS mudou: capturar um cURL novo e comparar.')
        return
    myself = me()
    if a.me:
        print(json.dumps(myself, ensure_ascii=False) if a.json else f'{myself["name"]} · {myself["public"]} · {myself["urn"]}'); return
    cache = _names()
    try:
        if a.show:
            conv = a.show if a.show.startswith('urn:') else f'urn:li:msg_conversation:({myself["urn"]},{a.show})'
            evs = messages(conv, cache)
            if a.json: print(json.dumps(evs, ensure_ascii=False, indent=1)); return
            for e in evs: print(f'[{fmt_time(e["when"])}] {"eu" if e["sender_urn"] == myself["urn"] else e["sender"]}: {e["text"]}')
            return
        if a.all:
            cs = conversations(myself['urn'])[:MAX_DETAILS]
            for c in cs: detail(c, myself['urn'], cache); c['msgs'] = c['msgs'][-1:]
            if a.json: print(json.dumps(cs, ensure_ascii=False, indent=1)); return
            print_block(cs, myself['urn'], '== CONVERSAS'); return
        cs = new_conversations(parse_since(a.since or '24h'), myself['urn'], cache)
        if a.json: print(json.dumps(cs, ensure_ascii=False, indent=1)); return
        if not print_block(cs, myself['urn']): print('nenhuma mensagem nova de outra pessoa no periodo')
    finally:
        _save_names(cache)

if __name__ == '__main__':
    main()
