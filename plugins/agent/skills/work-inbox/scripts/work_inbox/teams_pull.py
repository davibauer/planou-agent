#!/usr/bin/env python3
"""Puxa mensagens do Teams sob demanda pelo runtime do conector do Power Automate (mesma sessao do portal).

Uso:
  teams_pull.py --from "Maria"             # ultimas 30 do 1:1 com a Maria
  teams_pull.py --from "Time Dev" -n 60    # grupo pelo topico
  teams_pull.py --from Ana --since 3d      # so as mais novas que 3 dias
  teams_pull.py --list                     # quem esta no cache (1:1 e grupos)
  teams_pull.py --refresh                  # reidentifica os 1:1 (le poucas msgs de cada chat)
  teams_pull.py --token "eyJ0eX..."        # grava um Bearer novo (o do portal vence em ~1h)
  teams_pull.py --json                     # saida crua normalizada
  teams_pull.py --profile <cliente> ...    # outra conta/tenant (perfis em ~/.config/work-inbox/profiles/)

Token: renovado sozinho via `az account get-access-token --resource https://service.flow.microsoft.com/`
(login unico: `az login --tenant <tid> --use-device-code --allow-no-subscriptions`). Fallbacks, nesta ordem:
env TEAMS_PA_TOKEN, <pasta de dados>/token (gravado com --token; ver inbox_paths.py).
"""
import subprocess
import argparse, base64, json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_paths
BASE_DIR = inbox_paths.base()

def _perfis():
    d = f"{BASE_DIR}/profiles"
    return sorted(os.listdir(d)) if os.path.isdir(d) else []

def _cfg_de(perfil):
    try: return json.load(open(f"{BASE_DIR}/profiles/{perfil}/config.json"))
    except (OSError, ValueError): return {}

def _perfil_por_cwd():
    """Perfil cujo config.json lista um prefixo de caminho ("paths") que contem o cwd."""
    cwd = os.path.realpath(os.getcwd()) + "/"
    hits = [(len(pp), p) for p in _perfis() for pp in _cfg_de(p).get("paths", [])
            if cwd.startswith(os.path.realpath(os.path.expanduser(pp)).rstrip("/") + "/")]
    return max(hits)[1] if hits else None  # prefixo mais especifico vence

def _perfil_arg():
    for i, a in enumerate(sys.argv):  # 1. explicito
        if a == "--profile" and i + 1 < len(sys.argv): return sys.argv[i + 1], "--profile"
        if a.startswith("--profile="): return a.split("=", 1)[1], "--profile"
    if inbox_paths.env_profile(): return inbox_paths.env_profile(), "env"   # 2. ambiente (WORK_INBOX_PROFILE ou TEAMS_CHAT_PROFILE)
    p = _perfil_por_cwd()
    if p: return p, "pasta do projeto"                                                         # 3. cwd
    perfis = _perfis()
    if len(perfis) == 1: return perfis[0], "unico perfil"                                     # 4. so ha um
    # 5. fora de qualquer pasta conhecida e com mais de um perfil: NAO assumir — perguntar
    linhas = [f"  --profile {p:<12} {_cfg_de(p).get('conta','?')}   paths: {', '.join(_cfg_de(p).get('paths', [])) or '-'}" for p in perfis]
    sys.exit("Pasta atual nao pertence a nenhum perfil e ha mais de um. Perguntar ao usuario qual conta e repetir com --profile:\n" + "\n".join(linhas))

PERFIL, PERFIL_ORIGEM = _perfil_arg()
os.environ.setdefault("WORK_INBOX_PROFILE", PERFIL)   # teams_feed (nome do autor de mensagem encaminhada) le o perfil daqui
CFG_DIR = f"{BASE_DIR}/profiles/{PERFIL}"
if not os.path.isdir(CFG_DIR):
    sys.exit(f"Perfil '{PERFIL}' nao existe. Perfis: {sorted(os.listdir(BASE_DIR + '/profiles')) if os.path.isdir(BASE_DIR + '/profiles') else []}. Criar com teams_setup.py.")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teams_feed import normaliza, imprime, parse_since, quando_dt  # reuso do leitor do feed

def cfg(): return json.load(open(f"{CFG_DIR}/config.json"))

def _exp(t):
    try:
        return json.loads(base64.urlsafe_b64decode(t.split(".")[1] + "==")).get("exp", 0)
    except (IndexError, ValueError):
        return 0

def _valido(t):
    return bool(t) and _exp(t) > time.time() + 60

def _token_az():
    c = cfg()
    tid = c.get("tenant") or c["env"].replace("Default-", "")
    # --subscription <tenantId> seleciona a CONTA daquele tenant (conta tenant-level tem id == tenantId);
    # --tenant sozinho usa a conta default do az, que muda a cada `az login` de outro cliente
    r = subprocess.run(["az", "account", "get-access-token", "--subscription", tid,
                        "--resource", "https://service.flow.microsoft.com/", "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[az] {r.stderr.strip()[:300]}", file=sys.stderr)
        return None
    return r.stdout.strip()

_TOKEN = None
def token():
    global _TOKEN
    if _valido(_TOKEN):
        return _TOKEN
    cands = [("env TEAMS_PA_TOKEN", os.environ.get("TEAMS_PA_TOKEN"))]
    if os.path.exists(f"{CFG_DIR}/token"):
        cands.append(("arquivo", open(f"{CFG_DIR}/token").read()))
    for origem, t in cands:  # token colado pelo usuario tem prioridade enquanto vale
        t = (t or "").replace("Bearer ", "").strip()
        if _valido(t):
            _TOKEN = t; return t
    t = _token_az()
    if _valido(t):
        _TOKEN = t; return t
    sys.exit("Sem token valido. Renovar o az: `az login --tenant " + cfg()["env"].replace("Default-", "")
             + " --use-device-code --allow-no-subscriptions` (conta corporativa). Alternativa: --token <Bearer do portal>.")

TRANSITORIO = (429, 500, 502, 503, 504)

def abre(req, timeout):
    """urlopen com 2 novas tentativas para erro transitorio do runtime do Power Automate (24/09/2026: 429 do Exchange
    no Mail e 500 no flowbot apareciam em ticks isolados, perto da hora cheia, e viravam fonte quebrada no tick; uma segunda
    tentativa segundos depois passava). Respeita Retry-After (teto 20 s). Devolve o JSON; HTTPError sobe."""
    import time
    for tent in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in TRANSITORIO or tent == 2: raise
            try: espera = min(float(e.headers.get("Retry-After") or 0), 20)
            except ValueError: espera = 0
            time.sleep(espera or (3, 8)[tent])

def get(path):
    req = urllib.request.Request(cfg()["runtime"] + path, headers={"Authorization": "Bearer " + token()})
    try:
        return abre(req, 60)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} em {path}: {e.read()[:300]}")

def sem_acento(s): return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()

def mensagens(chat_id, top):
    out, path = [], f"/beta/chats/{urllib.parse.quote(chat_id, safe='')}/messages?$top={min(top, 50)}"
    while path and len(out) < top:
        d = get(path)
        out += d.get("value", [])
        nxt = d.get("@odata.nextLink")
        path = nxt[nxt.find("/beta/"):] if nxt else None
    return out[:top]

def refresh():
    me = cfg().get("me", "")
    cache = json.load(open(f"{CFG_DIR}/chats.json"))
    v = get("/flowbot/actions/listchats/chattypes/oneOnOne/topic/all/expandmembers/false").get("value", [])
    conhecidos = {c["id"] for c in cache["oneOnOne"]}
    for c in v:
        if c["id"] in conhecidos: continue
        nomes = {((m.get("from") or {}).get("user") or {}).get("displayName") for m in mensagens(c["id"], 20)}
        nomes.discard(me); nomes.discard(None)
        cache["oneOnOne"].append({"nome": ", ".join(sorted(nomes)) or "(sem autor)", "id": c["id"], "ultimo": (c.get("lastUpdatedDateTime") or "")[:10]})
    g = get("/flowbot/actions/listchats/chattypes/all/topic/isDefined/expandmembers/false").get("value", [])
    cache["grupos"] = [{"nome": c.get("topic"), "id": c["id"], "ultimo": (c.get("lastUpdatedDateTime") or "")[:10]} for c in g]
    json.dump(cache, open(f"{CFG_DIR}/chats.json", "w"), ensure_ascii=False, indent=1)
    return cache

def resolve(quem):
    # link do Teams (teams.microsoft.com/l/chat/<id>/...) ou o proprio id (19:...@thread.v2 / @unq.gbl.spaces)
    m = re.search(r"(19:[^/?\s]+@(?:thread\.v2|thread\.skype|unq\.gbl\.spaces))", urllib.parse.unquote(quem))
    if m:
        cid = m.group(1)
        cache = json.load(open(f"{CFG_DIR}/chats.json"))
        known = next((c for k in ("oneOnOne", "grupos") for c in cache[k] if c["id"] == cid), None)
        return known or {"nome": "(chat por id)", "id": cid, "ultimo": ""}
    cache = json.load(open(f"{CFG_DIR}/chats.json"))
    termos = sem_acento(quem).split()
    cands = [c for k in ("oneOnOne", "grupos") for c in cache[k] if all(t in sem_acento(c["nome"]) for t in termos)]
    if not cands:
        outros = {}
        for p in _perfis():
            if p == PERFIL: continue
            try: oc = json.load(open(f"{BASE_DIR}/profiles/{p}/chats.json"))
            except (OSError, ValueError): continue
            hits = [c for k in ("oneOnOne", "grupos") for c in oc[k] if all(t in sem_acento(c["nome"]) for t in termos)]
            if hits: outros[p] = hits
        if len(outros) == 1:
            (p, hits), = outros.items()
            sys.exit(f"'{quem}' nao esta no perfil '{PERFIL}', mas esta no perfil '{p}' ({_cfg_de(p).get('conta')}). "
                     f"Repetir com --profile {p} se for essa a conta.")
        if len(outros) > 1:
            sys.exit(f"'{quem}' nao esta no perfil '{PERFIL}' e bate em varios: {', '.join(outros)}. Escolher com --profile.")
        sys.exit(f"Ninguem no cache bate com '{quem}' (perfil {PERFIL}). Rodar --list para ver nomes, ou --refresh se a pessoa e nova.")
    if len(cands) > 1:
        print(f"[aviso] '{quem}' bate com {len(cands)} chats; usando o mais recente. Outros:", file=sys.stderr)
        for c in sorted(cands, key=lambda c: c["ultimo"], reverse=True)[1:]:
            print(f"   - {c['nome']} ({c['ultimo']})  {c['id']}", file=sys.stderr)
    return max(cands, key=lambda c: c["ultimo"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="quem"); ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--since"); ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--token")
    ap.add_argument("--profile", help="perfil/conta em ~/.config/work-inbox/profiles/")
    a = ap.parse_args()
    c = cfg()
    if not c.get("runtime"):
        sys.exit(f"Perfil '{PERFIL}' nao tem conexao Teams" + (" (so Slack: usar slack_pull.py)" if c.get("slack_token") else "") + ". Criar com teams_setup.py.")
    print(f"-- perfil {PERFIL} [{PERFIL_ORIGEM}]: {c.get('conta', '?')} (tenant {c.get('tenant', '?')[:8]}…)", file=sys.stderr)
    if a.token:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(os.open(f"{CFG_DIR}/token", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(a.token.replace("Bearer ", "").strip())
        print("token gravado, vence", datetime.fromtimestamp(_exp(a.token.replace("Bearer ", "").strip())).strftime("%d/%m %H:%M")); return
    if a.refresh: refresh()
    if a.list or a.refresh:
        cache = json.load(open(f"{CFG_DIR}/chats.json"))
        for k in ("oneOnOne", "grupos"):
            print(f"== {k}")
            for c in sorted(cache[k], key=lambda c: c["ultimo"], reverse=True): print(f"  {c['ultimo']}  {c['nome']}")
        return
    if not a.quem: ap.error("--from e obrigatorio (ou --list/--refresh/--token)")
    chat = resolve(a.quem)
    print(f"-- {chat['nome']}  ({chat['id']})", file=sys.stderr)
    msgs = [normaliza(m, m.get("id", "")) for m in mensagens(chat["id"], a.n)]
    msgs = [m for m in msgs if m["texto"] or m["anexos"]]
    if a.since:
        desde = parse_since(a.since)
        msgs = [m for m in msgs if (quando_dt(m) or datetime.min.replace(tzinfo=timezone.utc)) >= desde]
    msgs.sort(key=lambda m: m["quando"] or "")
    for m in msgs: imprime(m, a.json)

if __name__ == "__main__":
    main()
