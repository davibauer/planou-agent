#!/usr/bin/env python3
"""Checagem de pronuncia (ingles) de UM locutor, por COMPARACAO com os demais.

Uso: pronunciation.py <workdir> <video_ou_wav> <SPEAKER_ID_DO_USUARIO> [config.json]
Le  <workdir>/transcript.json ; escreve <workdir>/pronuncia.json

POR QUE COMPARATIVO (aprendido na marra, medido em 13/07/2026):
o modelo fonetico CTC tem vies proprio por fonema — num audio de reuniao ele so
"ouve" o /ŋ/ de -ing em ~60% das vezes e o /æ/ em ~30%, MESMO num falante nativo.
Se voce comparar a taxa do usuario contra um limiar fixo (ou contra os sons
faceis dele), acusa erro em -ING, /æ/ e R que sao puro artefato do modelo.
Medido nessa reuniao: o usuario 34% de TH vs 63% do entrevistador nativo (lacuna real),
mas -ING 60% vs 62% e /æ/ 51% vs 29% (o nativo pontuou PIOR) — ou seja, ruido.

A taxa do OUTRO locutor no MESMO fonema e no MESMO audio cancela esse vies:
so a DIFERENCA e reportada. Sem outro locutor com fala suficiente, nao se reporta
nada — silencio e melhor que erro inventado.
Premissa (declarada na saida): assume-se que o locutor de referencia pronuncia
aquele som corretamente. Se a reuniao for so de brasileiros, nenhuma lacuna
aparece — falha segura.
"""
import json, os, re, subprocess, sys, tempfile

WD, SRC, SPK = sys.argv[1], sys.argv[2], sys.argv[3]
CFG = json.load(open(sys.argv[4], encoding="utf-8")) if len(sys.argv) > 4 else {}
GAP_MIN = 15          # pontos abaixo da referencia para virar "lacuna"
MIN_WORDS = 25        # palavras validas minimas para um grupo valer
MIN_FALA_S = 120      # locutor precisa falar >=2 min para servir de referencia

d = json.load(open(os.path.join(WD, "transcript.json"), encoding="utf-8"))
LANG = str(d.get("language", ""))
OUT = os.path.join(WD, "pronuncia.json")

if not LANG.startswith("en"):
    json.dump({"pulado": f"idioma detectado = '{LANG}', não é inglês"},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    sys.exit(print(f"[pronuncia] idioma={LANG} — não é inglês, nada a fazer.") or 0)

segs = [s for s in d["segments"] if s.get("text", "").strip()]
by_spk = {}
for s in segs:
    by_spk.setdefault(s.get("speaker", "SEM_LOCUTOR"), []).append(s)
if SPK not in by_spk:
    sys.exit(f"❌ locutor {SPK} não está no transcript.json (tem: {list(by_spk)})")

fala = {sp: sum(s["end"] - s["start"] for s in sl) for sp, sl in by_spk.items()}
refs = [sp for sp in by_spk if sp != SPK and fala[sp] >= MIN_FALA_S]


def hms(t):
    t = int(max(0, t))
    return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}" if t >= 3600 else f"{t//60:02d}:{t%60:02d}"


def words_of(sl):
    """TODAS as palavras com timestamp — sem filtro de duração.

    O filtro de duração é de cada sonda, NÃO daqui: a checagem de fonema quer
    palavra longa (>0.18s) para o CTC ter o que ouvir, mas a de redução vocálica
    quer justamente a palavra CURTA — 'and' reduzido dura 100 ms. Filtrar aqui
    media redução vocálica excluindo as palavras reduzidas (bug real, 14/07/2026:
    reportou átona de 260 ms quando a mediana verdadeira é 121 ms).
    """
    ws = []
    for s in sl:
        for w in s.get("words", []):
            if "start" in w and "end" in w and w["end"] > w["start"]:
                t = re.sub(r"[^a-z']", "", w.get("word", "").lower())
                if t:
                    ws.append({"t": t, "start": w["start"], "end": w["end"]})
    return ws


tmp, wav = None, SRC
if not SRC.lower().endswith(".wav"):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); tmp.close(); wav = tmp.name
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", SRC,
                    "-map", CFG.get("audio_stream", "0:a:0"), "-ac", "1", "-ar", "16000", wav], check=True)

import torch, torchaudio
from transformers import AutoProcessor, AutoModelForCTC

dev = "cuda" if torch.cuda.is_available() else "cpu"
name = CFG.get("phoneme_model", "vitouphy/wav2vec2-xls-r-300m-timit-phoneme")
proc = AutoProcessor.from_pretrained(name)
model = AutoModelForCTC.from_pretrained(name).to(dev).eval()
audio, sr = torchaudio.load(wav)


def phon(a, b, pad=0.08):
    s0 = max(0, int((a - pad) * sr)); e0 = int((b + pad) * sr)
    if e0 - s0 < 400:
        return ""
    iv = proc(audio[0, s0:e0].numpy(), sampling_rate=sr, return_tensors="pt").input_values.to(dev)
    with torch.no_grad():
        return proc.batch_decode(torch.argmax(model(iv).logits, -1))[0]


# alvos: tropecos classicos de brasileiro. curados onde a ortografia engana
# (o "i" de 'like' e aɪ; o de 'it' e ɪ) — filtro por lista, nao por letra.
IH = {"it", "is", "this", "in", "if", "his", "him", "big", "did", "give", "live", "little", "list",
      "which", "will", "still", "think", "thing", "things", "different", "since", "minute", "minutes",
      "six", "fix", "build", "built", "system", "quick", "ship", "bit", "business", "into", "with"}
AE = {"back", "bad", "man", "can", "have", "that", "than", "thanks", "happen", "happens", "app", "apps",
      "actually", "add", "adding", "match", "matter", "handle", "map", "cache", "and", "answer",
      "packages", "package", "practice", "manage", "channel", "channels", "as", "at", "after"}
GRUPOS = {
    "TH (θ/ð)":      (lambda t: "th" in t and len(t) >= 3, {"θ", "ð"},
                      "língua entre os dentes; sem isso 'think' vira 'sink/tink' e 'the' vira 'de'"),
    "-ING (ŋ)":      (lambda t: t.endswith("ing") and len(t) >= 5, {"ŋ"},
                      "termine no nasal /ŋ/, não em 'in' nem em 'ingui'"),
    "/ɪ/ curto":     (lambda t: t in IH, {"ɪ"},
                      "'it/this/big' têm /ɪ/ curto, não o /i/ longo do português — 'ship' ≠ 'sheep'"),
    "/æ/":           (lambda t: t in AE, {"æ"},
                      "'back/man/have' têm /æ/, vogal aberta que não existe no português"),
    "R inicial (ɹ)": (lambda t: t.startswith("r") and len(t) >= 3, {"ɹ"},
                      "'right/really' começam com /ɹ/ (língua enrolada), não com o R do português"),
}
CONTROLE = (lambda t: ("m" in t or "n" in t or "s" in t) and len(t) >= 3, {"m", "n", "s"})

# --- reducao vocalica / connected speech -------------------------------------
# O que mais entrega o brasileiro nao e um fonema, e o RITMO: nativo esmaga a
# palavra atona ("and" -> /ən/ em 60 ms) e so alonga a tonica. Brasileiro articula
# "ÆND" por inteiro, e o resultado soa silabado mesmo com cada som correto.
# (Descoberto em 13/07/2026: o usuario "ganhou" do entrevistador nativo no /æ/
# justamente por NAO reduzir — taxa alta ali era sintoma, nao elogio.)
REDUZ = {"and", "can", "that", "of", "for", "to", "at", "as", "was", "from", "the", "a", "an",
         "but", "or", "them", "than", "would", "could", "should", "been", "are", "you", "your",
         "some", "us", "him", "her", "had", "has", "do", "does", "just", "were", "what", "there"}
SCHWA = {"ə", "ɝ"}
MIN_DUR_RED = 0.06     # palavra reduzida e CURTA: o filtro de 0.18s excluiria justamente ela


def reducao(ws):
    """Dois sinais de connected speech. O de duracao NAO usa modelo nenhum.

    A comparacao entre locutores e feita PALAVRA A PALAVRA ('the' vs 'the'), nao
    por razao atona/conteudo: eles falam palavras diferentes, entao o denominador
    de conteudo inflava sozinho e a razao dizia o oposto da verdade (14/07/2026:
    razao dizia que o usuario reduzia MAIS que o nativo; palavra a palavra mostrou
    que os dois estao empatados — 121 ms vs 120 ms).
    """
    import statistics
    fw = [w for w in ws if w["t"] in REDUZ and (w["end"] - w["start"]) > MIN_DUR_RED]
    if len(fw) < 30:
        return None
    per = {}
    for w in fw:
        per.setdefault(w["t"], []).append(w["end"] - w["start"])
    por_palavra = {t: {"med_ms": round(statistics.median(v) * 1000), "n": len(v)}
                   for t, v in per.items() if len(v) >= 5}

    # schwa: o modelo ouviu vogal neutra na atona? (so em palavras que o CTC consegue ler)
    val = hit = 0; miss = []
    for w in [w for w in fw if (w["end"] - w["start"]) > 0.12][:200]:
        p = phon(w["start"], w["end"], pad=0.05)
        if not p.strip():
            continue
        val += 1
        if any(s in p for s in SCHWA):
            hit += 1
        elif len(miss) < 12:
            miss.append({"palavra": w["t"], "t": round(w["start"], 2), "hms": hms(w["start"]),
                         "dur_ms": round((w["end"] - w["start"]) * 1000), "modelo_ouviu": p.strip()[:16]})
    return {"schwa_pct": round(100 * hit / val) if val else None, "n_schwa_testadas": val,
            "dur_atona_mediana_ms": round(statistics.median(w["end"] - w["start"] for w in fw) * 1000),
            "n_atonas": len(fw), "por_palavra": por_palavra, "nao_reduzidas": miss}


MIN_DUR_FON = 0.18     # palavra curta demais: o CTC nao tem o que ouvir


def probe(ws, filt, targets, maxn=220):
    val, hit, miss = 0, 0, []
    for w in [w for w in ws if filt(w["t"]) and (w["end"] - w["start"]) > MIN_DUR_FON][:maxn]:
        p = phon(w["start"], w["end"])
        if not p.strip():
            continue
        val += 1
        if any(t in p for t in targets):
            hit += 1
        else:
            miss.append({"palavra": w["t"], "t": round(w["start"], 2), "hms": hms(w["start"]),
                         "modelo_ouviu": p.strip()[:24]})
    return {"pct": round(100 * hit / val) if val else None, "n": val, "miss": miss}


words = {sp: words_of(by_spk[sp]) for sp in [SPK] + refs}
ctrl = {sp: probe(words[sp], *CONTROLE) for sp in words}
c_user = ctrl[SPK]
# porta de qualidade do audio: se o modelo erra ate m/n/s DESTE locutor, ele nao
# tem autoridade para dizer que o /θ/ saiu errado.
audio_ok = c_user["n"] >= 40 and (c_user["pct"] or 0) >= 80

res = {"locutor": SPK, "idioma": LANG,
       "metodo": "comparativo: a taxa de cada som é comparada à dos outros locutores DESTA gravação, "
                 "o que cancela o viés do modelo fonético. Só a diferença é reportada.",
       "premissa": "assume-se que o locutor de referência pronuncia o som corretamente — "
                   "ele pode não ser nativo. Confirme ouvindo o vídeo.",
       "controle_audio": {"sons_faceis_pct": c_user["pct"], "palavras": c_user["n"], "ok": audio_ok},
       "referencias": {sp: round(fala[sp] / 60, 1) for sp in refs}}

if not audio_ok:
    res["conclusao"] = (f"Áudio não permite isolar pronúncia com confiança (modelo acertou "
                        f"{c_user['pct']}% dos sons fáceis). Nada reportado.")
    print(f"[pronuncia] controle {c_user['pct']}% → NÃO confiável; não reporte pronúncia.")
elif not refs:
    res["conclusao"] = ("Nenhum outro locutor com fala suficiente (≥2 min) para servir de referência. "
                        "Sem referência não dá para separar erro seu do viés do modelo — nada reportado.")
    print("[pronuncia] sem locutor de referência → não reporte pronúncia.")
else:
    grupos, exemplos = {}, []
    print(f"[pronuncia] controle {c_user['pct']}% ({c_user['n']} palavras) | referências: {refs}")
    for nome, (filt, tg, dica) in GRUPOS.items():
        u = probe(words[SPK], filt, tg)
        rr = {sp: probe(words[sp], filt, tg) for sp in refs}
        val = {sp: r["pct"] for sp, r in rr.items() if r["n"] >= MIN_WORDS and r["pct"] is not None}
        if u["n"] < MIN_WORDS or not val:
            grupos[nome] = {"voce_pct": u["pct"], "n": u["n"], "obs": "ocorrências insuficientes (suas ou da referência)"}
            print(f"  {nome:16s} {str(u['pct']):>4}% (n={u['n']:3d})  — sem base para comparar")
            continue
        best = max(val, key=lambda s: val[s])
        gap = val[best] - u["pct"]
        lacuna = gap >= GAP_MIN
        grupos[nome] = {"voce_pct": u["pct"], "n": u["n"], "referencia_pct": val[best],
                        "referencia": best, "gap": gap, "lacuna": lacuna, "dica": dica,
                        "todas_referencias": {sp: val[sp] for sp in val}}
        if lacuna:
            for m in u["miss"][:8]:
                exemplos.append({"grupo": nome, **m})
        print(f"  {nome:16s} você {u['pct']:3d}% (n={u['n']:3d}) vs ref {val[best]:3d}% "
              f"→ gap {gap:+3d}  {'⚠️ LACUNA REAL' if lacuna else 'sem lacuna (viés do modelo)'}")
    res["grupos"] = grupos
    res["exemplos_para_ouvir"] = exemplos[:30]

    # --- redução vocálica (ritmo da frase, não fonema solto) ---
    ru = reducao(words[SPK])
    rr_ = {sp: r for sp in refs if (r := reducao(words[sp]))}
    if ru and rr_:
        ref = max(rr_, key=lambda s: rr_[s]["schwa_pct"] or 0)
        rf = rr_[ref]
        gap_schwa = (rf["schwa_pct"] or 0) - (ru["schwa_pct"] or 0)

        # duração PALAVRA A PALAVRA: só as que os dois usam bastante. Controla vocabulário.
        comum = {t: {"voce_ms": ru["por_palavra"][t]["med_ms"], "ref_ms": rf["por_palavra"][t]["med_ms"],
                     "mais_longa_pct": round(100 * (ru["por_palavra"][t]["med_ms"] /
                                                    max(1, rf["por_palavra"][t]["med_ms"]) - 1))}
                 for t in ru["por_palavra"] if t in rf["por_palavra"]}
        # mediana do quanto suas átonas são mais longas que as MESMAS palavras na referência
        import statistics
        excesso = round(statistics.median([v["mais_longa_pct"] for v in comum.values()])) if comum else 0
        # lacuna se reduz menos: menos schwa OU átonas >25% mais longas que as mesmas da referência
        lacuna = gap_schwa >= GAP_MIN or excesso >= 25
        ru.update({"referencia": ref, "ref_schwa_pct": rf["schwa_pct"], "gap_schwa": gap_schwa,
                   "ref_dur_atona_mediana_ms": rf["dur_atona_mediana_ms"],
                   "comparacao_palavra_a_palavra": comum,
                   "atonas_mais_longas_que_a_referencia_pct": excesso, "lacuna": lacuna,
                   "dica": "nativo esmaga a átona ('and'→/ən/ em ~100 ms) e só alonga a tônica. "
                           "Articular a funcional inteira ('ÆND') é o que mais soa silabado, "
                           "mesmo com cada fonema certo.",
                   "leitura": "compare a MESMA palavra nos dois ('the' vs 'the'). Átona mais curta = melhor. "
                              "Não use razão átona/conteúdo: o vocabulário difere e a razão inverte o sinal. "
                              "O schwa_pct só mede átonas >120 ms (abaixo disso o CTC não tem o que ouvir), "
                              "ou seja, amostra as MENOS reduzidas: os absolutos não são a taxa real, "
                              "só o gap contra a referência (medida igual) vale.",
                   "como_ler_junto": "duração OK + schwa baixo = o problema é a QUALIDADE da vogal, não o ritmo: "
                                     "a átona tem a duração certa mas vem cheia em vez de neutra. "
                                     "Treino: esvaziar a vogal (and→/ənd/, that→/ðət/), não acelerar."})
        res["reducao_vocalica"] = ru
        print(f"  {'REDUÇÃO':16s} schwa {ru['schwa_pct']}% vs ref {rf['schwa_pct']}% | "
              f"átonas {ru['dur_atona_mediana_ms']}ms vs {rf['dur_atona_mediana_ms']}ms "
              f"({excesso:+d}% nas mesmas palavras) → {'⚠️ LACUNA (você reduz menos)' if lacuna else 'ok, você reduz como a referência'}")
    elif ru:
        res["reducao_vocalica"] = {**ru, "obs": "sem referência com átonas suficientes para comparar"}

    lac = [n for n, g in grupos.items() if g.get("lacuna")]
    if res.get("reducao_vocalica", {}).get("lacuna"):
        lac.append("redução vocálica (connected speech)")
    res["conclusao"] = ("Lacunas reais: " + ", ".join(lac) if lac else
                        "Nenhuma lacuna acima do ruído do modelo — sua pronúncia acompanha a referência nos sons testados.")

json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
if tmp:
    os.unlink(tmp.name)
print(f"→ {OUT}")
