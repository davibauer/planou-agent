#!/bin/bash
# Monitora a pasta de gravacoes e roda o pipeline (transcricao + diarizacao + prosodia)
# em todo video FINALIZADO que ainda nao tem ata. Chamado pelo cron a cada 5 min.
#
#   watch.sh              -> modo cron: so processa video finalizado e estavel
#   watch.sh --backfill   -> processa tudo que falta agora (nao espera estabilidade)
#   watch.sh --dry-run    -> so mostra o que faria
#
# "Video finalizado" = tem moov atom (o OBS so escreve o indice do MP4 quando voce
# PARA a gravacao) E o tamanho parou de crescer. Os dois juntos, porque a pasta e
# sincronizada pelo Google Drive: um arquivo pode chegar pela metade ja com moov.
#
# NAO escreve a ata: isso exige saber quem e quem, que e julgamento do Claude na
# sessao. Aqui roda so o que e deterministico (a parte lenta de GPU).
set -u
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VDIR="$(python3 "$SKILL_DIR/mm_config.py" videos_dir)"   # env ATA_VIDEOS_DIR > config videos_dir > ~/Videos
STATE="$(python3 "$SKILL_DIR/mm_config.py" state)"
SKIP="$STATE/skip.tsv"          # <stem> \t <motivo>  — para nao re-testar lixo todo tick
SIZES="$STATE/sizes"            # tamanho visto no tick anterior
LOG="$STATE/watch.log"
LOCK="${ATA_LOCK:-/tmp/ata-reuniao-gpu.lock}"   # override só para teste
MIN_DUR=60                      # < 1 min nao e reuniao
STABLE_SECS=120                 # tamanho tem de estar parado ha 2 min
CORRUPT_AFTER_H=6               # sem moov e mais velho que isso = OBS morreu, nao vai finalizar

mkdir -p "$STATE" "$SIZES"; touch "$SKIP"
MODE="${1:-}"

# Pausa: enquanto state/PAUSED existir, NINGUEM processa — nem o cron, nem o
# FileSystemWatcher, nem o backfill. Retomar: rm state/PAUSED (ou resume.sh).
# Sem isso, matar o backfill nao adianta: o cron reinicia tudo no proximo tick.
if [ -f "$STATE/PAUSED" ] && [ "${1:-}" != "--dry-run" ]; then
  echo "⏸  pausado ($STATE/PAUSED existe) — nada será processado. Retomar: bash $SKILL_DIR/resume.sh"
  exit 0
fi
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; echo "$*"; }   # cron redireciona p/ /dev/null

# Lock GLOBAL, nao-bloqueante: so um watch.sh por vez (uma GPU so). Se o backfill
# esta rodando, o tick do cron sai na hora em vez de empilhar. Sem isso, cada tick
# ficava preso no lock, e ao ser liberado reprocessava video que o backfill ja tinha
# feito — decisao tomada ANTES de pegar o lock.
exec 9>"$LOCK"
if ! flock -n 9; then
  [ "$MODE" = "--dry-run" ] || { log "já há um watch.sh rodando (GPU ocupada) — saio"; exit 0; }
fi

skipped()  { cut -f1 "$SKIP" | grep -qxF "$1"; }
skip_add() { printf '%s\t%s\n' "$1" "$2" >> "$SKIP"; log "SKIP  $1 — $2"; }

shopt -s nullglob
for V in "$VDIR"/*.mp4 "$VDIR"/*.mkv; do
  BASE="$(basename "$V")"; STEM="${BASE%.*}"
  WD="$VDIR/ata_$STEM"

  [ -f "$WD/transcript.json" ] && continue          # ja processado
  skipped "$STEM" && continue

  # --- finalizado? (moov atom) ---
  DUR=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 "$V" 2>/dev/null | cut -d. -f1)
  if [ -z "$DUR" ] || [ "$DUR" = "N/A" ]; then
    AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$V") ) / 3600 ))
    if [ "$AGE_H" -ge "$CORRUPT_AFTER_H" ]; then
      skip_add "$STEM" "sem moov atom há ${AGE_H}h — gravação do OBS não finalizada (arquivo corrompido)"
    else
      log "WAIT  $STEM — sem moov ainda (OBS gravando?)"
    fi
    continue
  fi

  # --- lixo: curto demais ou sem audio ---
  if [ "$DUR" -lt "$MIN_DUR" ]; then
    skip_add "$STEM" "só ${DUR}s de duração — não é reunião"; continue
  fi
  NA=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$V" 2>/dev/null | wc -l)
  if [ "$NA" -eq 0 ]; then
    skip_add "$STEM" "nenhuma faixa de áudio"; continue
  fi

  # --- tamanho estavel? (o Drive pode estar baixando) ---
  # --dry-run passa por AQUI de propósito: ele tem de mostrar o que o cron faria de
  # verdade (inclusive segurar o arquivo que ainda não estabilizou). Só não roda a GPU.
  SZ=$(stat -c %s "$V"); NOW=$(date +%s); F="$SIZES/$STEM"
  if [ "$MODE" = "--now" ]; then
    # Gatilho do FileSystemWatcher: ele so chama depois do arquivo ficar quieto,
    # entao nao preciso dos dois ticks — confirmo a estabilidade aqui mesmo.
    sleep 15
    if [ "$(stat -c %s "$V")" != "$SZ" ]; then
      log "WAIT  $STEM — ainda crescendo; deixo para o próximo gatilho"; continue
    fi
  elif [ "$MODE" != "--backfill" ]; then
    if [ -f "$F" ]; then
      read -r OLD_SZ OLD_T < "$F"
      if [ "$SZ" != "$OLD_SZ" ]; then
        echo "$SZ $NOW" > "$F"; log "WAIT  $STEM — ainda crescendo ($OLD_SZ → $SZ)"; continue
      fi
      if [ $(( NOW - OLD_T )) -lt "$STABLE_SECS" ]; then
        log "WAIT  $STEM — estável há $(( NOW - OLD_T ))s (preciso de ${STABLE_SECS}s)"; continue
      fi
    else
      echo "$SZ $NOW" > "$F"; log "WAIT  $STEM — primeira vez que vejo; confirmo no próximo tick"; continue
    fi
  fi

  if [ "$MODE" = "--dry-run" ]; then log "PRONTO (dry-run) $STEM — ${DUR}s"; continue; fi

  log "RODA  $STEM — $((DUR/60)) min de vídeo"
  if bash "$SKILL_DIR/run.sh" "$V" >> "$LOG" 2>&1; then
    log "OK    $STEM → $WD"
  else
    log "ERRO  $STEM — run.sh falhou (veja o log acima)"
    rm -rf "$WD"                                    # nao deixa pasta pela metade
  fi
done

# Lembrete do que ficou esperando a ata (o Claude escreve na sessao seguinte)
PEND=()
for W in "$VDIR"/ata_*/; do
  [ -f "$W/transcript.json" ] && [ ! -f "$W/ata.md" ] && PEND+=("$(basename "$W")")
done
if [ ${#PEND[@]} -gt 0 ]; then
  printf '%s\n' "${PEND[@]}" > "$STATE/pendentes.txt"
  log "→ ${#PEND[@]} transcrição(ões) esperando a ata: ${PEND[*]}"
else
  : > "$STATE/pendentes.txt"
fi
