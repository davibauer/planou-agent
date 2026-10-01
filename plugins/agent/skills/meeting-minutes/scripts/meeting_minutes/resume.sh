#!/bin/bash
# Retoma o processamento de onde parou. Idempotente: video que ja tem
# transcript.json e pulado, entao nada e refeito.
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$(python3 "$SKILL_DIR/mm_config.py" state)"

rm -f "$STATE/PAUSED" && echo "▶  trava removida — cron e FileSystemWatcher voltam a valer."

FALTAM=$(bash "$SKILL_DIR/watch.sh" --dry-run 2>/dev/null | grep -c PRONTO || true)
echo "   $FALTAM vídeo(s) ainda sem transcrição."

if [ "${1:-}" = "--nofork" ]; then
  bash "$SKILL_DIR/watch.sh" --backfill
else
  setsid nohup bash "$SKILL_DIR/watch.sh" --backfill > "$STATE/backfill.out" 2>&1 < /dev/null &
  disown
  echo "   backfill retomado em segundo plano."
  echo "   acompanhar:  tail -f $STATE/watch.log"
fi
