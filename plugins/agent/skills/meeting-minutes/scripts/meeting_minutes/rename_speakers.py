#!/usr/bin/env python3
"""Reescreve a transcricao.md trocando SPEAKER_XX pelos nomes reais.

O whisperx entrega rotulos anonimos e costuma PARTIR um mesmo locutor em varios
(turnos curtos, backchannel, sobreposicao). Depois do passo 2 da skill — voiceprint
+ leitura — voce ja sabe quem e quem; este script aplica esse mapeamento:

  - varios rotulos podem apontar para o MESMO nome (junta o locutor partido);
  - turnos consecutivos que viram o mesmo nome sao fundidos num bloco so;
  - as estatisticas da tabela sao recalculadas DEPOIS da fusao, entao o % de cada
    pessoa passa a refletir a fala real dela, nao a fatia de um rotulo.

Rotulo nao mapeado e preservado como esta (e marcado se for fatia minima) — nao
inventa dono para fragmento que a voz nao identificou.

Uso:
  rename_speakers.py <workdir> SPEAKER_00=Ana SPEAKER_02=Bruno SPEAKER_01=Bruno ...

Le <workdir>/transcript.json (fonte da verdade, com os rotulos crus) e reescreve
<workdir>/transcricao.md. Nao toca no transcript.json — rodar de novo com outro
mapeamento sempre funciona, e o `run.sh`/make_transcript.py regenera o original.
"""
import json, os, sys
from collections import Counter

if len(sys.argv) < 3:
    sys.exit(__doc__)

WD = sys.argv[1]
mapa = dict(a.split("=", 1) for a in sys.argv[2:])

d = json.load(open(os.path.join(WD, "transcript.json"), encoding="utf-8"))
segs = [s for s in d["segments"] if s.get("text", "").strip()]
lang = d.get("language", "?")

def hhmmss(t):
    t = int(t)
    return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}" if t >= 3600 else f"{t//60:02d}:{t%60:02d}"

def nome(s):
    return mapa.get(s.get("speaker", "SEM_LOCUTOR"), s.get("speaker", "SEM_LOCUTOR"))

# ---- funde turnos consecutivos que agora sao a mesma pessoa ----
blocos, cur = [], None
for s in segs:
    n = nome(s)
    if cur and cur["nome"] == n:
        cur["end"] = s["end"]; cur["txt"].append(s["text"].strip())
    else:
        if cur: blocos.append(cur)
        cur = {"nome": n, "start": s["start"], "end": s["end"], "txt": [s["text"].strip()]}
if cur: blocos.append(cur)

# ---- estatisticas POS-fusao ----
# tempo somado SEGMENTO a segmento, nao pelo vao do bloco: o vao inclui as pausas
# internas e inflaria o total. Assim a tabela bate com speakers.json/oratoria.json.
dur, cnt, words = Counter(), Counter(), Counter()
for s in segs:
    n = nome(s)
    dur[n] += s["end"] - s["start"]
    words[n] += len(s["text"].split())
for b in blocos:
    cnt[b["nome"]] += 1   # "falas" = blocos apos a fusao
total = sum(dur.values()) or 1
noise = [n for n in dur if dur[n] / total < 0.01 and cnt[n] <= 5]

# ---- preserva o cabecalho (data/duracao) da transcricao.md ja existente ----
titulo = f"# Transcrição — {os.path.basename(WD).replace('ata_', '')}"
cab = f"**Idioma detectado:** `{lang}`"
antigo = os.path.join(WD, "transcricao.md")
if os.path.exists(antigo):
    linhas = open(antigo, encoding="utf-8").read().splitlines()
    if linhas and linhas[0].startswith("# "):
        titulo = linhas[0]
    for l in linhas[:6]:
        if l.startswith("**Data:**"):
            cab = l
            break

out = [titulo, "", cab, "",
       "> Locutores nomeados por impressão de voz (`voiceprint.py identify`) + leitura da"
       " transcrição. Rótulos que o whisperx partiu foram fundidos, e os percentuais abaixo"
       " já refletem a fusão.", "",
       "| Locutor | Tempo de fala | % | Falas |", "|---|---|---|---|"]
for n, _ in dur.most_common():
    flag = " ⚠️ (provável ruído)" if n in noise else ""
    out.append(f"| **{n}**{flag} | {dur[n]/60:.1f} min | {dur[n]/total*100:.1f}% | {cnt[n]} |")

if mapa:
    juntos = {}
    for rot, n in mapa.items():
        juntos.setdefault(n, []).append(rot)
    det = "; ".join(f"**{n}** = {', '.join(sorted(r))}" for n, r in sorted(juntos.items()))
    out += ["", f"*Mapeamento aplicado: {det}.*"]

out += ["", "---", ""]
for b in blocos:
    out.append(f"**[{hhmmss(b['start'])}] {b['nome']}:** " + " ".join(b["txt"]))
    out.append("")

open(antigo, "w", encoding="utf-8").write("\n".join(out))

print(f"[rename_speakers] {len(segs)} segmentos → {len(blocos)} blocos; {len(dur)} locutores")
for n, _ in dur.most_common():
    print(f"  {n:22s} {dur[n]/60:5.1f} min ({dur[n]/total*100:4.1f}%) {cnt[n]:4d} falas"
          + ("  ⚠️ provável ruído" if n in noise else ""))
