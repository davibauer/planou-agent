#!/usr/bin/env python3
"""Corrige o campo `language` do transcript.json quando a auto-deteccao do whisperx
erra — o que acontece MUITO com daily de time brasileiro (rotula 'en' fala pt-BR).

O rotulo errado envenena tudo a jusante: prosody.py escolhe a regex de muletas/
inseguranca pelo idioma, e pronunciation.py so roda se for 'en'. Uma daily em portugues
marcada 'en' recebia analise fonetica de ingles (sem sentido) e muletas zeradas.

Sniff por stopwords muito marcadas de cada idioma (nao ambiguas). Barato, sem rede.
Uso: fixlang.py <transcript.json>  -> reescreve o campo language se o texto discordar.
Imprime: <antes> -> <depois>  (ou 'ok' se ja estava certo).
"""
import json, re, sys

PT = set("que não nao né então gente pra tá isso já porque uma mas cara acho beleza a o e de "
         "com para você vou tem fazer coisa aqui agora".split())
EN = set("the and to of is are you that this with for have will would there their about "
         "what when been they're we're going".split())

path = sys.argv[1]
d = json.load(open(path, encoding="utf-8"))
lab = str(d.get("language", "?"))
words = re.findall(r"[a-zà-ú']+", " ".join(s.get("text", "") for s in d["segments"]).lower())
pt = sum(1 for w in words if w in PT)
en = sum(1 for w in words if w in EN)
tot = pt + en

if tot < 20:
    print(f"{lab} -> {lab} (amostra pequena, não mexo)")
    sys.exit(0)

ptp = 100 * pt / tot
real = "pt" if ptp > 65 else "en" if ptp < 35 else None   # zona cinzenta (mista): nao força

if real and real != lab:
    d["language"] = real
    d["_language_original"] = lab          # deixa rastro do que o whisperx tinha dito
    json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{lab} -> {real}  (texto {ptp:.0f}% pt)")
else:
    print(f"{lab} -> {lab} (ok, texto {ptp:.0f}% pt)")
