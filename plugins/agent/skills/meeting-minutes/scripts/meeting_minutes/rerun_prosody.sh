#!/bin/bash
# Recalcula oratoria.json em TODAS as pastas ata_* (schema uniforme).
# Uso quando o prosody.py muda: as pastas antigas ficam com o schema velho e a
# comparacao entre reunioes passa a comparar coisas diferentes.
# Nao usa GPU (parselmouth e CPU) — pode rodar junto com o backfill.
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VDIR="$(python3 "$SKILL_DIR/mm_config.py" videos_dir)"   # env ATA_VIDEOS_DIR > config videos_dir > ~/Videos
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

shopt -s nullglob
for WD in "$VDIR"/ata_*/; do
  [ -f "$WD/transcript.json" ] || continue
  STEM="$(basename "$WD")"; STEM="${STEM#ata_}"
  V=""
  for ext in mp4 mkv; do [ -f "$VDIR/$STEM.$ext" ] && V="$VDIR/$STEM.$ext"; done
  [ -n "$V" ] || { echo "⏭  $STEM — vídeo original sumiu, pulo"; continue; }

  printf '%-42s ' "$STEM"
  ffmpeg -hide_banner -loglevel error -y -i "$V" -map 0:a:0 -ac 1 -ar 16000 "$TMP/a.wav" 2>/dev/null || {
    echo "❌ falha ao extrair áudio"; continue; }
  if python3 "$SKILL_DIR/prosody.py" "$WD" "$TMP/a.wav" > "$TMP/out" 2>&1; then
    echo "✅ $(grep -c 'min |' "$TMP/out" || true) locutor(es)"
  else
    echo "❌ prosody.py falhou:"; tail -3 "$TMP/out" | sed 's/^/     /'
  fi
  rm -f "$TMP/a.wav"
done
