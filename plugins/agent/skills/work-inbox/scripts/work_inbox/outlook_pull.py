#!/usr/bin/env python3
"""Le e-mails do Outlook (Office 365) do usuario pelo runtime do conector do Power Automate — mesmo perfil/token do Teams.

Uso:
  outlook_pull.py                              # ultimos 20 da Inbox (cabecalhos + preview)
  outlook_pull.py --from maria                 # remetente (trecho do nome ou e-mail)
  outlook_pull.py --subject "GMUD" -n 50       # filtro de assunto
  outlook_pull.py --search "PR 42631"          # busca (KQL do Outlook: texto, from:, hasattachment:yes ...)
  outlook_pull.py --unread                     # so nao lidos
  outlook_pull.py --folder "Caixa de Entrada/Projetos"
  outlook_pull.py --since 2d                   # recorte temporal (aplicado apos buscar); 3du = 3 dias uteis
  outlook_pull.py --digest --since 3du         # triagem: pessoas x notificacoes, agrupado por assunto/PR
  outlook_pull.py --read <id>                  # corpo completo de um e-mail (texto)
  outlook_pull.py --json                       # cru normalizado
  outlook_pull.py --profile <cliente>          # outra conta/tenant
"""
import argparse, html, json, re, sys, urllib.parse, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import teams_pull as tp  # perfil, cfg(), token(), parse_since, sem_acento
import remetentes

def rt():
    c = tp.cfg()
    if not c.get("outlook_runtime"):
        sys.exit(f"Perfil '{tp.PERFIL}' sem conexao Outlook. Criar a conexao 'Office 365 Outlook' em make.powerautomate.com e gravar 'outlook_connection'/'outlook_runtime' no config (teams_setup.py --outlook faz isso).")
    return c["outlook_runtime"]

def get(path):
    req = urllib.request.Request(rt() + path, headers={"Authorization": "Bearer " + tp.token()})
    try:
        return tp.abre(req, 180)
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode(errors="replace")
        if e.code in (401, 403) and ("authenticated" in body.lower() or "token not found" in body.lower()):
            sys.exit("Conexao Outlook nao autenticada: abrir make.powerautomate.com > Connections > Office 365 Outlook > Fix connection.")
        sys.exit(f"HTTP {e.code} em {path}: {body}")

def limpa(t):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", t or "", flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()

def norm(m):
    return {"id": m.get("id"), "quando": m.get("receivedDateTime"), "de": m.get("from"), "para": m.get("toRecipients"),
            "assunto": m.get("subject"), "preview": (m.get("bodyPreview") or "").strip(), "lido": m.get("isRead"),
            "anexos": m.get("hasAttachments"), "importancia": m.get("importance"), "conversa": m.get("conversationId")}

# automatic senders by the address only: remetentes.py (copied from the agent plugin by tools/sync_shared.py; the gmail
# source uses the same rules). AUTOMATICOS is the old pattern, kept only for an older agent plugin that still reads it
AUTOMATICOS = re.compile(r"azuredevops@|comunicacao@|comunicado|marketing@|rh@|no-?reply|noreply|notification|notifications@|donotreply|mailer-daemon|newsletter|@e\.|alerts?@|freshservice|servicedesk|jira@|github\.com|atlassian", re.I)
automatico = remetentes.outlook

def _h(m):
    return datetime.fromisoformat(m["quando"].replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M") if m["quando"] else "??"

def digest(msgs, as_json):
    pessoas = [m for m in msgs if not automatico(m["de"])]
    autos = [m for m in msgs if automatico(m["de"])]
    # agrupa notificacoes por chave: numero de PR/work item no assunto, senao assunto sem prefixos
    def chave(m):
        a = m["assunto"] or ""
        k = re.search(r"\b(\d{5,6})\b", a) or re.search(r"\b(?!20[2-3]\d\b)(\d{4})\b", a)  # id de PR/card, nunca um ano
        return f"#{k.group(1)}" if k else re.sub(r"^(re|fw|fwd|enc):\s*", "", a, flags=re.I).strip().lower()[:70]
    grupos = {}
    for m in autos:
        grupos.setdefault(chave(m), []).append(m)
    if as_json:
        print(json.dumps({"pessoas": pessoas, "notificacoes": {k: v for k, v in grupos.items()}}, ensure_ascii=False)); return
    print(f"{len(msgs)} e-mails | {sum(1 for m in msgs if not m['lido'])} nao lidos | {len(pessoas)} de pessoas | {len(autos)} automaticos em {len(grupos)} assuntos\n")
    print(f"== PESSOAS ({len(pessoas)})" + ("" if pessoas else " — nenhum"))
    for m in pessoas:
        tipo = " [convite de reunião]" if "Reunião do Microsoft Teams" in m["preview"] or "Microsoft Teams meeting" in m["preview"] else ""
        print(f"[{_h(m)}] {'●' if not m['lido'] else ' '} {m['de']} — {m['assunto']}{tipo}\n    {re.sub(chr(10), ' ', m['preview'])[:200]}\n    id: {m['id']}")
    print(f"\n== NOTIFICACOES ({len(autos)})")
    for k, v in sorted(grupos.items(), key=lambda kv: kv[1][-1]["quando"] or "", reverse=True):
        v.sort(key=lambda m: m["quando"] or "")
        titulo = re.sub(r"^PR - ", "", v[-1]["assunto"] or "")
        print(f"\n{k}  ({len(v)}x, {_h(v[0])} → {_h(v[-1])})  {titulo[:110]}")
        for m in v:
            ev = re.sub(r"\s+", " ", m["preview"]).split(" Azure DevOps ")[0]
            print(f"    {_h(m)[6:]}  {ev[:120]}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="de"); ap.add_argument("--subject"); ap.add_argument("--search")
    ap.add_argument("--unread", action="store_true"); ap.add_argument("--folder", default="Inbox")
    ap.add_argument("-n", type=int, default=20); ap.add_argument("--since"); ap.add_argument("--read")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--profile")
    ap.add_argument("--digest", action="store_true", help="triagem agrupada (usar com --since)")
    a = ap.parse_args()
    c = tp.cfg()
    print(f"-- perfil {tp.PERFIL} [{tp.PERFIL_ORIGEM}]: {c.get('conta','?')} — Outlook", file=sys.stderr)

    if a.read:
        m = get(f"/v2/Mail/{urllib.parse.quote(a.read, safe='')}?includeAttachments=false")
        if a.json: print(json.dumps(m, ensure_ascii=False)); return
        print(f"De: {m.get('from')}\nPara: {m.get('toRecipients')}\nCc: {m.get('ccRecipients') or ''}\nData: {m.get('receivedDateTime')}\nAssunto: {m.get('subject')}\n")
        print(limpa(m.get("body")) if (m.get("bodyType") or "").lower() != "text" else m.get("body")); return

    q = {"folderPath": a.folder, "top": min(a.n, 250), "fetchOnlyUnread": str(a.unread).lower(), "includeAttachments": "false"}
    por_nome = bool(a.de) and "@" not in a.de
    if a.de and not por_nome: q["from"] = a.de          # e-mail exato: filtro do servidor
    if por_nome: q["top"] = min(max(a.n * 10, 100), 250)  # nome: pega mais e filtra localmente (KQL from: nao casa nome parcial)
    if a.subject: q["subjectFilter"] = a.subject
    busca = a.search or ""
    if a.since:  # recorte no servidor (KQL) — evita puxar 250 corpos para descartar a maioria
        desde = tp.parse_since(a.since)
        busca = (busca + f" received>={desde.astimezone().strftime('%Y-%m-%d')}").strip()
        if a.n == 250 and a.digest: q["top"] = 100
    if busca: q["searchQuery"] = busca
    q = {k: v for k, v in q.items() if v not in ("", None)}
    msgs = [norm(m) for m in get("/v3/Mail?" + urllib.parse.urlencode(q)).get("value", [])]
    if por_nome:
        termos = tp.sem_acento(a.de).split()
        msgs = [m for m in msgs if all(t in tp.sem_acento(str(m["de"])) for t in termos)][-a.n:]
    if a.since:
        desde = tp.parse_since(a.since)
        msgs = [m for m in msgs if m["quando"] and datetime.fromisoformat(m["quando"].replace("Z", "+00:00")) >= desde]
    msgs.sort(key=lambda m: m["quando"] or "")
    if a.digest:
        digest(msgs, a.json); return
    for m in msgs:
        if a.json: print(json.dumps(m, ensure_ascii=False)); continue
        q_ = datetime.fromisoformat(m["quando"].replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M") if m["quando"] else "??"
        flags = ("" if m["lido"] else "● ") + ("📎 " if m["anexos"] else "")
        print(f"[{q_}] {flags}{m['de']} — {m['assunto']}\n    {m['preview'][:220].replace(chr(10),' ')}\n    id: {m['id']}")

if __name__ == "__main__":
    main()
