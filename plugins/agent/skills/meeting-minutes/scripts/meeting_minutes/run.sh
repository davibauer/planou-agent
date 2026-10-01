#!/bin/bash
# Transcreve + diariza um video de reuniao e prepara os insumos da ata.
# Uso: run.sh "<video>" ["<output_dir>"]
# Saida em <pasta_do_video>/ata_<nome>/: transcricao.md, transcript.json, speakers.json
set -e
# O cron roda com PATH mínimo (/usr/bin:/bin) e não acha o whisperx de ~/.local/bin.
export PATH="$HOME/.local/bin:$PATH"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$(python3 "$SKILL_DIR/mm_config.py" config)"   # ~/.config/meeting-minutes/config.json (ou o exemplo)
VIDEO="$1"
[ -f "$VIDEO" ] || { echo "❌ Vídeo não encontrado: $VIDEO"; exit 1; }
VDIR="$(cd "$(dirname "$VIDEO")" && pwd)"
BASE="$(basename "$VIDEO")"; STEM="${BASE%.*}"
WD="${2:-$VDIR/ata_${STEM}}"
mkdir -p "$WD"

eval "$(python3 - "$CFG" <<'PY'
import json,sys,os,shlex
c=json.load(open(sys.argv[1])); q=shlex.quote
print("MODEL="+q(c['whisper_model']))
print("ASTREAM="+q(c['audio_stream']))
print("BATCH="+q(str(c['batch_size'])))
print("HFPATH="+q(os.path.expanduser(c['hf_token_path'])))
print("CFGLANG="+q(c.get('language','') or ''))
PY
)"
export HF_TOKEN=$(cat "$HFPATH")
# ATA_LANG força o idioma sem editar o config.json (que vale para todas as gravações).
# Necessário quando o whisper nao so erra o rotulo: ele TRADUZ a reuniao pt-BR para
# ingles. O fixlang.py nao pega esse caso — ele fareja stopwords, e o texto traduzido
# e ingles de verdade. Visto em 03/08/2026 (1h36 de reuniao pt-BR saiu inteira em en).
LANGCODE="${ATA_LANG:-$CFGLANG}"   # nao reusar $LANG: e o locale POSIX, exportado p/ ffmpeg/python
LANGARG=""; [ -n "$LANGCODE" ] && LANGARG="--language $LANGCODE"

# A GPU e compartilhada com o Windows (WSL): Chrome/Edge/VSCode seguram ~5 GB dos 8 GB
# pela aceleracao de hardware, e o large-v2 em float16 (~3,1 GB so de pesos) nao carrega
# — "CUDA failed with error out of memory" ja no load_model. int8 (~1,6 GB) cabe.
# Mantenha o MESMO modelo: cair para 'medium' muda a retencao de muletas ("assim", "ne")
# e o wpm, que sao medidos do texto e comparados entre reunioes. Visto em 01/09/2026.
COMPUTE="${ATA_COMPUTE_TYPE:-float16}"
BATCH="${ATA_BATCH_SIZE:-$BATCH}"

# Trabalha em disco Linux (WSL): o disco do Windows montado é lento para os WAV intermediários.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "[1/4] Extraindo áudio (faixa $ASTREAM)…"
NA=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$VIDEO" | wc -l)
[ "$NA" -gt 1 ] && echo "      ℹ️  $NA faixas de áudio; usando $ASTREAM (normalmente o mix). Ajuste audio_stream em config.json se faltar alguma voz."
ffmpeg -hide_banner -loglevel error -y -i "$VIDEO" -map "$ASTREAM" -ac 1 -ar 16000 "$TMP/audio.wav"

# Serializa o uso da GPU AQUI, e nao em quem chama: qualquer sessao do Claude (ou
# o cron, ou voce na mao) pode invocar este run.sh direto. Com o lock la fora, dois
# whisperx dividiam a mesma GPU de 8 GB — visto em 14/07/2026 com 7917/8192 MiB em uso.
GPU_LOCK="${ATA_GPU_LOCK:-/tmp/ata-reuniao-whisperx.lock}"
exec 8>"$GPU_LOCK"
if ! flock -n 8; then
  echo "[2/4] GPU ocupada por outra transcrição — aguardando na fila…"
  flock 8
fi

echo "[2/4] Transcrevendo + diarizando na GPU (~1 min por 6 min de áudio)…"
whisperx "$TMP/audio.wav" --model "$MODEL" $LANGARG --diarize --hf_token "$HF_TOKEN" \
  --compute_type "$COMPUTE" --batch_size "$BATCH" --output_dir "$TMP/out" --output_format json \
  --print_progress True 2>&1 | grep -viE "^\s*$|tensorflow|warn" || true
[ -f "$TMP/out/audio.json" ] || { echo "❌ whisperx não gerou saída."; exit 1; }
cp "$TMP/out/audio.json" "$WD/transcript.json"
exec 8>&-   # libera a GPU: o resto (prosódia) é CPU

# Corrige o idioma antes de qualquer coisa a jusante ler: o whisperx rotula daily de
# time BR como 'en' com frequencia, e isso envenena prosodia (regex de muletas) e
# pronuncia (que so roda em 'en'). Sniff por texto, barato.
echo "      $(python3 "$SKILL_DIR/fixlang.py" "$WD/transcript.json")"

# Data: o nome do arquivo (padrão OBS "AAAA-MM-DD HH-MM-SS") é mais confiável que o
# mtime, que vira a data da cópia se o vídeo for baixado/movido.
if [[ "$STEM" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2}) ]]; then
  DATA="${BASH_REMATCH[3]}/${BASH_REMATCH[2]}/${BASH_REMATCH[1]}"
else
  DATA=$(date -r "$VIDEO" '+%d/%m/%Y')
  echo "      ℹ️  data vinda do mtime do arquivo ($DATA) — confirme se o vídeo foi copiado/baixado."
fi
SECS=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$VIDEO" | cut -d. -f1)
DUR=$(printf '%dh%02dmin' $((SECS/3600)) $((SECS%3600/60)))
[ $SECS -lt 3600 ] && DUR=$(printf '%dmin%02ds' $((SECS/60)) $((SECS%60)))

echo "[3/4] Gerando transcrição e estatísticas de locutores…"
python3 "$SKILL_DIR/make_transcript.py" "$WD" "$BASE" "$DATA" "$DUR"

echo "[4/5] Medindo prosódia (pitch/semitons, ritmo, pausas, muletas)…"
python3 "$SKILL_DIR/prosody.py" "$WD" "$TMP/audio.wav" || echo "⚠️ prosódia falhou; ata segue sem ela."

echo "[5/5] Impressão de voz por locutor (voiceprint p/ reconhecer quem é quem)…"
# CPU de propósito: a GPU já foi liberada (fd 8 fechado) e outro whisperx pode estar
# usando-a. São poucos segundos de áudio por locutor — barato na CPU.
CUDA_VISIBLE_DEVICES="" python3 "$SKILL_DIR/voiceprint.py" embed "$VIDEO" "$WD" \
  || echo "⚠️ voiceprint falhou (não bloqueia a ata)."

echo ""
echo "✅ Insumos prontos em: $WD"
echo "   • transcricao.md  → transcrição diarizada (leia para escrever a ata)"
echo "   • speakers.json   → estatísticas + pistas de nomes (VERIFIQUE quem é quem)"
echo "   • voiceprints.npz  → impressão de voz por locutor (identifica quem é quem)"
echo "   • oratoria.json   → prosódia por locutor (use a do USUÁRIO para o oratoria.md)"
echo "   • transcript.json → dados brutos"
echo ""
echo "Próximo: identifique quem é quem e, se o idioma for 'en', rode a pronúncia:"
echo "   python3 $SKILL_DIR/pronunciation.py \"$WD\" \"$VIDEO\" <SPEAKER_DO_USUARIO> $CFG"
