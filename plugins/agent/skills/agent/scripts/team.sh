#!/usr/bin/env bash
# team <agent> [new]: opens the session of a team agent in the right folder, RESUMING the previous conversation
# (context, decisions, pending items in memory) when it exists. Lives in the agent plugin since PLN0215 (30/09/2026);
# ~/.local/bin/team is a stub that runs this file from the plugin copy in use, so a `git pull` there updates it.
#
# Agents (PLN0112): the team list is ~/.config/team/agents.json, read by team_agents.py (next to this file). Name,
# aliases, folder ("cwd"), opening skill ("skill") and rotation ("rotate") come from there; what the entry does not say
# comes from the "session" block of the agent-plugin instance config. The same list opens the Team Terminals extension.
#   team --list              the team (name, aliases, folder, skill)
#   team --resolve <agent>   dry run: what `team <agent>` would open (name|folder|skill|rotation|agent plugin)
#
#   - the session id is kept in ~/.config/team/<agent>.session (a UUID);
#   - if ~/.claude/projects/<cwd-slug>/<uuid>.jsonl exists -> `claude --resume <uuid>`;
#   - otherwise -> `claude --session-id <uuid>` (created with that id, so the next opening resumes it);
#   - `team <agent> new` drops the pointer and starts a new conversation.
# One session per agent, by two locks (PLN0132): ~/.cache/team/locks/<agent>.lock survives the boot (systemd-tmpfiles
# wipes /tmp while launchers start) and /tmp/team-<agent>.lock (TEAM_TMP_DIR) sees older launchers still open.
# PLN0215: a shell left behind by the launcher never keeps the locks (fds 8 and 9 are closed before it). When the lock
# is taken, team_lock.py says who holds it: the agent running, or a leftover idle shell of an older launcher, with its
# pid, terminal and how to release it. Typed at a shell prompt (the parent is a shell, as in the Team Terminals
# extension) the launcher just returns to that shell, which stays idle so the extension can revive it; started as the
# terminal's own command, it keeps the terminal open with a plain `bash -i`.
# Per-agent environment (tokens etc.) in ~/.config/team/<agent>.env (chmod 600). Exports TEAM_AGENT=<agent>.
# Overrides for tests: TEAM_TMP_DIR (the /tmp lock), XDG_CACHE_HOME.
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TA="${TEAM_AGENTS_PY:-$here/team_agents.py}"
TL="$here/team_lock.py"
# SEED: the fixed table from before agents.json (agents outside the agent plugin). Only the migration reads it, and only
# without the file.
seed_cwd="${TEAM_SEED_CWD:-$HOME}"
SEED='[
 {"name": "chief-of-staff", "aliases": ["chief"], "cwd": "@CWD@", "skill": "chief-of-staff"},
 {"name": "job-scout", "cwd": "@CWD@", "skill": "job-scout"},
 {"name": "travel-agent", "cwd": "@CWD@", "skill": "travel-agent"}
]'
SEED="${SEED//@CWD@/$seed_cwd}"
if [ ! -f "$TA" ]; then echo "team: não achei $TA (plugin agent)" >&2; exit 1; fi

# Leaves the launcher without holding any lock. At a shell prompt, return to it; otherwise keep the terminal open.
leave() {
  exec 8>&- 9>&-
  case "$(cat "/proc/$PPID/comm" 2>/dev/null)" in
    bash|zsh|sh|dash|fish|ksh) exit "${1:-0}" ;;
  esac
  exec bash -i
}

if [ "$1" = "--list" ]; then
  [ -e "$HOME/.config/team/agents.json" ] || python3 "$TA" migrate --seed - <<< "$SEED" >&2
  exec python3 "$TA" list "${@:2}"
fi
dry=0; if [ "$1" = "--resolve" ]; then dry=1; shift; fi
[ -n "$1" ] || { python3 "$TA" resolve "" --seed - <<< "$SEED"; exit 2; }
info=$(python3 "$TA" resolve "$1" --seed - <<< "$SEED") || exit $?
m=$(sed -n 1p <<< "$info"); dir=$(sed -n 2p <<< "$info"); skill=$(sed -n 3p <<< "$info")
agent_rotate=$(sed -n 4p <<< "$info"); agent_plugin=$(sed -n 5p <<< "$info")
if [ "$dry" = 1 ]; then echo "$m|$dir|$skill|$agent_rotate|$agent_plugin"; exit 0; fi

lockdir="${XDG_CACHE_HOME:-$HOME/.cache}/team/locks"; mkdir -p "$lockdir"
exec 9>"${TEAM_TMP_DIR:-/tmp}/team-$m.lock" 8>"$lockdir/$m.lock"
if ! flock -n 9 || ! flock -n 8; then
  exec 8>&- 9>&-        # one of the two may be ours already: let it go before looking for the holder
  python3 "$TL" explain "$m" 2>/dev/null || echo ">> $m já está rodando em outro terminal. Feche aquele antes de abrir de novo."
  leave 1
fi

[ -f "$HOME/.config/team/$m.env" ] && . "$HOME/.config/team/$m.env"

cd "$dir" || leave 1
export TEAM_AGENT="$m"

# --- rotation (27/09/2026): the cost comes from the conversation size. Agents with rotation get (1) a new session when
# the current one started before today and (2) automatic compaction past COMPACT_PCT % of the window. The old ids go
# to <agent>.history, which team-cost adds up.
ROTATE=" "
[ "${agent_rotate:-0}" = 1 ] && ROTATE="$ROTATE$m "     # "rotate": true in agents.json (or in the instance "session")
COMPACT_PCT=30

# --- one session per agent: resume when it exists, create otherwise ---
mkdir -p "$HOME/.config/team"
ptr="$HOME/.config/team/$m.session"
slug=$(printf '%s' "$dir" | sed 's#[/._]#-#g')     # /a/b.c_d -> -a-b-c-d (what Claude Code does with the project folder)
aposenta() {   # keeps the current id in the history and drops the pointer
  [ -s "$ptr" ] && printf '%s\t%s\t%s\n' "$(date -Iseconds)" "$(tr -d '[:space:]' < "$ptr")" "$1" >> "$HOME/.config/team/$m.history"
  rm -f "$ptr"
}
[ "$2" = "new" ] && aposenta "manual (team $m new)"
if [[ "$ROTATE" == *" $m "* ]]; then
  export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=$COMPACT_PCT
  if [ -s "$ptr" ]; then
    old=$(tr -d '[:space:]' < "$ptr"); f="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$slug/$old.jsonl"
    if [ -f "$f" ]; then
      ts=$(head -c 20000 "$f" | grep -o '"timestamp":"[^"]*"' | head -1 | cut -d'"' -f4)   # UTC
      inicio=$(TZ=America/Sao_Paulo date -d "$ts" +%F 2>/dev/null)
      [ -n "$inicio" ] && [ "$inicio" != "$(TZ=America/Sao_Paulo date +%F)" ] && { echo ">> $m: a sessão atual começou em $inicio; abrindo uma nova (rotação diária)"; aposenta "rotação diária (começou em $inicio)"; }
    fi
  fi
fi
if [ ! -s "$ptr" ]; then
  python3 -c 'import uuid; print(uuid.uuid4())' > "$ptr"
fi
sid=$(tr -d '[:space:]' < "$ptr")
if [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$slug/$sid.jsonl" ]; then
  echo ">> $m: retomando a sessão $sid"
  claude --resume "$sid" --name "$m" --remote-control "$m" --dangerously-skip-permissions "/$skill"
else
  echo ">> $m: sessão nova $sid (a próxima abertura retoma esta)"
  claude --session-id "$sid" --name "$m" --remote-control "$m" --dangerously-skip-permissions "/$skill"
fi
rc=$?
# Planou (28/09/2026): the session closed -> session_closed sign of life for this agent, with the session id. Short
# timeout, silent, without changing the exit code; an agent without the "planou" block (or in test mode) sends nothing.
# Closing the terminal directly (without leaving claude) does not pass here.
WATCH_CORE="$here"   # the agent plugin's own watch_core, for every agent (shared/watch-core left the repository in E6)
[ -d "$WATCH_CORE/watch_core" ] && PYTHONPATH="$WATCH_CORE" timeout 8 python3 -m watch_core.planou --agent "$m" heartbeat session_closed --session-id "$sid" >/dev/null 2>&1
echo; echo ">> claude saiu (código $rc). (team $m new = começar conversa do zero)"
leave "$rc"
