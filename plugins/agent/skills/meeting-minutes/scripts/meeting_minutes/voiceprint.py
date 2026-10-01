#!/usr/bin/env python3
"""Impressao digital de voz (speaker embedding) por locutor, para reconhecer QUEM
e cada voz entre todos os videos — em vez de SPEAKER_00/01 anonimos por reuniao.

Usa o MESMO modelo de embedding que a diarizacao do whisperx (wespeaker), entao os
vetores sao consistentes com a diarizacao. Validado em 14/07/2026: a voz do usuario em
reunioes diferentes bate 0.79-0.93; contra outras vozes, ~0.1. Gap vazio entre 0.2 e
0.8, entao o limiar (0.5) nao fica no fio da navalha.

CLI:
  voiceprint.py embed <video> <workdir>         -> escreve <workdir>/voiceprints.npz
  voiceprint.py identify <workdir>              -> quem e cada SPEAKER, vs o catalogo
  voiceprint.py add-ref <nome> <workdir> <SPK>  -> registra a voz de <nome> no catalogo
  voiceprint.py catalog                         -> lista o catalogo

O catalogo fica em ~/.config/meeting-minutes/state/voices/ (mm_config.VOICES).
  <nome>.npy         = centroide (media normalizada das amostras) daquele nome
  <nome>.samples.npz = amostras individuais (reuniao->vetor), para recomputar o centroide
"""
import os, sys, json, glob, warnings, subprocess, tempfile
warnings.filterwarnings("ignore")
import numpy as np

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)
import mm_config
VOICES = mm_config.VOICES
os.makedirs(VOICES, exist_ok=True)
MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"
THRESH = 0.50          # >= isto = mesma pessoa; <= 0.4 outra; meio = incerto
MIN_TURN = 1.2         # ignora turno < isto (curto demais p/ voz limpa)
MAX_S = 45             # concatena ate ~45s dos maiores turnos por locutor

_inf = None
def _model():
    global _inf
    if _inf is None:
        import torch
        from pyannote.audio import Model, Inference
        tok = open(os.path.expanduser(mm_config.load().get("hf_token_path") or "~/.cache/huggingface/token")).read().strip()
        m = Model.from_pretrained(MODEL, use_auth_token=tok)
        # GPU se estiver livre; senao CPU. O caller decide via CUDA_VISIBLE_DEVICES.
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _inf = Inference(m, window="whole"); _inf.to(torch.device(dev))
    return _inf

def speaker_turns(d, sp):
    return sorted([(s["start"], s["end"]) for s in d["segments"]
                   if s.get("speaker") == sp and s["end"] - s["start"] > MIN_TURN],
                  key=lambda t: -(t[1] - t[0]))

def embed_speaker(video, d, sp):
    """Vetor L2-normalizado da voz de <sp>, dos maiores turnos (evita sobreposicao)."""
    turns = speaker_turns(d, sp)
    if not turns:
        return None
    sel, tot = [], 0.0
    for a, b in turns:
        sel.append((a, b)); tot += b - a
        if tot >= MAX_S:
            break
    if tot < 3.0:                     # menos de 3s de voz: nao da p/ impressao confiavel
        return None
    fc = "+".join(f"between(t,{a},{b})" for a, b in sel)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); tmp.close()
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", video,
                        "-map", "0:a:0", "-af", f"aselect='{fc}',asetpts=N/SR/TB",
                        "-ac", "1", "-ar", "16000", tmp.name], check=True)
        e = np.asarray(_model()(tmp.name)).reshape(-1)
        n = np.linalg.norm(e)
        return (e / n) if n > 1e-9 else None
    finally:
        os.unlink(tmp.name)

def embed_folder(video, wd):
    """Calcula e salva o embedding de cada locutor nao-ruido da reuniao."""
    d = json.load(open(os.path.join(wd, "transcript.json"), encoding="utf-8"))
    from collections import Counter
    dur = Counter()
    for s in d["segments"]:
        if s.get("speaker"):
            dur[s["speaker"]] += s["end"] - s["start"]
    out = {}
    for sp, sec in dur.items():
        if sec < 3.0:
            continue
        e = embed_speaker(video, d, sp)
        if e is not None:
            out[sp] = e
    if out:
        np.savez(os.path.join(wd, "voiceprints.npz"), **out)
    return out

def load_folder(wd):
    p = os.path.join(wd, "voiceprints.npz")
    if not os.path.exists(p):
        return {}
    z = np.load(p)
    return {k: z[k] for k in z.files}

# ---- catalogo ----
def load_catalog():
    cat = {}
    for f in glob.glob(os.path.join(VOICES, "*.npy")):
        cat[os.path.splitext(os.path.basename(f))[0]] = np.load(f)
    return cat

def add_ref(name, video, wd, sp):
    d = json.load(open(os.path.join(wd, "transcript.json"), encoding="utf-8"))
    e = embed_speaker(video, d, sp)
    if e is None:
        print(f"❌ {sp} não tem voz suficiente em {wd}"); return
    sp_file = os.path.join(VOICES, f"{name}.samples.npz")
    samples = dict(np.load(sp_file)) if os.path.exists(sp_file) else {}
    samples[os.path.basename(wd)] = e
    np.savez(sp_file, **samples)
    cen = np.mean(list(samples.values()), axis=0); cen /= np.linalg.norm(cen)
    np.save(os.path.join(VOICES, f"{name}.npy"), cen)
    print(f"✅ {name}: +1 amostra ({os.path.basename(wd)}/{sp}) → {len(samples)} no total")

def identify(wd, video=None):
    fp = load_folder(wd)
    if not fp and video:
        fp = embed_folder(video, wd)
    cat = load_catalog()
    d = json.load(open(os.path.join(wd, "transcript.json"), encoding="utf-8"))
    from collections import Counter
    dur = Counter()
    for s in d["segments"]:
        if s.get("speaker"):
            dur[s["speaker"]] += (s["end"] - s["start"]) / 60
    res = {}
    for sp, v in fp.items():
        best, bc = None, -1
        for name, cen in cat.items():
            c = float(v @ cen)
            if c > bc:
                best, bc = name, c
        who = best if bc >= THRESH else ("?incerto:" + best if best and bc >= 0.4 else "desconhecido")
        res[sp] = {"nome": who, "cos": round(bc, 3), "min": round(dur.get(sp, 0), 1)}
    return res

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "embed":
        out = embed_folder(sys.argv[2], sys.argv[3])
        print(f"[voiceprint] {len(out)} locutores → {sys.argv[3]}/voiceprints.npz")
    elif cmd == "add-ref":
        # add-ref <nome> <video> <workdir> <SPK>
        add_ref(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "identify":
        wd = sys.argv[2]; video = sys.argv[3] if len(sys.argv) > 3 else None
        for sp, r in sorted(identify(wd, video).items(), key=lambda kv: -kv[1]["min"]):
            print(f"  {sp} ({r['min']:.1f} min): {r['nome']} (cos {r['cos']})")
    elif cmd == "catalog":
        cat = load_catalog()
        print(f"catálogo: {len(cat)} voz(es) conhecida(s)")
        for name in sorted(cat):
            sp = os.path.join(VOICES, f"{name}.samples.npz")
            n = len(np.load(sp).files) if os.path.exists(sp) else 0
            print(f"  {name}: {n} amostra(s)")
    else:
        print(__doc__)
