#!/usr/bin/env python3
"""Le mensagens do Slack sob demanda pela Web API, com token gravado no perfil (sem app instalado no workspace).

Uso:
  slack_pull.py --from "#dev"                 # ultimas 30 do canal (publico ou privado)
  slack_pull.py --from Ana                    # DM 1:1 pelo nome (parcial, sem acento serve)
  slack_pull.py --from "https://x.slack.com/archives/C0.../p1694..."   # link de canal => o canal; de mensagem => contexto ate ela; com thread_ts => a thread
  slack_pull.py --from "#dev" --since 2d      # recorte temporal (30m, 4h, 2d, 3du, ISO) — vai ao servidor (oldest)
  slack_pull.py --from "#dev" --threads       # expande as respostas das threads inline
  slack_pull.py --thread C0.../1694...        # so uma thread (canal/ts do pai)
  slack_pull.py --search "audit report"       # busca do Slack (modifiers: in:#canal from:@user after:2026-09-01)
  slack_pull.py --list                        # canais/DMs conhecidos (cache slack.json)
  slack_pull.py --refresh                     # remonta o cache (conversas do usuario + diretorio de pessoas)
  slack_pull.py --json ...                    # cru normalizado, uma linha por mensagem
  slack_pull.py --profile <cliente> ...       # outra conta/workspace

Credencial (uma vez por workspace; cria o perfil se nao existir):
  slack_pull.py --profile acme --paths ~/Projects/acme --token xoxc-... --cookie xoxd-...
  * xoxc- (sessao do navegador) EXIGE o cookie `d` (xoxd-...); xoxp-/xoxb- (app) dispensam o cookie.
  * env SLACK_TOKEN / SLACK_COOKIE tem prioridade sobre o config.json.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_paths
BASE_DIR = inbox_paths.base()

def _pre(flag):
    """Le um argumento antes do argparse (o perfil e resolvido no import do teams_pull)."""
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv): return sys.argv[i + 1]
        if a.startswith(flag + "="): return a.split("=", 1)[1]
    return None

# --token com --profile novo: cria a pasta antes do import, senao o teams_pull recusa o perfil inexistente
if _pre("--token") and _pre("--profile") and not os.path.isdir(f"{BASE_DIR}/profiles/{_pre('--profile')}"):
    d = f"{BASE_DIR}/profiles/{_pre('--profile')}"
    os.makedirs(d, mode=0o700)
    json.dump({"paths": []}, open(f"{d}/config.json", "w"), indent=1); os.chmod(f"{d}/config.json", 0o600)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import teams_pull as tp  # PERFIL, PERFIL_ORIGEM, CFG_DIR, cfg(), sem_acento, _perfis, _cfg_de
from teams_feed import imprime, parse_since, quando_dt

CACHE = f"{tp.CFG_DIR}/slack.json"

def cred():
    c = tp.cfg()
    tok = os.environ.get("SLACK_TOKEN") or c.get("slack_token")
    ck = os.environ.get("SLACK_COOKIE") or c.get("slack_cookie")
    if not tok:
        sys.exit(f"Perfil '{tp.PERFIL}' sem token do Slack. Gravar com: slack_pull.py --profile {tp.PERFIL} --token xoxc-... --cookie xoxd-... "
                 "(app.slack.com > F12 > Application > Cookies > `d`; token: Network > qualquer api/* > form data `token`).")
    if tok.startswith("xoxc-") and not ck:
        sys.exit("Token xoxc- (sessao do navegador) exige o cookie `d` (xoxd-...): repetir com --cookie.")
    return tok, ck

def api(method, **params):
    tok, ck = cred()
    headers = {"Authorization": "Bearer " + tok, "Content-Type": "application/x-www-form-urlencoded",
               "User-Agent": "Mozilla/5.0 (slack_pull)"}
    if ck: headers["Cookie"] = "d=" + (ck if "%" in ck else urllib.parse.quote(ck, safe=""))  # o navegador manda o xoxd percent-encoded
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    for _ in range(4):
        req = urllib.request.Request(f"https://slack.com/api/{method}", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5"))); continue
            sys.exit(f"HTTP {e.code} em {method}: {e.read()[:300]}")
        if d.get("ok"): return d
        err = d.get("error", "?")
        if err == "ratelimited":
            time.sleep(5); continue
        if err in ("invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"):
            sys.exit(f"Slack recusou a credencial ({err}). Sessao do navegador venceu? Pegar token+cookie novos e regravar com --token/--cookie.")
        if err == "missing_scope" or err == "not_allowed_token_type":
            sys.exit(f"{method}: {err} — token de app sem escopo; com xoxc- (sessao do usuario) esse metodo funciona.")
        sys.exit(f"{method}: {err}")
    sys.exit(f"{method}: rate limit persistente")

def paginado(method, chave, **params):
    out, cursor = [], None
    while True:
        d = api(method, cursor=cursor, **params)
        out += d.get(chave, [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor: return out

# ---------- cache: pessoas + conversas ----------
def carrega_cache():
    try: return json.load(open(CACHE))
    except (OSError, ValueError): return None

def refresh():
    users = {}
    for u in paginado("users.list", "members", limit=200):
        p = u.get("profile") or {}
        users[u["id"]] = {"nome": (p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name")) + (" (inativo)" if u.get("deleted") else ""),
                          "real": p.get("real_name") or u.get("real_name") or "", "handle": u.get("name"), "bot": bool(u.get("is_bot"))}
    convs = []
    for c in paginado("users.conversations", "channels", types="public_channel,private_channel,mpim,im", exclude_archived="true", limit=200):
        if c.get("is_im"):
            nome, tipo = nome_user(c.get("user"), users), "dm"  # externo/Slack Connect nao vem no users.list
        elif c.get("is_mpim"):
            membros = paginado("conversations.members", "members", channel=c["id"], limit=200)
            me = tp.cfg().get("slack_user_id")
            nome = ", ".join(sorted(nome_user(m, users) for m in membros if m != me)) or c.get("name")
            tipo = "grupo"
        else:
            nome, tipo = "#" + c.get("name", c["id"]), "privado" if c.get("is_private") else "canal"
        convs.append({"nome": nome, "id": c["id"], "tipo": tipo, "ultimo": _dia(c.get("updated", 0) / 1000 if c.get("updated") else None)})
    cache = {"quando": datetime.now(timezone.utc).isoformat(), "users": users, "conversas": convs}
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False, indent=1); os.chmod(CACHE, 0o600)
    return cache

def _dia(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""

def cache():
    return carrega_cache() or refresh()

def nome_user(uid, users):
    if uid in users: return users[uid]["nome"]
    try:
        u = api("users.info", user=uid)["user"]; p = u.get("profile") or {}
        users[uid] = {"nome": p.get("display_name") or p.get("real_name") or u.get("name"), "real": p.get("real_name", ""), "handle": u.get("name"), "bot": bool(u.get("is_bot"))}
        return users[uid]["nome"]
    except SystemExit:
        return uid

# ---------- resolucao do alvo ----------
LINK = re.compile(r"slack\.com/archives/([A-Z0-9]+)(?:/p(\d{10})(\d{6}))?")

def resolve(quem):
    m = LINK.search(quem)
    if m:  # link de canal ou de mensagem
        cid, ts = m.group(1), (f"{m.group(2)}.{m.group(3)}" if m.group(2) else None)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(quem).query)
        thread = (q.get("thread_ts") or [None])[0]
        return {"id": cid, "nome": _nome_por_id(cid), "thread": thread, "ate": None if thread else ts}
    if re.fullmatch(r"[CDG][A-Z0-9]{8,}", quem):
        return {"id": quem, "nome": _nome_por_id(quem), "thread": None, "ate": None}
    cs = cache()["conversas"]
    termos = tp.sem_acento(quem.lstrip("#")).split()
    def bate(c): return all(t in tp.sem_acento(c["nome"]) for t in termos)
    cands = [c for c in cs if bate(c)]
    if quem.startswith("#"): cands = [c for c in cands if c["tipo"] in ("canal", "privado")]
    if not cands:
        outros = {}
        for p in tp._perfis():
            if p == tp.PERFIL: continue
            try: oc = json.load(open(f"{BASE_DIR}/profiles/{p}/slack.json"))
            except (OSError, ValueError): continue
            if any(bate(c) for c in oc["conversas"]): outros[p] = True
        if len(outros) == 1:
            sys.exit(f"'{quem}' nao esta no perfil '{tp.PERFIL}', mas esta no Slack do perfil '{list(outros)[0]}'. Repetir com --profile se for essa a conta.")
        sys.exit(f"Nada no cache bate com '{quem}' (perfil {tp.PERFIL}). Rodar --list para ver nomes, ou --refresh se e canal/pessoa nova.")
    # DM exato pelo nome vence canal que so contem o termo; depois o mais recente
    exatos = [c for c in cands if tp.sem_acento(c["nome"].lstrip("#")) == " ".join(termos)]
    if len(exatos) == 1: cands = exatos
    if len(cands) > 1:
        print(f"[aviso] '{quem}' bate com {len(cands)} conversas; usando a mais recente. Outras:", file=sys.stderr)
        for c in sorted(cands, key=lambda c: c["ultimo"], reverse=True)[1:]:
            print(f"   - {c['nome']} [{c['tipo']}] ({c['ultimo'] or '?'})  {c['id']}", file=sys.stderr)
    c = max(cands, key=lambda c: c["ultimo"])
    return {"id": c["id"], "nome": c["nome"], "thread": None, "ate": None}

def _nome_por_id(cid):
    c = carrega_cache() or {"conversas": []}
    return next((x["nome"] for x in c["conversas"] if x["id"] == cid), cid)

# ---------- mensagens ----------
def texto_limpo(t, users):
    t = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + nome_user(m.group(1), users), t or "")
    t = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", t)
    t = re.sub(r"<!(here|channel|everyone)>", r"@\1", t)
    t = re.sub(r"<!subteam\^[A-Z0-9]+\|@?([^>]*)>", r"@\1", t)
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()

def normaliza(m, cid, users):
    de = m.get("user") or (m.get("bot_profile") or {}).get("name") or m.get("username") or "?"
    if de.startswith(("U", "W")) and m.get("user"): de = nome_user(de, users)
    texto = texto_limpo(m.get("text"), users)
    for a in m.get("attachments") or []:  # unfurls/bots: titulo + texto
        extra = " ".join(x for x in (a.get("title"), a.get("text") or a.get("fallback")) if x)
        if extra and extra not in texto: texto += ("\n" if texto else "") + texto_limpo(extra, users)
    ts = m.get("ts", "")
    return {"id": ts, "quando": datetime.fromtimestamp(float(ts), timezone.utc).isoformat() if ts else None, "chat": cid,
            "de": de, "texto": texto, "anexos": [f.get("name") or f.get("title") for f in (m.get("files") or []) if f.get("name") or f.get("title")],
            "thread": m.get("thread_ts") if m.get("thread_ts") and m.get("thread_ts") != ts else None,
            "respostas": m.get("reply_count", 0), "reacoes": {r["name"]: r["count"] for r in (m.get("reactions") or [])}}

def historico(cid, n, oldest=None, latest=None):
    out, cursor = [], None
    while len(out) < n:
        d = api("conversations.history", channel=cid, limit=min(n - len(out), 200), oldest=oldest, latest=latest,
                inclusive="true" if latest else None, cursor=cursor)
        out += d.get("messages", [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor") or None
        if not d.get("has_more") or not cursor: break
    return out

def thread(cid, ts, n=200):
    return paginado("conversations.replies", "messages", channel=cid, ts=ts, limit=min(n, 200))

def imprime_msg(m, as_json, indent=""):
    if as_json: imprime(m, True); return
    q = quando_dt(m)
    hora = q.astimezone().strftime("%d/%m %H:%M") if q else "??/?? ??:??"
    extras = []
    if m["anexos"]: extras.append("anexos: " + ", ".join(m["anexos"]))
    if m["respostas"]: extras.append(f"thread: {m['respostas']} respostas, ts={m['id']}")
    if m["reacoes"]: extras.append("reacoes: " + " ".join(f":{k}:{v}" for k, v in m["reacoes"].items()))
    ex = f"  [{' | '.join(extras)}]" if extras else ""
    txt = m["texto"].replace("\n", "\n" + indent + "    ")
    print(f"{indent}[{hora}] {m['de']}: {txt}{ex}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="quem"); ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--since"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--threads", action="store_true", help="expande as respostas das threads inline")
    ap.add_argument("--thread", help="canal/ts do pai (ou so o ts, com --from)")
    ap.add_argument("--search", help="busca do Slack (search.messages)")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--token"); ap.add_argument("--cookie"); ap.add_argument("--paths", nargs="*")
    ap.add_argument("--profile")
    a = ap.parse_args()
    c = tp.cfg()

    if a.token or a.cookie or a.paths is not None:
        if a.token: c["slack_token"] = a.token.strip()
        if a.cookie: c["slack_cookie"] = a.cookie.strip().removeprefix("d=")
        if a.paths is not None: c["paths"] = a.paths
        json.dump(c, open(f"{tp.CFG_DIR}/config.json", "w"), indent=1); os.chmod(f"{tp.CFG_DIR}/config.json", 0o600)
        if a.token or a.cookie:
            me = api("auth.test")
            c.update({"slack_workspace": me.get("team"), "slack_url": me.get("url"), "slack_user": me.get("user"), "slack_user_id": me.get("user_id")})
            c.setdefault("conta", f"{me.get('user')}@{me.get('team')} (slack)")
            json.dump(c, open(f"{tp.CFG_DIR}/config.json", "w"), indent=1)
            print(f"credencial ok: {me.get('user')} em {me.get('team')} ({me.get('url')}). Montando cache...")
            refresh()
        print(f"perfil '{tp.PERFIL}' gravado (paths: {', '.join(c.get('paths') or []) or '-'})"); return

    print(f"-- perfil {tp.PERFIL} [{tp.PERFIL_ORIGEM}]: {c.get('slack_user', '?')} @ {c.get('slack_workspace', '?')} (slack)", file=sys.stderr)
    if a.refresh: refresh()
    if a.list or a.refresh:
        cs = cache()["conversas"]
        for tipo, rotulo in (("canal", "canais"), ("privado", "privados"), ("grupo", "grupos"), ("dm", "DMs")):
            xs = [x for x in cs if x["tipo"] == tipo]
            if not xs: continue
            print(f"== {rotulo} ({len(xs)})")
            for x in sorted(xs, key=lambda x: (x["ultimo"], x["nome"]), reverse=True): print(f"  {x['ultimo'] or '          '}  {x['nome']}")
        return

    cache_obj = cache(); users = cache_obj["users"]
    if a.search:
        d = api("search.messages", query=a.search, count=min(a.n, 100), sort="timestamp", sort_dir="desc")
        ms = d.get("messages", {}).get("matches", [])
        print(f"-- {d.get('messages', {}).get('total', len(ms))} resultados para '{a.search}'", file=sys.stderr)
        for m in reversed(ms):
            ch = m.get("channel") or {}
            n = normaliza(m, ch.get("id"), users)
            n["canal"] = ("DM " + nome_user(ch["name"], users)) if re.fullmatch(r"[UW][A-Z0-9]{8,}", ch.get("name") or "") else "#" + ch.get("name", "?")
            if a.json: imprime(n, True)
            else: print(f"{n['canal']} ", end=""); imprime_msg(n, False)
        return

    if a.thread and "/" in a.thread:
        cid, ts = a.thread.split("/", 1); alvo = {"id": cid, "nome": _nome_por_id(cid), "thread": ts, "ate": None}
    else:
        if not a.quem: ap.error("--from e obrigatorio (ou --list/--refresh/--search/--thread canal/ts/--token)")
        alvo = resolve(a.quem)
        if a.thread: alvo["thread"] = a.thread
    print(f"-- {alvo['nome']}  ({alvo['id']}{'  thread ' + alvo['thread'] if alvo['thread'] else ''}{'  ate a msg ' + alvo['ate'] if alvo.get('ate') else ''})", file=sys.stderr)

    if alvo["thread"]:
        msgs = [normaliza(m, alvo["id"], users) for m in thread(alvo["id"], alvo["thread"], a.n)]
        msgs.sort(key=lambda m: m["id"])
        for i, m in enumerate(msgs): imprime_msg(m, a.json, "" if i == 0 else "  ↳ ")
        return

    oldest = None
    if a.since:
        oldest = f"{parse_since(a.since).timestamp():.6f}"
    raw = historico(alvo["id"], a.n if not a.since else max(a.n, 500), oldest, alvo.get("ate"))  # com --since o servidor corta; -n vira piso
    msgs = [normaliza(m, alvo["id"], users) for m in raw if m.get("subtype") not in ("channel_join", "channel_leave", "bot_add")]
    msgs = [m for m in msgs if m["texto"] or m["anexos"]]
    msgs.sort(key=lambda m: m["id"])
    for m in msgs:
        imprime_msg(m, a.json)
        if a.threads and m["respostas"]:
            for r in sorted((normaliza(x, alvo["id"], users) for x in thread(alvo["id"], m["id"])[1:]), key=lambda x: x["id"]):
                imprime_msg(r, a.json, "  ↳ ")
    json.dump(cache_obj, open(CACHE, "w"), ensure_ascii=False, indent=1)  # persiste users descobertos via users.info

if __name__ == "__main__":
    main()
