#!/usr/bin/env python3
"""Cria um perfil (conta/tenant) para o plugin work-inbox.

  teams_setup.py --profile acme --tenant <tenant-id> [--default]

Passos (os mesmos feitos a mao para o primeiro perfil em 17/09/2026):
  1. token do az para o tenant (se falhar, mostra o `az login` a rodar e para)
  2. ambiente default do Power Platform desse tenant
  3. cria a conexao do conector Microsoft Teams (nasce "Unauthenticated")
  4. imprime a URL do portal para o usuario clicar em Fix connection e espera Enter
  5. le a conexao autenticada, descobre o host do runtime (testLinks) e grava config.json
  6. monta o cache de chats (grupos por topico; 1:1 identificados pelo autor das ultimas msgs)
Nao precisa de admin: tudo roda como o proprio usuario.
"""
import argparse, json, os, subprocess, sys, urllib.parse, urllib.request, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox_paths
BASE = inbox_paths.base()

def az_token(tenant, resource):
    r = subprocess.run(["az", "account", "get-access-token", "--subscription", tenant, "--resource", resource,
                        "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None

def http(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"raw": e.read()[:300].decode(errors="replace")}

def _garante_outlook_pendente(tf, tp, flt, env_name):
    """No modo --no-wait, ja cria a conexao Outlook junto para o usuario clicar nas duas de uma vez."""
    ob = "https://api.powerapps.com/providers/Microsoft.PowerApps/apis/shared_office365/connections"
    _, ex = http("GET", f"{ob}?{urllib.parse.quote(flt, safe='=&$')}", tp)
    if ex.get("value"):
        return
    on = uuid.uuid4().hex
    code, oc = http("PUT", f"{ob}/{on}?{urllib.parse.quote(flt, safe='=&$')}", tf,
                    {"properties": {"environment": {"id": f"/providers/Microsoft.PowerApps/environments/{env_name}", "name": env_name}}})
    print(f"[5b] conexao Outlook criada: {on}. Clicar em 'Fix connection' tambem em Office 365 Outlook." if code in (200, 201) else f"[5b] criar Outlook falhou: {code} {oc}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True); ap.add_argument("--tenant", required=True)
    ap.add_argument("--default", action="store_true", help="torna este o perfil padrao")
    ap.add_argument("--outlook", action="store_true", help="tambem cria/liga a conexao Office 365 Outlook (e-mail)")
    ap.add_argument("--no-wait", action="store_true", help="nao espera Enter: cria o que falta, imprime o que clicar e sai (rodar de novo depois)")
    ap.add_argument("--paths", nargs="*", default=None, help="pastas de projeto que selecionam este perfil pelo cwd")
    a = ap.parse_args()
    d = f"{BASE}/profiles/{a.profile}"; os.makedirs(d, mode=0o700, exist_ok=True)

    tf = az_token(a.tenant, "https://service.flow.microsoft.com/")
    tp = az_token(a.tenant, "https://service.powerapps.com/")
    if not (tf and tp):
        sys.exit(f"az sem sessao nesse tenant. Rodar:\n  az login --tenant {a.tenant} --use-device-code --allow-no-subscriptions\ne repetir o setup.")
    tg = az_token(a.tenant, "https://graph.microsoft.com/")
    me = {}
    if tg:
        _, me = http("GET", "https://graph.microsoft.com/v1.0/me?$select=displayName,userPrincipalName", tg)
    print(f"[1] sessao az ok: {me.get('userPrincipalName','?')}")

    _, envs = http("GET", "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments?api-version=2016-11-01", tf)
    env = next((e for e in envs.get("value", []) if e["properties"].get("isDefault")), None) or envs["value"][0]
    env_name = env["name"]; print(f"[2] ambiente: {env['properties'].get('displayName')} ({env_name})")

    flt = f"api-version=2016-11-01&$filter=environment eq '{env_name}'"
    base = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apis/shared_teams/connections"
    _, existing = http("GET", f"{base}?{urllib.parse.quote(flt, safe='=&$')}", tp)
    conn = next((c for c in existing.get("value", []) if all(s["status"] == "Connected" for s in c["properties"]["statuses"])), None)
    if conn:
        print(f"[3] conexao Teams ja autenticada: {conn['name']}")
    else:
        name = uuid.uuid4().hex
        code, conn = http("PUT", f"{base}/{name}?{urllib.parse.quote(flt, safe='=&$')}", tf,
                          {"properties": {"environment": {"id": f"/providers/Microsoft.PowerApps/environments/{env_name}", "name": env_name}}})
        if code not in (200, 201): sys.exit(f"criar conexao falhou: {code} {conn}")
        print(f"[3] conexao Teams criada: {name} (ainda nao autenticada)")
        print(f"[4] AGORA: abrir https://make.powerautomate.com/environments/{env_name}/connections , clicar em 'Fix connection' na conexao Microsoft Teams e logar com a conta desse tenant.")
        if a.no_wait:
            if a.outlook: _garante_outlook_pendente(tf, tp, flt, env_name)
            sys.exit(2)
        input("    Enter quando estiver 'Connected'... ")
        _, conn = http("GET", f"{base}/{name}?{urllib.parse.quote(flt, safe='=&$')}", tp)
        st = [s["status"] for s in conn["properties"]["statuses"]]
        if st != ["Connected"]: sys.exit(f"conexao ainda nao autenticada: {st}")

    test = conn["properties"]["testLinks"][0]["requestUri"]
    runtime = test[: test.index(conn["name"]) + len(conn["name"])]  # dois formatos: .../connections/{id} (powerplatform) e /apim/teams/{id} (azure-apihub)
    cfg = {"env": env_name, "tenant": a.tenant, "conta": me.get("userPrincipalName"), "me": me.get("displayName"),
           "connection": conn["name"], "runtime": runtime}
    antigo = {}
    try: antigo = json.load(open(f"{d}/config.json"))
    except (OSError, ValueError): pass
    cfg["paths"] = a.paths if a.paths is not None else antigo.get("paths", [])
    for k in ("outlook_connection", "outlook_runtime"):
        if antigo.get(k): cfg[k] = antigo[k]
    json.dump(cfg, open(f"{d}/config.json", "w"), indent=1); os.chmod(f"{d}/config.json", 0o600)
    print(f"[5] runtime: {runtime}")
    if a.outlook:
        ob = "https://api.powerapps.com/providers/Microsoft.PowerApps/apis/shared_office365/connections"
        _, ex = http("GET", f"{ob}?{urllib.parse.quote(flt, safe='=&$')}", tp)
        oc = next((c for c in ex.get("value", []) if all(s["status"] == "Connected" for s in c["properties"]["statuses"])), None)
        pend = next((c for c in ex.get("value", []) if c not in [oc]), None)
        if not oc and pend:
            st = [s["status"] for s in pend["properties"]["statuses"]]
            sys.exit(f"conexao Outlook {pend['name']} ainda nao autenticada ({st}): clicar em Fix connection e rodar de novo.")
        if not oc:
            on = uuid.uuid4().hex
            code, oc = http("PUT", f"{ob}/{on}?{urllib.parse.quote(flt, safe='=&$')}", tf,
                            {"properties": {"environment": {"id": f"/providers/Microsoft.PowerApps/environments/{env_name}", "name": env_name}}})
            if code not in (200, 201): sys.exit(f"criar conexao Outlook falhou: {code} {oc}")
            print(f"[5b] conexao Outlook criada: {on}. Clicar em 'Fix connection' em Office 365 Outlook no mesmo portal.")
            if a.no_wait: sys.exit(2)
            input("     Enter quando estiver 'Connected'... ")
            _, oc = http("GET", f"{ob}/{on}?{urllib.parse.quote(flt, safe='=&$')}", tp)
        ot = oc["properties"]["testLinks"][0]["requestUri"]
        cfg["outlook_connection"] = oc["name"]; cfg["outlook_runtime"] = ot[: ot.index(oc["name"]) + len(oc["name"])]
        print(f"[5b] runtime Outlook: {cfg['outlook_runtime']}")
        json.dump(cfg, open(f"{d}/config.json", "w"), indent=1)  # regrava com o Outlook incluido

    if not os.path.exists(f"{d}/chats.json"):
        json.dump({"oneOnOne": [], "grupos": []}, open(f"{d}/chats.json", "w"))
    if a.default or not os.path.exists(f"{BASE}/default"):
        open(f"{BASE}/default", "w").write(a.profile)
    print("[6] montando cache de chats (le poucas mensagens de cada 1:1 para descobrir o nome)...")
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run([sys.executable, f"{here}/teams_pull.py", "--profile", a.profile, "--refresh"])
    print(f"\nperfil '{a.profile}' pronto. Uso: teams_pull.py --profile {a.profile} --from <nome>")

if __name__ == "__main__":
    main()
