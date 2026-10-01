#!/bin/bash
# Pausa TODO o processamento de gravacoes: backfill, cron e FileSystemWatcher.
# Matar o backfill sozinho nao adianta — o cron o reergue no proximo tick (5 min).
# Retomar de onde parou: resume.sh
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VDIR="$(python3 "$SKILL_DIR/mm_config.py" videos_dir)"   # env ATA_VIDEOS_DIR > config videos_dir > ~/Videos
STATE="$(python3 "$SKILL_DIR/mm_config.py" state)"; mkdir -p "$STATE"

date '+%F %T' > "$STATE/PAUSED"
echo "⏸  trava criada — cron e FileSystemWatcher não vão processar nada."

# Derruba o que esta rodando AGORA (so o desta pasta; nao mexe em outras sessoes).
# ORDEM IMPORTA: mata o whisperx FILHO primeiro. Se matar o run.sh pai antes, o
# whisperx vira orfao e nao da mais para saber de que pasta ele era — sobrevive
# segurando a GPU (aconteceu em 14/07/2026).
ME=$$
for RP in $(pgrep -f "run.sh $VDIR" 2>/dev/null); do
  [ "$RP" = "$ME" ] && continue
  for CH in $(pgrep -P "$RP" 2>/dev/null) ; do
    for GC in $CH $(pgrep -P "$CH" 2>/dev/null); do
      ps -o args= -p "$GC" 2>/dev/null | grep -q whisperx && { kill -9 "$GC" 2>/dev/null; echo "   whisperx $GC interrompido"; }
    done
  done
  kill "$RP" 2>/dev/null && echo "   run.sh $RP interrompido"
done
for BP in $(pgrep -f 'watch.sh --backfill' 2>/dev/null); do
  [ "$BP" = "$ME" ] && continue
  kill "$BP" 2>/dev/null && echo "   backfill $BP interrompido"
done

# A pasta do video que estava no meio fica incompleta: remove, senao ele e dado
# como pronto e nunca mais e transcrito. Sem transcript.json = sera refeito no resume.
sleep 1
for d in "$VDIR"/ata_*/; do
  [ -d "$d" ] || continue
  [ -f "$d/transcript.json" ] || { rm -rf "$d"; echo "   descartada pasta incompleta: $(basename "$d")"; }
done

echo
echo "✅ pausado. Nada se perde: o que já tem transcript.json fica."
echo "   Retomar:  bash $SKILL_DIR/resume.sh"
