#!/usr/bin/env python3
"""Le o feed de mensagens do Teams gravado pelo flow do Power Automate (um JSON por mensagem).

Uso:
  teams_feed.py                 # ultimas 20 mensagens
  teams_feed.py -n 50           # ultimas 50
  teams_feed.py --since 2h      # desde 2 horas atras (tambem 30m, 1d, ou ISO 2026-09-17T10:00)
  teams_feed.py --chat <trecho> # so o chat cujo id/topico contem o trecho
  teams_feed.py --json          # saida crua normalizada (uma linha por mensagem)
  teams_feed.py --watch         # fica aguardando e imprime cada mensagem nova

Pasta: env TEAMS_FEED_DIR, senao o primeiro que existir em CANDIDATOS.
"""
import re, argparse, glob, html, json, os, re, sys, time
from datetime import datetime, timedelta, timezone

CANDIDATOS = [
    os.environ.get("TEAMS_FEED_DIR"),
    *glob.glob("/mnt/*/Users/*/OneDrive - */TeamsFeed"),     # WSL: OneDrive do Windows
    *glob.glob("/mnt/*/Users/*/OneDrive/TeamsFeed"),
]

def pasta():
    for p in CANDIDATOS:
        if p and os.path.isdir(p):
            return p
    sys.exit("Pasta do feed nao encontrada. Defina TEAMS_FEED_DIR ou crie /TeamsFeed no OneDrive sincronizado.")

def texto(body):
    if body.get("plainTextContent"):
        return body["plainTextContent"].strip()
    t = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        t = re.sub(r"<br\s*/?>|</p>|</div>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", "", t)
        t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()

IMG = r'<img[^>]+src="[^"]*?/hostedContents/([^/"]+)/\$value"'
_NOMES = {}


def _nome_por_id(uid):
    """Nome de quem escreveu a mensagem ORIGINAL de um encaminhamento: o Graph manda so o id (displayName null).
    O cache de chats do perfil (chats.json) tem os 1:1: o id do outro participante esta no proprio id do chat."""
    if not uid: return None
    if not _NOMES:
        try:
            import inbox_paths
            perfil = inbox_paths.env_profile()
            cache = json.load(open(os.path.join(inbox_paths.base(), 'profiles', perfil, 'chats.json'), encoding='utf-8'))
            for c in cache.get('oneOnOne') or []:
                for parte in re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', c.get('id') or ''):
                    _NOMES.setdefault(parte, []).append(c.get('nome'))
        except Exception:
            _NOMES['_'] = []
    nomes = [n for n in _NOMES.get(uid) or [] if n and n != '(sem autor)']
    return nomes[0] if len(set(nomes)) == 1 else None      # o meu proprio id aparece em todos: ambiguo, fica sem nome


def _encaminhadas(m):
    """Anexos forwardedMessageReference: texto, autor, hora e prints da mensagem original. Sem isto o encaminhamento
    chegava vazio (25/09/2026: credenciais do Cognito encaminhadas pelo Yanes nao apareceram no tick)."""
    out = []
    for a in m.get("attachments") or []:
        if a.get("contentType") != "forwardedMessageReference": continue
        try: c = json.loads(a.get("content") or "{}")
        except ValueError: continue
        html_orig = c.get("originalMessageContent") or ""
        u = (c.get("originalMessageSender") or {}).get("user") or {}
        quem = u.get("displayName") or _nome_por_id(u.get("id")) or "outra conversa"
        quando = c.get("originalSentDateTime") or ""
        try: q = datetime.fromisoformat(quando.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M")
        except ValueError: q = "?"
        out.append({"de": quem, "quando": q, "texto": texto({"content": html_orig, "contentType": "html"}),
                    "imagens": re.findall(IMG, html_orig)})
    return out


def normaliza(raw, arquivo):
    # o flow grava triggerBody() inteiro; tolera tanto o objeto direto quanto {"body": {...}}
    m = raw.get("body") if isinstance(raw.get("body"), dict) and "content" not in raw.get("body", {}) else raw
    if "body" in m and isinstance(m["body"], dict) and "content" in m["body"]:
        corpo = m["body"]
    else:
        corpo = {"content": m.get("content"), "contentType": m.get("contentType"),
                 "plainTextContent": m.get("plainTextContent")}
    de = (m.get("from") or {}).get("user") or (m.get("from") or {}).get("application") or {}
    fw = _encaminhadas(m)
    txt = texto(corpo)
    for e in fw:
        txt = (txt + "\n" if txt else "") + f"[encaminhada de {e['de']} · {e['quando']}] {e['texto']}"
    return {
        "id": m.get("id"),
        "quando": m.get("createdDateTime"),
        "chat": m.get("conversationId") or m.get("chatId") or (m.get("channelIdentity") or {}).get("channelId"),
        "de": de.get("displayName") or "?",
        "texto": txt,
        "anexos": [a.get("name") for a in (m.get("attachments") or []) if a.get("name")],
        # arquivo anexado (OneDrive/SharePoint de quem mandou): nome + url, para o agent guardar uma copia
        "arquivos": [{"nome": a.get("name"), "url": a.get("contentUrl")} for a in (m.get("attachments") or [])
                     if a.get("contentType") == "reference" and a.get("name") and a.get("contentUrl")],
        # prints colados: <img src=".../hostedContents/<id>/$value"> — o conector nao serve o binario; a sessao do Teams web
        # baixa (teams_web.baixar_imagem). Aqui so' os ids, para o agent decidir se baixa.
        "imagens": re.findall(IMG, (corpo.get("content") or "") if corpo.get("contentType") == "html" else "") + [i for e in fw for i in e["imagens"]],
        "arquivo": os.path.basename(arquivo),
    }

def carrega(dir_):
    msgs = []
    for f in glob.glob(os.path.join(dir_, "**", "*.json"), recursive=True):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                msgs.append(normaliza(json.load(fh), f))
        except Exception as e:  # arquivo ainda sincronizando ou lixo
            print(f"[aviso] {os.path.basename(f)}: {e}", file=sys.stderr)
    msgs.sort(key=lambda m: (m["quando"] or "", m["id"] or ""))
    return msgs

def parse_since(s):
    m = re.fullmatch(r"(\d+)(du|bd)", s)  # dias uteis: 3du = hoje + 2 dias uteis anteriores, desde 00:00 local
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
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def quando_dt(m):
    try:
        return datetime.fromisoformat((m["quando"] or "").replace("Z", "+00:00"))
    except ValueError:
        return None

def imprime(m, as_json):
    if as_json:
        print(json.dumps(m, ensure_ascii=False)); return
    q = quando_dt(m)
    hora = q.astimezone().strftime("%d/%m %H:%M") if q else "??/?? ??:??"
    anexos = f"  [anexos: {', '.join(m['anexos'])}]" if m["anexos"] else ""
    print(f"[{hora}] {m['de']}: {m['texto']}{anexos}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--since")
    ap.add_argument("--chat")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()
    d = pasta()
    vistos = set()

    def filtra(msgs):
        out = []
        desde = parse_since(a.since) if a.since else None
        for m in msgs:
            if a.chat and a.chat.lower() not in (m["chat"] or "").lower():
                continue
            if desde and (quando_dt(m) or datetime.min.replace(tzinfo=timezone.utc)) < desde:
                continue
            out.append(m)
        return out

    msgs = filtra(carrega(d))
    if not a.since and not a.watch:
        msgs = msgs[-a.n:]
    for m in msgs:
        vistos.add(m["arquivo"]); imprime(m, a.json)
    if not a.watch:
        return
    print(f"-- aguardando mensagens novas em {d} (Ctrl+C para sair)", file=sys.stderr)
    while True:
        time.sleep(3)
        for m in filtra(carrega(d)):
            if m["arquivo"] not in vistos:
                vistos.add(m["arquivo"]); imprime(m, a.json); sys.stdout.flush()

if __name__ == "__main__":
    main()
