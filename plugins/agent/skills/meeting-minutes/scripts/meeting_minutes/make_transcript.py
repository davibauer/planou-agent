#!/usr/bin/env python3
"""Converte a saida JSON do whisperx (com diarizacao) em:
  - transcricao.md : turnos agrupados por locutor, com timestamps clicaveis
  - speakers.json  : estatisticas por locutor + candidatos a ruido + pistas de nome

Uso: make_transcript.py <workdir> <video_basename> <data> <duracao>
Le <workdir>/transcript.json e escreve em <workdir>/.
"""
import json, os, re, sys
from collections import Counter

WD, VIDEO, DATA, DUR = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
d = json.load(open(os.path.join(WD, "transcript.json"), encoding="utf-8"))
segs = [s for s in d["segments"] if s.get("text", "").strip()]
lang = d.get("language", "?")

def hhmmss(t):
    t = int(t)
    return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}" if t >= 3600 else f"{t//60:02d}:{t%60:02d}"

# ---- estatisticas por locutor ----
dur, cnt, words = Counter(), Counter(), Counter()
for s in segs:
    sp = s.get("speaker", "SEM_LOCUTOR")
    dur[sp] += s["end"] - s["start"]; cnt[sp] += 1; words[sp] += len(s["text"].split())
total = sum(dur.values()) or 1

# locutor com <1% do tempo E <=5 falas provavelmente e ruido/atribuicao errada
noise = [sp for sp in dur if dur[sp] / total < 0.01 and cnt[sp] <= 5]

# ---- pistas de nome: vocativos ("Hey Bruno,", "Thanks, Ana") no texto DIRIGIDO a alguem ----
# quem e chamado por um nome tende a ser o *outro* locutor do turno adjacente.
# o gatilho é case-insensitive ("Hey"/"hey"), mas o nome tem de vir capitalizado
NAME = re.compile(r"\b(?i:hey|hi|hello|ok|okay|thanks|thank you|yeah|yes|so|well|bom dia|boa tarde|bem|oi|olá|obrigado|valeu)[,\s]+([A-Z][a-zà-ú]{2,})\b")
hints = {}
for i, s in enumerate(segs):
    for m in NAME.finditer(s["text"]):
        name = m.group(1)
        spk = s.get("speaker")           # quem FALA o nome
        hints.setdefault(name, Counter())[spk] += 1

speakers = {
    sp: {
        "fala_min": round(dur[sp] / 60, 1),
        "pct": round(dur[sp] / total * 100, 1),
        "falas": cnt[sp],
        "palavras": words[sp],
        "provavel_ruido": sp in noise,
    } for sp in dur
}
json.dump(
    {"idioma": lang, "locutores": speakers,
     "nomes_citados": {n: dict(c) for n, c in hints.items()},
     "dica": "quem é chamado pelo nome costuma ser o OUTRO locutor, não quem fala"},
    open(os.path.join(WD, "speakers.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# ---- transcricao.md ----
out = [f"# Transcrição — {VIDEO}", ""]
out.append(f"**Data:** {DATA} · **Duração:** {DUR} · **Idioma detectado:** `{lang}`")
out.append("")
out.append("| Locutor | Tempo de fala | % | Falas |")
out.append("|---|---|---|---|")
for sp, _ in dur.most_common():
    flag = " ⚠️ (provável ruído)" if sp in noise else ""
    out.append(f"| `{sp}`{flag} | {dur[sp]/60:.1f} min | {dur[sp]/total*100:.1f}% | {cnt[sp]} |")
out += ["", "---", ""]

cur, buf, st = None, [], 0.0
def flush():
    if buf:
        out.append(f"**[{hhmmss(st)}] {cur}:** " + " ".join(buf))
        out.append("")
for s in segs:
    sp = s.get("speaker", "SEM_LOCUTOR"); t = s["text"].strip()
    if sp != cur:
        flush(); cur, buf, st = sp, [t], s["start"]
    else:
        buf.append(t)
flush()
open(os.path.join(WD, "transcricao.md"), "w", encoding="utf-8").write("\n".join(out))

print(f"[make_transcript] idioma={lang} locutores={len(dur)} segmentos={len(segs)}")
for sp, _ in dur.most_common():
    print(f"  {sp}: {dur[sp]/60:5.1f} min ({dur[sp]/total*100:4.1f}%) {cnt[sp]:4d} falas"
          + ("  ⚠️ provável ruído" if sp in noise else ""))
if hints:
    print("  nomes citados (por quem fala):", {n: dict(c) for n, c in hints.items()})
