#!/usr/bin/env bash
# planou-agent <employee>: the employee's Claude Code session in THIS terminal window, opened again when it ends
# (PLN0297). ~/.local/bin/planou-agent (written by install.sh) runs this file from the plugin copy in use; Planou's
# join command ends here. Needs only bash and Claude Code: no VS Code, no tmux.
#
#   planou-agent <employee>    open it here (the same as `team <employee>`, but it stays: when the session ends it opens
#                              again in this same window)
#
# One window per employee: the two locks of the `team` launcher (~/.cache/team/locks/<employee>.lock and
# /tmp/team-<employee>.lock) are taken once and held for as long as this window lives, never dropped between two
# sessions. A second window (or a VS Code terminal) finds them taken, says where the employee is open and leaves (0).
#
# The loop, around `team.sh <employee>` (TEAM_IN_LOOP=1: no locks of its own, returns claude's exit code):
#   - session closed on purpose by the watcher below (~/.config/team/<employee>.restart, first line = the reason):
#     daily rotation (rotate on, the day changed since the session opened) or a new plugin version on disk, both only
#     while the session is idle (no file under ~/.claude/projects/<folder> written for IDLE_MIN minutes) -> opens the
#     next one right away. The runner and team.sh read the marker and send Planou session_restart, not session_closed;
#   - paused on Planou (the provisioner writes the marker with "pausado") -> says how to turn it on and leaves;
#   - /exit (code 0) -> opens again in 10 s; Ctrl+C during the countdown closes the employee;
#   - a crash (any other code) -> opens again after 5, 15, 30, 60, 120, 300 s; the 5th crash in 10 min stops the loop
#     and tells Planou (session_closed);
#   - the window closed (SIGHUP) or Ctrl+C in a countdown -> session_closed to Planou (5 s, silent) and leave.
# The watcher (a background subshell, silent) also runs `provisioner.py once` every 60 s when no provisioner service is
# up on this computer (WSL without systemd, a container): pausing and removing on Planou keep working while the window
# is open.
#
# Python: the portable one of the installer (~/.local/share/planou/python) goes first in PATH when it exists, so
# team.sh, the runner and the provisioner find it as python3.
# Overrides for tests: PLANOU_EMPLOYEE_WATCH_S (30), PLANOU_EMPLOYEE_IDLE_MIN (20), PLANOU_EMPLOYEE_BACKOFF
# ("5 15 30 60 120 300"), PLANOU_EMPLOYEE_EXIT_WAIT (10), PLANOU_EMPLOYEE_ONCE_S (60), PLANOU_ALLOW_ROOT=1,
# PLANOU_EMPLOYEE_PLUGIN_JSON (the plugin.json whose version is watched),
# PLANOU_PYTHON_DIR, TEAM_TMP_DIR, XDG_CACHE_HOME.
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TEAM_SH="$here/team.sh"
PROV="$here/provisioner.py"
PLUGIN_JSON="${PLANOU_EMPLOYEE_PLUGIN_JSON:-$here/../../../.claude-plugin/plugin.json}"

usage() { sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
case "${1:-}" in -h|--help) usage; exit 0;; '') usage >&2; exit 2;; esac
[ $# -eq 1 ] || { usage >&2; exit 2; }

pydir="${PLANOU_PYTHON_DIR:-$HOME/.local/share/planou/python}"
[ -x "$pydir/bin/python3" ] && PATH="$pydir/bin:$PATH"
export PATH

if [ "$(id -u)" = 0 ] && [ "${PLANOU_ALLOW_ROOT:-}" != 1 ]; then
  echo ">> rode como o seu usuario, sem sudo: o Claude Code nao abre o modo sem aprovacao como root" >&2
  exit 1
fi
command -v claude >/dev/null 2>&1 || { echo ">> Claude Code (comando claude) nao encontrado neste terminal" >&2; exit 1; }

info=$(bash "$TEAM_SH" --resolve "$1" 2>&1) || { echo ">> $1 nao esta neste computador: $info" >&2; exit 1; }
IFS='|' read -r m dir _skill rotate _plugin <<< "$info"

# ------------------------------------------------------------------------------------------------ one window
lockdir="${XDG_CACHE_HOME:-$HOME/.cache}/team/locks"; mkdir -p "$lockdir"
exec 9>"${TEAM_TMP_DIR:-/tmp}/team-$m.lock" 8>"$lockdir/$m.lock"
if ! flock -n 9 || ! flock -n 8; then
  exec 8>&- 9>&-
  echo ">> $m ja esta aberto em outra janela deste computador. Use aquela janela."
  who=$(python3 "$here/team_lock.py" explain "$m" 2>/dev/null | sed -n 's/.*outro terminal (\(.*\))\. Feche.*/\1/p')
  [ -n "$who" ] && echo "   onde: $who"
  echo "   Para trocar de janela, feche a outra (Ctrl+C na contagem, ou feche a janela) e rode este comando de novo."
  exit 0
fi

team="$HOME/.config/team"; mkdir -p "$team"
RESTART="$team/$m.restart"
STATE="$team/$m.window"
LOG="$team/$m.window.log"
WATCH_S=${PLANOU_EMPLOYEE_WATCH_S:-30}
IDLE_MIN=${PLANOU_EMPLOYEE_IDLE_MIN:-20}
BACKOFF=(${PLANOU_EMPLOYEE_BACKOFF:-5 15 30 60 120 300})
EXIT_WAIT=${PLANOU_EMPLOYEE_EXIT_WAIT:-10}
ONCE_S=${PLANOU_EMPLOYEE_ONCE_S:-60}
slug=$(printf '%s' "$dir" | sed 's#[/._]#-#g')
pdir="$HOME/.claude/projects/$slug"

today() { TZ=America/Sao_Paulo date +%F; }
version() { sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN_JSON" 2>/dev/null | head -n 1; }
sid() { tr -d '[:space:]' < "$team/$m.session" 2>/dev/null; }
hb() {   # heartbeat <phase>: short, silent, never changes the flow
  local s; s=$(sid)
  PYTHONPATH="$here" timeout 5 python3 -m watch_core.planou --agent "$m" heartbeat "$1" ${s:+--session-id "$s"} >/dev/null 2>&1 || true
}
fresh_reason() {   # the first line of a marker written in the last 2 minutes
  [ -f "$RESTART" ] && [ -n "$(find "$RESTART" -mmin -2 2>/dev/null)" ] && head -n 1 "$RESTART"
}
kids() { cat /proc/"$1"/task/*/children 2>/dev/null; }
service_up() {
  systemctl --user is-active --quiet agent-provisioner.service 2>/dev/null && return 0
  [ "$(uname -s)" = Darwin ] && launchctl print "gui/$(id -u)/app.planou.agent-provisioner" >/dev/null 2>&1 && return 0
  return 1
}

# ------------------------------------------------------------------------------------------------ the watcher
# Silent, in the background for one session: closes claude on purpose (marker first) and plays the provisioner when
# there is no service. $1 = the pid of this loop (team.sh is its child, claude is team.sh's).
watch_session() {
  local main=$1 me=$BASHPID since=$SECONDS once_at=-100000 day ver
  read -r day ver < "$STATE"
  while sleep "$WATCH_S"; do
    # a marker of the previous session (read by its runner by now) goes away; one written for this session stays
    if [ -f "$RESTART" ] && [ "$RESTART" -ot "$STATE" ] && [ $((SECONDS - since)) -ge 20 ]; then rm -f "$RESTART"; fi
    if [ $((SECONDS - once_at)) -ge "$ONCE_S" ] && [ -f "$HOME/.config/agent-provisioner/secrets/planou.env" ] && ! service_up; then
      once_at=$SECONDS
      timeout 170 python3 "$PROV" once >> "$LOG" 2>&1 </dev/null
    fi
    local why=
    if [ -f "$RESTART" ] && ! [ "$RESTART" -ot "$STATE" ]; then
      why=$(head -n 1 "$RESTART")                      # the provisioner paused it, or another reason already written
    elif [ -z "$(find "$pdir" -type f -mmin -"$IDLE_MIN" -print -quit 2>/dev/null)" ]; then
      local now_ver; now_ver=$(version)
      if [ "$rotate" = 1 ] && [ "$(today)" != "$day" ]; then why="rotacao diaria: a sessao comecou em $day"
      elif [ -n "$now_ver" ] && [ -n "$ver" ] && [ "$now_ver" != "$ver" ]; then why="versao nova $now_ver do plugin agent"
      fi
      [ -z "$why" ] || printf '%s\n' "$why" > "$RESTART"
    fi
    [ -n "$why" ] || continue
    local t c
    for t in $(kids "$main"); do
      [ "$t" = "$me" ] && continue
      for c in $(kids "$t"); do kill -TERM "$c" 2>/dev/null; done
    done
    sleep 10
    for t in $(kids "$main"); do
      [ "$t" = "$me" ] && continue
      for c in $(kids "$t"); do kill -KILL "$c" 2>/dev/null; done
    done
    return 0
  done
}

WATCHER=
stop_watcher() { [ -n "$WATCHER" ] && { kill "$WATCHER" 2>/dev/null; wait "$WATCHER" 2>/dev/null; }; WATCHER=; }

closing() {   # $1 = what to say first
  trap '' HUP INT TERM
  stop_watcher
  hb session_closed
  echo
  [ -n "${1:-}" ] && echo "$1"
  echo "== $m saiu do ar. O Planou mostra Fora do ar e avisa se houver tarefa na fila."
  echo "   Para abrir de novo: rode o mesmo comando, ou planou-agent $m"
  exit "${2:-0}"
}

countdown() {   # countdown <seconds> <line>: Ctrl+C or the window closing ends the employee
  echo "$2"
  trap 'closing "^C"' INT
  trap 'closing' HUP TERM
  sleep "$1" & wait $! 2>/dev/null
  trap - INT HUP TERM
}

crashes=()
streak=0
echo ">> $m nesta janela: deixe aberta (fechar a janela tira $m do ar)."
while :; do
  printf '%s %s\n' "$(today)" "$(version)" > "$STATE"
  started=$SECONDS
  watch_session $$ </dev/null >/dev/null 2>&1 8>&- 9>&- &
  WATCHER=$!
  GONE=
  trap ':' INT                # claude reads Ctrl+C itself; the handler (not an ignore) is reset in the children
  trap 'GONE=1' HUP TERM
  TEAM_IN_LOOP=1 bash "$TEAM_SH" "$m" 8>&- 9>&-
  rc=$?
  stop_watcher
  trap - INT HUP TERM
  [ -n "$GONE" ] && closing
  why=$(fresh_reason)
  [ $((SECONDS - started)) -ge 600 ] && streak=0
  case "$why" in
    pausado*|removido*|saiu*)
      rm -f "$RESTART"
      echo; echo ">> claude saiu ($why)"
      case "$why" in
        pausado*) echo "== $m foi pausado pelo Planou. Para religar: Ligar na pagina do funcionario, ou rode de novo este comando.";;
        *) echo "== $m nao roda mais neste computador ($why). Esta janela pode ser fechada.";;
      esac
      exit 0;;
    ?*)
      echo; echo ">> claude saiu ($why)"
      echo "== religando $m nesta janela"
      continue;;
  esac
  echo
  if [ "$rc" = 0 ]; then
    echo ">> claude saiu (codigo 0)"
    countdown "$EXIT_WAIT" "== Sessao encerrada. Religando em $EXIT_WAIT s; Ctrl+C fecha o funcionario."
    continue
  fi
  now=$SECONDS
  recent=()
  for t in "${crashes[@]}"; do [ $((now - t)) -lt 600 ] && recent+=("$t"); done
  crashes=("${recent[@]}" "$now")
  if [ "${#crashes[@]}" -ge 5 ]; then
    closing ">> claude saiu com erro (codigo $rc): 5 quedas em 10 min, parei de religar. Veja a mensagem acima." 1
  fi
  delay=${BACKOFF[$streak]:-${BACKOFF[-1]}}
  streak=$((streak + 1))
  echo ">> claude saiu com erro (codigo $rc)"
  countdown "$delay" "== religando em $delay s (${#crashes[@]}a queda em 10 min). Ctrl+C fecha o funcionario."
done
