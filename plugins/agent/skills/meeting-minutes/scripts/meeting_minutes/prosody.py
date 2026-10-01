#!/usr/bin/env python3
"""Prosodia por locutor (Praat/parselmouth) para diagnostico de oratoria.

Uso: prosody.py <workdir> <audio.wav>
Le  <workdir>/transcript.json (saida do whisperx, com diarizacao)
Escreve <workdir>/oratoria.json

Mede, por locutor: pitch (mediana em Hz, variacao em SEMITONS), dinamica de
volume (so intra-sessao), ritmo (WPM bruto e articulado), pausas, muletas, e
lista "momentos" com timestamp para o usuario ouvir o trecho no video.
"""
import json, os, re, sys
from collections import Counter
import numpy as np
import soundfile as sf
import parselmouth

WD, WAV = sys.argv[1], sys.argv[2]
d = json.load(open(os.path.join(WD, "transcript.json"), encoding="utf-8"))
LANG = d.get("language", "?")
segs = sorted([s for s in d["segments"] if s.get("text", "").strip()], key=lambda s: s["start"])

# Volume absoluto (dB) NAO e comparavel entre reunioes: muda com distancia do
# microfone e ganho. So a dinamica DENTRO da sessao tem sentido.
FAIXAS = {
    "pitch_sd_semitons": {"monotono": "< 2.0", "ok": "2.0 a 2.8", "expressivo": "> 2.8",
                          "nota": "desvio-padrao do F0 em semitons re mediana do proprio locutor"},
    "wpm_bruto": {"lento": "< 110", "ok": "110 a 160", "acelerado": "> 160",
                  "nota": "taxa de fala INCLUINDO as pausas — e esta que classifica o ritmo. "
                          "conversa normal 120-150 wpm"},
    "wpm_articulado": {"lento": "< 150", "ok": "150 a 210", "acelerado": "> 210",
                       "nota": "palavras/min DESCONTANDO as pausas. roda naturalmente mais alto que o bruto; "
                               "nao aplique as faixas de conversa aqui"},
    "pausa_ratio": {"nota": "% do tempo de fala que e silencio >=0.4s dentro dos turnos"},
    "volume_db": {"nota": "dinamica INTRA-sessao apenas; nao compare dB entre reunioes (muda com mic/ganho)"},
}

def hms(t):
    t = int(max(0, t))
    return f"{t//3600:02d}:{t%3600//60:02d}:{t%60:02d}" if t >= 3600 else f"{t//60:02d}:{t%60:02d}"

# ---------------------------------------------------------------- pitch/volume
# Processa em blocos: um WAV de 3h em float64 nao cabe confortavelmente na RAM.
CHUNK_S = 600
f0_t, f0_v, in_t, in_v = [], [], [], []
sr = sf.info(WAV).samplerate
with sf.SoundFile(WAV) as f:
    off = 0.0
    while True:
        blk = f.read(int(CHUNK_S * sr), dtype="float64")
        if len(blk) == 0:
            break
        if blk.ndim > 1:
            blk = blk.mean(axis=1)
        snd = parselmouth.Sound(blk, sampling_frequency=sr)
        try:
            p = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=400)
            v = p.selected_array["frequency"]; t = p.xs() + off
            m = v > 0                     # frames nao-vozeados vem como 0
            f0_t.append(t[m]); f0_v.append(v[m])
        except Exception:
            pass
        try:
            it = snd.to_intensity(minimum_pitch=75, time_step=0.01)
            v2 = np.asarray(it.values[0]); t2 = it.xs() + off
            m2 = np.isfinite(v2) & (v2 > 0)
            in_t.append(t2[m2]); in_v.append(v2[m2])
        except Exception:
            pass
        off += len(blk) / sr

f0_t = np.concatenate(f0_t) if f0_t else np.array([])
f0_v = np.concatenate(f0_v) if f0_v else np.array([])
in_t = np.concatenate(in_t) if in_t else np.array([])
in_v = np.concatenate(in_v) if in_v else np.array([])

# ---------------------------------------------------------------- por locutor
def merge(ivs):
    out = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out

def inside(ivs, times):
    """mascara booleana: quais `times` caem dentro de `ivs` (ordenado, sem overlap)"""
    if not len(times) or not ivs:
        return np.zeros(len(times), bool)
    st = np.array([a for a, b in ivs]); en = np.array([b for a, b in ivs])
    i = np.searchsorted(st, times, side="right") - 1
    ok = i >= 0
    res = np.zeros(len(times), bool)
    res[ok] = times[ok] <= en[i[ok]]
    return res

# muletas: whisper LIMPA boa parte das hesitacoes -> as contagens sao PISO, nao taxa real.
HES = {"pt": r"\b(é+é|ééé+|ahn+|hã+|hum+|hmm+|ahm+|eh+m?)\b",
       "en": r"\b(uh+|um+|er+|ah+|hmm+|mm+)\b"}
MUL = {"pt": r"\b(tipo|né|então|assim|sabe|entendeu|meio que|digamos|basicamente|literalmente|na verdade|beleza|cara|mano|ok)\b",
       "en": r"\b(like|you know|i mean|kind of|sort of|basically|actually|right|well|so)\b"}
L = "en" if str(LANG).startswith("en") else "pt"

# --- linguagem de inseguranca -------------------------------------------------
# Numa entrevista, isto derruba a percepcao de senioridade mais que sotaque:
# nao e o QUE voce diz, e o quanto voce se protege antes de dizer.
# So conta como problema por COMPARACAO com os outros locutores da mesma call —
# uma taxa de "I think" isolada nao diz nada; o dobro da do entrevistador, sim.
HEDGE = {
    "pt": {
        "atenuador": r"\b(acho que|eu acho|talvez|meio que|acredito que|eu diria|possivelmente|se não me engano|de repente)\b",
        "minimizador": r"\b(só um|apenas um|um pouquinho|um pouco|rapidinho|uma coisinha)\b",
        "insegurança": r"\b(não sei se|não tenho certeza|não sei|desculpa|desculpe|perdão|faz sentido\?|espero que|acho que sim)\b",
    },
    "en": {
        "atenuador": r"\b(i think|i guess|i believe|maybe|perhaps|probably|kind of|kinda|sort of|sorta|somewhat|i would say|i'd say)\b",
        "minimizador": r"\b(just|only|a bit|a little bit|a little)\b",
        "insegurança": r"\b(i'?m not sure|not sure|sorry|does that make sense|if that makes sense|hopefully|i don'?t know|i hope)\b",
    },
}

def words_of(sl):
    ws = []
    for s in sl:
        for w in s.get("words", []):
            if "start" in w and "end" in w and w.get("word", "").strip():
                ws.append(w)
    return sorted(ws, key=lambda w: w["start"])

spk_segs = {}
for s in segs:
    spk_segs.setdefault(s.get("speaker", "SEM_LOCUTOR"), []).append(s)

total_fala = sum(s["end"] - s["start"] for s in segs) or 1.0
out = {}
for sp, sl in spk_segs.items():
    ivs = merge([[s["start"], s["end"]] for s in sl])
    fala = sum(b - a for a, b in ivs)
    txt = " ".join(s["text"] for s in sl).lower()
    nw = len(txt.split())

    r = {"tempo_fala_min": round(fala / 60, 1), "pct_reuniao": round(100 * fala / total_fala, 1),
         "palavras": nw, "turnos": len(ivs),
         # mesma regra do make_transcript.py: fatia minuscula = eco/silencio/atribuicao errada
         "provavel_ruido": bool(fala / total_fala < 0.01 and len(sl) <= 5)}

    # --- pitch em semitons re mediana do PROPRIO locutor (isso e o "semitom") ---
    m = inside(ivs, f0_t)
    f0 = f0_v[m]
    if len(f0) >= 200:
        med = float(np.median(f0))
        st = 12 * np.log2(f0 / med)
        st = st[np.abs(st) <= 12]            # descarta erros de oitava do estimador
        r["pitch"] = {
            "mediana_hz": round(med),
            "sd_semitons": round(float(np.std(st)), 2),
            "range_p5_p95_semitons": round(float(np.percentile(st, 95) - np.percentile(st, 5)), 2),
            "frames_vozeados": int(len(f0)),
        }
        r["pitch"]["classificacao"] = ("monótono" if r["pitch"]["sd_semitons"] < 2.0
                                       else "ok" if r["pitch"]["sd_semitons"] <= 2.8 else "expressivo")
    else:
        r["pitch"] = {"erro": "frames vozeados insuficientes"}

    # --- volume: SO dinamica intra-sessao, e so em frames com voz ativa ---
    # sem o corte, o silencio dentro do turno (que cai pra ~0 dB) entra na conta e
    # infla a "dinamica" para 50-60 dB, o que nao diz nada sobre a voz.
    mi = inside(ivs, in_t)
    iv = in_v[mi]
    if len(iv) >= 200:
        iv = iv[iv >= np.percentile(iv, 95) - 25]     # descarta silencio/respiracao
    if len(iv) >= 200:
        r["volume_intra_sessao"] = {"sd_db": round(float(np.std(iv)), 1),
                                    "p10_p90_db": round(float(np.percentile(iv, 90) - np.percentile(iv, 10)), 1),
                                    "nota": "só dinâmica dentro desta reunião; não comparar dB entre reuniões"}

    # --- ritmo e pausas (dos timestamps de palavra) ---
    ws = words_of(sl)
    pausas, longas = [], []
    if len(ws) >= 10:
        gaps = []
        for a, b in zip(ws, ws[1:]):
            g = b["start"] - a["end"]
            if 0 < g < 8:                     # >8s = troca de turno, nao pausa dentro da fala
                gaps.append(g)
                if g >= 0.4:
                    pausas.append(g)
                if g >= 2.0:
                    longas.append({"t": round(a["end"], 1), "hms": hms(a["end"]), "seg": round(g, 1)})
        pausado = sum(pausas)
        artic = max(0.1, fala - pausado)
        r["ritmo"] = {
            "wpm_bruto": round(len(ws) / (fala / 60)),
            "wpm_articulado": round(len(ws) / (artic / 60)),
            "pausas_ge_0_4s": len(pausas),
            "pausa_ratio_pct": round(100 * pausado / fala, 1),
            "pausa_media_s": round(float(np.mean(pausas)), 2) if pausas else 0.0,
            "pausas_longas_ge_2s": len(longas),
        }
        # classifica pelo BRUTO (faixas de conversa valem para ele, nao para o articulado)
        b = r["ritmo"]["wpm_bruto"]
        r["ritmo"]["classificacao"] = "lento" if b < 110 else "ok" if b <= 160 else "acelerado"
        a = r["ritmo"]["wpm_articulado"]
        r["ritmo"]["classificacao_articulado"] = "lento" if a < 150 else "ok" if a <= 210 else "acelerado"

    # --- muletas (PISO: o whisper limpa parte das hesitacoes) ---
    hes = re.findall(HES[L], txt)
    mul = Counter(m_.group(1) for m_ in re.finditer(MUL[L], txt))
    r["muletas"] = {
        "hesitacoes": len(hes),
        "por_100_palavras": round(100 * len(hes) / max(1, nw), 1),
        "candidatas": dict(sorted(mul.items(), key=lambda kv: -kv[1])[:10]),
        "aviso": "PISO, não taxa real — o whisper remove boa parte de 'é/uh/um' na transcrição",
    }

    # --- linguagem de insegurança (hedging) ---
    hg, hg_ex = {}, []
    for cat, rx in HEDGE[L].items():
        n = len(re.findall(rx, txt))
        hg[cat] = {"ocorrencias": n, "por_100_palavras": round(100 * n / max(1, nw), 1)}
    for s in sl:                                    # exemplos com timestamp
        low = s["text"].lower()
        for cat, rx in HEDGE[L].items():
            m_ = re.search(rx, low)
            if m_ and len(hg_ex) < 12:
                hg_ex.append({"cat": cat, "hms": hms(s["start"]), "t": round(s["start"], 1),
                              "trecho": s["text"].strip()[:110]})
                break
    tot_h = sum(v["ocorrencias"] for v in hg.values())
    r["inseguranca"] = {
        "por_categoria": hg,
        "total_por_100_palavras": round(100 * tot_h / max(1, nw), 1),
        "exemplos": hg_ex,
        "leitura": "compare com os OUTROS locutores desta call — taxa isolada não diz nada. "
                   "'just/só' e 'a bit' são legítimos às vezes: confira o trecho antes de acusar.",
    }

    # --- momentos com timestamp, para ouvir no vídeo ---
    mom = []
    for p in sorted(longas, key=lambda x: -x["seg"])[:5]:
        mom.append({"tipo": "pausa longa", "hms": p["hms"], "t": p["t"], "detalhe": f"{p['seg']}s de silêncio no meio da fala"})
    # janelas de 30s monotonas dentro da fala do locutor
    if len(f0) >= 200:
        med = float(np.median(f0)); ft = f0_t[m]
        if len(ft):
            for w0 in np.arange(ft[0], ft[-1], 30.0):
                sel = f0[(ft >= w0) & (ft < w0 + 30)]
                if len(sel) < 600:            # <6s de voz na janela: nao julga
                    continue
                sd = float(np.std(np.clip(12 * np.log2(sel / med), -12, 12)))
                if sd < 1.5:
                    mom.append({"tipo": "trecho monótono", "hms": hms(w0), "t": round(float(w0), 1),
                                "detalhe": f"variação de pitch {sd:.1f} st (bem abaixo do seu normal)"})
    # turnos longos e acelerados
    for s in sl:
        dur = s["end"] - s["start"]
        n = len(s["text"].split())
        if dur >= 15 and n / (dur / 60) > 190:
            mom.append({"tipo": "trecho acelerado", "hms": hms(s["start"]), "t": round(s["start"], 1),
                        "detalhe": f"{round(n/(dur/60))} wpm por {int(dur)}s"})
    mom.sort(key=lambda x: x["t"])
    r["momentos"] = mom[:20]
    out[sp] = r

json.dump({"idioma": LANG, "faixas_referencia": FAIXAS, "locutores": out},
          open(os.path.join(WD, "oratoria.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print(f"[oratoria] idioma={LANG}")
for sp, r in sorted(out.items(), key=lambda kv: -kv[1]["tempo_fala_min"]):
    if r["provavel_ruido"]:
        print(f"  {sp}: {r['tempo_fala_min']:5.1f} min  ⚠️ provável ruído — ignore")
        continue
    p = r.get("pitch", {}); rt = r.get("ritmo", {})
    print(f"  {sp}: {r['tempo_fala_min']:5.1f} min | pitch {p.get('mediana_hz','?')} Hz, "
          f"SD {p.get('sd_semitons','?')} st ({p.get('classificacao','?')}) | "
          f"{rt.get('wpm_bruto','?')} wpm ({rt.get('classificacao','?')}), "
          f"{rt.get('wpm_articulado','?')} artic | "
          f"pausas {rt.get('pausa_ratio_pct','?')}% | muletas {r['muletas']['hesitacoes']} | "
          f"insegurança {r['inseguranca']['total_por_100_palavras']}/100pal")
