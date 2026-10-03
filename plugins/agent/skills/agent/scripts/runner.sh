#!/usr/bin/env bash
# runner.sh <instancia> [interval_s] | runner.sh <instancia> stop
# The one runner of the agent plugin, born from work-watch's runner after the stop fix (work-watch 0.30.0). It runs
# `agent.py <instancia>` (the heavy tick) every INTERVAL WITHOUT the model and ends only when there is something for the
# session: Claude Code wakes the session when the process ends (Bash run_in_background). An empty tick costs no token.
#
# Folder: the resolver (watch_core.config.agent_root): ~/.config/agent/<instancia>/ when it exists, else the legacy
# folder. Output of the tick that woke: <root>/cache/runner/tick.out (+ EXIT=n HH:MM); metrics in metricas.tsv.
# Interval: argument, $INTERVAL, "interval_s" of the config (default 300, at least 60). Relaunch with FIRST_NOW=0 after
# reading a tick (the first heavy tick was done already; PRIMEIRO_IMEDIATO=0 is the old spelling and still works).
# Refuses an instance in test mode ("live" not true) and an invalid config (agent.py <instancia> --validate).
# The runner lives INSIDE the session on purpose: closing the session turns the agent off.
#
# Light loop (Planou): between heavy ticks, a long poll (`python3 -m watch_core.planou poll --wait 50`, PLANOU_WAIT_S,
# at most 55) runs in a continuous loop; a Planou without long polling answers at once and the runner sleeps
# PLANOU_POLL_S (default 90); Planou down backs off from 5 s up to PLANOU_POLL_S and never wakes the session. What the
# person does there (an answer, a message from the Conversa tab, a task handed to the agent's queue) becomes the block
# `== PLANOU (n)` in tick.out and wakes the session; messages and queue tasks are acknowledged (`ack-delivered`) only
# after tick.out was printed. The next heavy tick keeps its time (next_heavy): a light wake never pushes it.
# Conversation: every 5 s a `stat` of the session transcript; only when it changed, `watch_core.transcript pump` sends
# the new messages, the cost of each closed turn and the Remote Control link. Never wakes the session.
# Session: on HUP/TERM/INT, or when the 5 s check finds the session process gone, heartbeat session_closed and leave.
# Stop: `runner.sh <instancia> stop` stops every runner of the instance (each one's process group and tree: the long
# poll, the sleep, the heavy tick) and waits for them to go; relaunch = stop and up again.
# One runner per instance (PLN0082): runner.lock beside runner.pid, held while the runner lives (see `trava`).
# Without the "planou" block (or without the key) the light loop is off and this is a plain sleep.
# Cycle (PLN0144): Claude Code stops a background task when its Bash `timeout` runs out (30 min by default, 2 h at most),
# with "stopped after reaching its background time limit", and a runner that only ends when there is content died
# unseen in any quiet half hour. The runner now leaves on its own after RUNNER_MAX_S seconds (default 1500, 25 min) of
# life, counted on the monotonic clock from the start of this process (execs keep it): next_heavy saved, metrics row
# with exit "ciclo", tick.out and stdout just `== RUNNER CICLO (relancar sem tratar)`, exit 0. The session only
# relaunches it (FIRST_NOW=0). A long poll in flight is not cut at RUNNER_MAX_S (the next check comes when it ends), nor
# a heavy tick; a due heavy tick that could not end in time waits for the new cycle (the hard line below).
# Hard line (PLN0235, PLN0237): a runner killed at 30 min without the cycle line once more. Every way out now comes
# before HARD = RUNNER_MAX_S + RUNNER_MARGIN_S (default 240 s: 29 min), with RUNNER_RESERVE_S (default 120 s) kept for
# what follows a heavy tick (the grouped blocks, the rollout, the wake and its Planou calls). A due heavy tick opens only
# when its ceiling (TEMPO + the 20 s of `timeout -k`) still ends before HARD - RESERVE; TEMPO itself is capped so that a
# runner that just started always fits it. So a heavy tick alive at HARD - RESERVE is stuck: left behind (PLN0241).
# After the tick, each step gets only the time left before HARD (the rollout is skipped when short: it runs on the next
# tick), and so do the Planou and transcript calls (fg_wait). A long poll still alive at HARD - RESERVE (normal ones end
# in 75 s at most) is stopped. A self relaunch (PLN0155) happens only before RUNNER_MAX_S; later the session relaunches.
# Lines that ask nothing (PLN0155, see acorda): `PLANOU: <PID> alterada ...`, `-- ALERTA VISTO` and `== DEPLOY JA AVISADO`
# never wake alone; they wait in cache/runner/adiado and go under `== SEM ACAO` at the end of the next tick.out.
# Grouped blocks (PLN0247, scripts/wake.py): an instance whose behaviors declare wake.json (or with "wake" in its config)
# has the blocks it lists (job-scout: the jobs that passed the filter, the nags about them, the Gmail reminder) moved out of
# each heavy tick into <root>/data/retido.json (data/: the only copy of jobs already marked as seen), so they never wake
# the session alone. Any wake (anything else in a tick, a Planou event, a stuck tick) prints the buffer at the top of tick.out, as content to handle (never under `== SEM ACAO`),
# and only then deletes it. The buffer wakes on its own on an urgent line, when its oldest block passes hold_max_min, when
# it passes hold_max_kb, and on the first heavy tick of a session (FIRST_NOW=1). A tick that only fed the buffer writes its
# metrics row as an empty one (exit, 0 bytes). "broken_repeat_min" there sets how often a FONTE QUEBRADA of a source
# already broken wakes again (default 60 min; 0 = only when a new source breaks; a source that breaks again after coming
# back is new).
# Stuck tick (PLN0241): a Windows drive (/mnt/<letter>, 9P) that dies mid-tick leaves the tick in state D for good: not
# even SIGKILL takes it out, and `timeout` (and a `wait` on it) waits forever, so the runner hung and the background
# limit killed it unseen. No blocking `wait` on a child any more: the heavy tick and every call are polled (`kill -0` and
# /proc). After RUNNER_TICK_STUCK_S (default TEMPO + 30 s; never past the cycle, so the runner still leaves before the
# background limit) the runner leaves the tick behind: SIGKILL to its tree (it lands when the drive answers), its output
# moved to tick.preso.out, cache/runner/tick.preso ("pid start since state wchan", the thread in D and its wchan read
# from /proc/<pid>/task/*/stat and wchan), a medium Planou alert "tick de <inst> preso (<wchan>)" (one per pid: the ref
# is the pid) and one wake with `== TICK PRESO (pid N, desde HH:MM, <wchan>)`. While that pid lives (the same pid and
# start time) no heavy tick opens, in this runner or the next ones: the check comes back every minute, never wakes, and
# when it is gone the line `tick preso ... terminou` waits for the next wake (`== SEM ACAO`). A Planou or transcript call
# stuck past FG_MAX_S (default 60 s) is left behind the same way, with a line in avisos.log.
INST=${1:?uso: runner.sh <instancia> [interval_s] | runner.sh <instancia> stop}
RUNNER_ARGS=("$@")
S=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=; LIVE=; INTERVAL_CFG=; ERR=; WAKE=; BROKEN_S=
ENVL=$(python3 "$S/agent.py" "$INST" --runner-env 2>&1) || { echo "runner: $ENVL"; exit 1; }
eval "$(printf '%s\n' "$ENVL" | sed -n 's/^ROOT=/ROOT=/p; s/^LIVE=/LIVE=/p; s/^INTERVAL=/INTERVAL_CFG=/p; s/^ERR=/ERR=/p; s/^WAKE=\([01]\)$/WAKE=\1/p; s/^BROKEN_S=\([0-9]*\)$/BROKEN_S=\1/p')"
[ -n "$ROOT" ] || { echo "runner: $INST sem pasta ($ENVL)"; exit 1; }
D=$ROOT/cache/runner
# Signals ignored when the runner starts cannot be trapped by bash: nohup ignores HUP, and a shell that is PID 1 of a pid
# namespace (`unshare -Urpf --mount-proc`, as the local CI runs) hands TERM and INT down ignored. The runner then outlived
# a TERM, a stop waited its whole 30 s and a closing terminal went unnoticed (PLN0067). Run again, same pid, with HUP,
# INT and TERM back to their defaults, so the traps below hold.
sig_ign=$(sed -n 's/^SigIgn:[[:space:]]*//p' /proc/$$/status 2>/dev/null)
if [ "$RUNNER_SIGRESET" != "$$" ] && (( 0x${sig_ign:-0} & 0x4003 )) && env --default-signal=HUP true 2>/dev/null; then
  RUNNER_SIGRESET=$$ exec env --default-signal=HUP,INT,TERM bash "$0" "$@"
fi
unset sig_ign
pstat() { local x; x=$(cat "/proc/$1/stat" 2>/dev/null) || return 1; echo "${x##*) }"; }   # fields from 3 on (state ppid ...)
eh_runner() {   # $1 = pid: alive, not a zombie, and running this runner.sh (agent.py beside it) for this instance
  roda "$1" agent.py "$INST"
}
roda() {   # $1 = pid, $2 = script beside runner.sh, $3 = instance argument ('*' = any, or none)
  local p=$1 st a i d; [ -n "$p" ] && kill -0 "$p" 2>/dev/null || return 1
  st=$(pstat "$p") && [ "${st%% *}" != Z ] || return 1
  mapfile -d '' a < "/proc/$p/cmdline" 2>/dev/null || return 1
  for i in "${!a[@]}"; do
    case "${a[$i]}" in */runner.sh) d=${a[$i]%/*};; runner.sh) d=.;; *) continue;; esac
    case "$d" in /*) ;; *) d=/proc/$p/cwd/$d;; esac
    [ -f "$d/$2" ] && { [ "$3" = '*' ] || [ "${a[$((i + 1))]}" = "$3" ]; } && return 0
  done
  return 1
}
# Work-watch, job-scout or travel-agent instance moved to this plugin (agent.py --migrate): the old runner of the same instance may still
# run from the session that was open, through the link left at the old folder, and shares cache/runner with this one.
# Never two runners, and never this one's stop removing the other's pid: stop the old one first.
case "$INST" in work-watch-?*)
  p=$(cat "$D/runner.pid" 2>/dev/null)
  if roda "$p" watch.py "${INST#work-watch-}"; then
    echo "runner: o runner antigo do work-watch ainda roda ${INST#work-watch-} (pid $p); pare antes: bash <work-watch>/scripts/runner.sh ${INST#work-watch-} stop"
    exit 1
  fi;;
job-scout)   # the same for job-scout (E4): its old runner.sh runs scout.py beside it and takes no instance argument
  p=$(cat "$D/runner.pid" 2>/dev/null)
  if roda "$p" scout.py '*'; then
    echo "runner: o runner antigo do job-scout ainda roda (pid $p); pare antes: bash <job-scout>/scripts/runner.sh stop"
    exit 1
  fi;;
travel-agent)   # the same for travel-agent (E5): its old runner.sh has gflights.py beside it and takes no instance argument
  p=$(cat "$D/runner.pid" 2>/dev/null)
  if roda "$p" gflights.py '*'; then
    echo "runner: o runner antigo do travel-agent ainda roda (pid $p); pare antes: bash <travel-agent>/scripts/runner.sh --stop"
    exit 1
  fi;;
esac
grupo_vivo() {   # $1 = runner pid: it or anything left in its process group (zombies do not count, nor, with the runner
  # gone, a process in D: a tick stuck on a dead drive takes no signal, PLN0241)
  local q st; eh_runner "$1" && return 0
  for q in $(pgrep -g "$1" 2>/dev/null); do st=$(pstat "$q") && case "${st%% *}" in Z|D) ;; *) return 0;; esac; done
  return 1
}
mata_arvore() {   # $1 = pid, $2 = signal: every descendant, deepest first (`timeout` leaves the group: a group kill misses it)
  local c; for c in $(pgrep -P "$1" 2>/dev/null); do mata_arvore "$c" "$2"; kill "-$2" "$c" 2>/dev/null; done
}
same_home() { grep -qxzF "HOME=$HOME" "/proc/$1/environ" 2>/dev/null; }   # same HOME = same ~/.config: a stop in a
# test's temporary HOME never reaches a real runner (PLN0082: a test stop once took the real one down)
runners() {   # every runner of this instance alive now: group leaders running this runner.sh for it (never this very
  # process, a stop or a --status, nor the session's `bash -c` wrapper, which does not lead a group)
  local q st g a x
  for q in $(pgrep -x bash 2>/dev/null); do
    [ "$q" != $$ ] && eh_runner "$q" || continue
    same_home "$q" || continue
    st=$(pstat "$q") || continue; read -r _ _ g _ <<< "$st"; [ "$g" = "$q" ] || continue
    mapfile -d '' a < "/proc/$q/cmdline" 2>/dev/null || continue
    for x in "${a[@]}"; do case "$x" in stop|--stop|--parar|--status) continue 2;; esac; done
    echo "$q"
  done
}
# PLN0082: two launches in a row once left two runners of one instance, and a stop took down only the one in runner.pid.
# The stop takes down every runner of the instance (the one in runner.pid and any other found in /proc).
if [ "$2" = stop ]; then
  p=$(cat "$D/runner.pid" 2>/dev/null)
  alvos=$( { eh_runner "$p" && same_home "$p" && echo "$p"; runners; } | sort -un | tr '\n' ' ')
  if [ -z "${alvos// }" ]; then rm -f "$D/runner.pid"; echo "runner $INST ja estava parado"; exit 0; fi
  printf '%s\n' $alvos > "$D/runner.stop"   # each runner sees it: a stop, not the terminal closing (no wait for the session)
  algum_vivo() { local q; for q in $alvos; do grupo_vivo "$q" && return 0; done; return 1; }
  for q in $alvos; do kill -TERM -- "-$q" 2>/dev/null; kill -TERM "$q" 2>/dev/null; done
  for _ in $(seq 1 60); do algum_vivo || break; sleep 0.5; done
  if algum_vivo; then for q in $alvos; do mata_arvore "$q" KILL; kill -KILL -- "-$q" 2>/dev/null; kill -KILL "$q" 2>/dev/null; done; sleep 0.5; fi
  rm -f "$D/runner.stop"; case " $alvos" in *" $(cat "$D/runner.pid" 2>/dev/null) "*) rm -f "$D/runner.pid";; esac
  alvos=${alvos% }
  if algum_vivo; then echo "runner $INST (pid ${alvos// /, }) nao saiu"; exit 1; fi
  echo "runner $INST parado (pid ${alvos// /, })"; exit 0
fi
# lead a process group of its own (same pid, same session: the session check still walks up from here)
if [ "$(ps -o pgid= -p $$ | tr -d ' ')" != "$$" ] && [ -z "$RUNNER_PGRP" ]; then
  RUNNER_PGRP=1 exec python3 -c 'import os, sys; os.setpgid(0, 0); os.execvp(sys.argv[1], sys.argv[1:])' bash "${BASH_SOURCE[0]}" "$@"
fi
unset RUNNER_PGRP
V=$S/agent.py; ARGS=("$INST")
[ -z "$ERR" ] || { echo "runner: $INST: $ERR"; exit 1; }
[ "$LIVE" = 1 ] || { echo "runner: $INST esta em modo teste (live != true); nao armo o runner de uma instancia em teste"; exit 1; }
INTERVALO=${2:-${INTERVAL:-${INTERVALO:-$INTERVAL_CFG}}}
case "$INTERVALO" in ''|*[!0-9]*) INTERVALO=300;; esac; [ "$INTERVALO" -lt 60 ] && INTERVALO=60
TEMPO=$(( INTERVALO > 600 ? 600 : INTERVALO - 20 ))      # teto de um tick (os de 15 min olham clusters e demoram)
mkdir -p "$D"
# STEP: the check step (session, conversation) and the floor of every interval: 5 s. RUNNER_TEST_STEP_S (1 to 5) is
# only for the tests, so they never wait 5 s in real time.
STEP=5; case "${RUNNER_TEST_STEP_S:-}" in [1-5]) STEP=$RUNNER_TEST_STEP_S;; esac
POLL=${PLANOU_POLL_S:-90}; [ "$POLL" -lt "$STEP" ] 2>/dev/null && POLL=$STEP; [ "$POLL" -gt 600 ] && POLL=600
WAIT=${PLANOU_WAIT_S:-50}; [ "$WAIT" -lt "$STEP" ] 2>/dev/null && WAIT=$STEP; [ "$WAIT" -gt 55 ] && WAIT=55   # long poll (<= 55: Cloudflare)
# every command that may take long runs in the background with a wait: bash defers a trapped signal until a foreground
# command ends, and a `wait` is interrupted at once (a stop never waits for the tick, a Planou call or the transcript pump)
# PLN0241: never a blocking `wait` on a child that may be in D (a dead Windows drive): poll with `kill -0` and /proc, and
# `wait` only for the exit code of one that is gone already.
FG=; ESPERA_RC=
vivo() {   # $1 = pid: alive and not a zombie. Only stat, by the builtin: cmdline and environ can block on a process in D
  local s; [ -n "$1" ] || return 1
  read -r s 2>/dev/null < "/proc/$1/stat" || return 1; s=${s##*) }; [ "${s%% *}" != Z ]
}
espera() {   # $1 = child pid, $2 = limit in s: 0 = it ended (its exit code in ESPERA_RC), 1 = still alive after the limit
  local up t0 d=0.05
  read -r up _ < /proc/uptime; t0=${up%.*}          # monotonic: the WSL wall clock steps
  while vivo "$1"; do
    read -r up _ < /proc/uptime; [ $(( ${up%.*} - t0 )) -ge "$2" ] && return 1
    sleep "$d" & SONO=$!; wait "$SONO" || kill "$SONO" 2>/dev/null; SONO=   # a trapped signal cuts it at once
    case $d in 0.05) d=0.1;; 0.1) d=0.2;; 0.2) d=0.5;; *) d=1;; esac
  done
  wait "$1"; ESPERA_RC=$?; return 0
}
arvore() { local c; echo "$1"; for c in $(pgrep -P "$1" 2>/dev/null); do arvore "$c"; done; }
onde_preso() {   # $1 = pid: "<pid> <state> <wchan>" of the first thread in D in its tree, else of its deepest live process
  local q t s w last=
  for q in $(arvore "$1"); do
    vivo "$q" || continue; last=$q
    for t in /proc/"$q"/task/*; do
      read -r s 2>/dev/null < "$t/stat" || continue; s=${s##*) }
      if [ "${s%% *}" = D ]; then w=; read -r w 2>/dev/null < "$t/wchan"; [ "${w:-0}" = 0 ] && w=?; echo "$q D $w"; return 0; fi
    done
  done
  [ -n "$last" ] || { echo "$1 ? ?"; return 0; }
  s=; w=; read -r s 2>/dev/null < "/proc/$last/stat"; s=${s##*) }; read -r w 2>/dev/null < "/proc/$last/wchan"; [ "${w:-0}" = 0 ] && w=?
  echo "$last ${s%% *} $w"
}
abandona() {   # $1 = pid: print where it is stuck, then SIGKILL its tree (it lands when the drive answers); never waited
  onde_preso "$1"; mata_arvore "$1" KILL; kill -KILL "$1" 2>/dev/null; return 0
}
ate_hard() {   # $1 = a limit in s: the same, or the time left before the cycle's hard line (5 s kept to leave), at least 1
  local r; [ -n "$CYCLE_T0" ] || { echo "$1"; return; }
  r=$(( CYCLE_CAP - 5 - $(cycle_elapsed) )); [ "$r" -lt 1 ] && r=1
  [ "$r" -lt "$1" ] && echo "$r" || echo "$1"
}
fg_wait() {   # "$@" in the background; a trapped signal cuts the wait at once. Left behind after FG_MAX_S (exit 125)
  local rc lim=${FG_MAX_S:-60} w; case "$lim" in ''|*[!0-9]*|0) lim=60;; esac
  lim=$(ate_hard "$lim")   # PLN0235: never past the cycle's hard line
  "$@" & FG=$!
  if espera "$FG" "$lim"; then rc=$ESPERA_RC
  else rc=125; w=$(abandona "$FG"); printf '%s\t%s\n' "$(date +%FT%T)" "chamada presa abandonada apos ${lim}s: ${*:1:6} (pid/estado/wchan $w)" >> "$D/avisos.log"; fi
  FG=; return "$rc"
}
PL() { fg_wait env PYTHONPATH="$S" timeout 25 python3 -m watch_core.planou --agent "$INST" --root "$ROOT" "$@"; }
PLW() { PYTHONPATH="$S" timeout --foreground $(( $1 + 20 )) python3 -m watch_core.planou --agent "$INST" --root "$ROOT" poll --wait "$1" \
  --next-heavy "$prox" --poll-s "$POLL"; }
PT() { fg_wait env PYTHONPATH="$S" timeout 25 python3 -m watch_core.transcript --agent "$INST" --root "$ROOT" pump 2>> "$D/planou.err"; }
TPATH=$ROOT/cache/planou/transcript.path   # written by the pump: the session transcript
tsig() { stat -c '%i %s %Y' "$(cat "$TPATH" 2>/dev/null)" 2>/dev/null; }
# One runner per instance (PLN0082): the check "runner.pid names a live runner" alone had a race (two launches in a row
# both passed it before either wrote runner.pid). Now runner.lock, taken before anything else, while the runner lives.
# The lock is held by a small holder in this runner's group, never by the runner itself: bash cannot mark an fd
# close-on-exec, and a lock fd inherited by the heavy tick, the long poll or a sleep would outlive a runner killed with -9
# (the lesson of the build server that held the test lock). `flock -o` keeps the lock out of the `sh` it runs, which
# checks this runner's pid every second and leaves within a second of its death (kill -9 included, a zombie counts as
# dead), so the lock goes with it; a stop and the exit take the holder down with the group. Lock taken by another:
# runner.pid naming a live runner = already running (leave at once); otherwise the other one is leaving (a relaunch
# right after a wake or a stop, a runner killed with -9) or has not written runner.pid yet: wait for it, up to 60 s.
# RUNNER_LOCK_HELD: the rollout exec below keeps the pid, and so the lock.
trava() {   # 0 = this runner holds runner.lock
  local ok=
  exec 8< <(exec flock -n -o "$D/runner.lock" sh -c 'echo ok; exec >/dev/null
    while read -r s < "/proc/$1/stat"; do case "${s##*) }" in Z*) exit;; esac; sleep 1; done' _ $$ </dev/null 2>/dev/null)
  read -r -t 10 -u 8 ok; exec 8<&-
  [ "$ok" = ok ]
}
if [ "$RUNNER_LOCK_HELD" != "$$" ]; then
  for i in $(seq 1 120); do
    trava && break
    if eh_runner "$(cat "$D/runner.pid" 2>/dev/null)"; then echo "runner $INST ja rodando (pid $(cat "$D/runner.pid"))"; exit 0; fi
    if [ "$i" = 120 ]; then
      o=$(runners | tr '\n' ' '); [ -n "$o" ] && { echo "runner $INST ja rodando (pid ${o% })"; exit 0; }
      echo "runner: $INST: $D/runner.lock preso ha 60 s sem runner vivo"; exit 1
    fi
    sleep 0.5
  done
fi
unset RUNNER_LOCK_HELD
# a runner of a version before the lock (it never takes it) still counts
if eh_runner "$(cat "$D/runner.pid" 2>/dev/null)"; then echo "runner $INST ja rodando (pid $(cat "$D/runner.pid"))"; exit 0; fi
# Rollout (PLN0056, watch_core.rollout): with ~/.config/team/rollout.json naming a canary for this plugin, the runner runs
# a snapshot of ONE version (~/.cache/team/plugins/<plugin>@<version>), never the shared copy on disk: the canary the
# version on disk, the others the last released one. ROLLOUT_LIVE = the plugin folder on disk, checked after each heavy
# tick. Without rollout.json (or without a canary for the plugin) nothing changes. After the "already running" check:
# a second launch must not snapshot, mark a canary or rewrite runner.version.
if [ -z "$ROLLOUT_LIVE" ]; then
  mkdir -p "$D"
  RO=$(PYTHONPATH="$S" timeout 180 python3 -m watch_core.rollout start --scripts "$S" --agent "$INST" --runner-dir "$D" 2>> "$D/rollout.err")
  case "$RO" in
    STOP=*) echo "runner: $INST: ${RO#STOP=}"; exit 1;;
    PIN=*) RUNNER_LOCK_HELD=$$ ROLLOUT_LIVE=$(cd -P "$S/../../.." && pwd -P) exec bash "${RO#PIN=}/runner.sh" "$@";;
  esac
fi
echo $$ > "$D/runner.pid"; rm -f "$D/runner.stop"

# volta leve so' com o Planou configurado, live e com chave (conferido sem rede)
planou=0; PL active >/dev/null 2>&1 && planou=1

# the Claude Code session this runner belongs to: the first ancestor with ~/.claude/sessions/<pid>.json
SDIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/sessions"   # a session opened with another config folder
SPID=; SSTART=; SID=; p=$PPID
for _ in $(seq 1 32); do
  [ "${p:-0}" -gt 1 ] 2>/dev/null || break
  if [ -f "$SDIR/$p.json" ]; then SPID=$p; break; fi
  p=$(pstat "$p" | cut -d' ' -f2)
done
if [ -n "$SPID" ]; then
  SSTART=$(pstat "$SPID" | cut -d' ' -f20)
  SID=$(sed -n 's/.*"sessionId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SDIR/$SPID.json" 2>/dev/null | head -1)
fi
sessao_viva() {   # true without a known session (never close what we did not find)
  [ -z "$SPID" ] && return 0
  local st; st=$(pstat "$SPID") || return 1
  [ "${st%% *}" != Z ] && [ "$(echo "$st" | cut -d' ' -f20)" = "$SSTART" ]
}
fecha_sessao() {   # the session is gone: tell Planou (only with the light loop on) and leave without waking anyone
  stop_poll
  [ "$planou" = 1 ] && PYTHONPATH="$S" timeout 8 python3 -m watch_core.planou --agent "$INST" --root "$ROOT" heartbeat session_closed \
    ${SID:+--session-id "$SID"} >/dev/null 2>&1
  printf '%s\t%s\n' "$(date +%FT%T)" "sessao $SID (pid $SPID) encerrada: session_closed" >> "$D/avisos.log"
  [ "$(cat "$D/runner.pid" 2>/dev/null)" = "$$" ] && rm -f "$D/runner.pid"
  exit 0
}
SONO=
# a sleep a signal interrupts at once; with `poll`, the end of the long poll ends it too: the poll's subshell writes a
# line to fd 3 (a pipe) and the check step reads it with `read -t` instead of sleeping (a poll that ended while the loop
# was busy is still there to read). Up to 0.29.x it sent USR1: on bash 5.2 a trapped signal that lands while bash parses
# a $(...) breaks the parse ("unexpected EOF while looking for matching `)'") and the runner died with exit 1.
POLL_DONE=0; FIFO_OK=0
mkfifo "$D/poll.fifo.$$" 2>/dev/null && exec 3<>"$D/poll.fifo.$$" && FIFO_OK=1; rm -f "$D/poll.fifo.$$"
# PLN0133: bash 5.1 runs a trap that lands during `read -t` inside the read, and the read's own timer (SIGALRM) cuts it
# short with a longjmp. no_sinal had already ignored HUP/TERM/INT by then, so the runner went on deaf to TERM and a stop
# waited its whole 30 s (a cut inside malloc may also abort bash: exit 134). Inside the read the trap only takes note and
# writes a line to fd 3, so the read returns at once; the whole no_sinal runs right after it, outside the read.
EM_READ=; SINAL=
# PLN0081: a stop sends two TERMs in a row (the group, then the runner): outside the read the handler ignores
# HUP/TERM/INT before anything else, so the second one never lands inside the first one's exit. Inside the read it
# must not (PLN0133: the read's timer could cut the handler right after and leave the runner deaf).
sinal() { SINAL=1; if [ -n "$EM_READ" ]; then echo sinal >&3; else trap '' HUP TERM INT; no_sinal; fi; }
dorme() {
  if [ "$2" = poll ] && [ "$FIFO_OK" = 1 ]; then
    [ "$POLL_DONE" = 1 ] && return 0
    local l rc; EM_READ=1; read -r -t "$1" -u 3 l; rc=$?; EM_READ=
    [ -n "$SINAL" ] && no_sinal
    [ "$rc" = 0 ] && POLL_DONE=1; return 0
  fi
  sleep "$1" & SONO=$!; wait "$SONO" || { [ $? -gt 128 ] && kill "$SONO" 2>/dev/null; }; SONO=
}
POLL_PID=
stop_poll() {   # never a blocking wait (PLN0235): a poll stuck in D is left behind after 5 s
  [ -n "$POLL_PID" ] && { pkill -TERM -P "$POLL_PID" 2>/dev/null; kill "$POLL_PID" 2>/dev/null; espera "$POLL_PID" 5 || abandona "$POLL_PID" >/dev/null; }
  POLL_PID=
}
mata_filhos() {   # the whole tree and the whole group (the long poll, the sleep, the heavy tick), never the runner itself
  trap '' HUP TERM INT
  mata_arvore $$ TERM
  [ "$(ps -o pgid= -p $$ | tr -d ' ')" = "$$" ] && kill -TERM -- -$$ 2>/dev/null
  # PLN0081: wait for the children to go (1 s at most, then KILL), so the runner never exits with a curl or a python
  # of its own still in a call (a stop of an older runner ended in "longjmp causes uninitialized stack frame", exit 134)
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do live_children || break; sleep 0.1; done
  live_children && mata_arvore $$ KILL
  SONO=; POLL_PID=; FG=
}
live_children() {   # a child of the runner that is not a zombie yet (pgrep -P lists zombies too)
  local c; for c in $(pgrep -P $$ 2>/dev/null); do case "$(cut -d' ' -f3 "/proc/$c/stat" 2>/dev/null)" in ''|Z) ;; *) return 0;; esac; done
  return 1
}
no_sinal() {
  mata_filhos
  [ "$(cat "$D/runner.pid" 2>/dev/null)" = "$$" ] && rm -f "$D/runner.pid"   # a relaunch may start right away
  grep -qx "$$" "$D/runner.stop" 2>/dev/null && exit 143                     # `runner.sh <inst> stop`: the session goes on
  # closing the terminal takes the session down around the same time: a detached watcher gives it 10 s to go (and then
  # sends session_closed); the runner itself leaves now
  if [ -n "$SPID" ]; then
    trap - EXIT                           # the children are gone already: the exit must not take the watcher too
    ( trap - HUP TERM INT; for _ in $(seq 1 20); do sessao_viva || fecha_sessao; sleep 0.5; done ) </dev/null >/dev/null 2>&1 &
  fi
  exit 143
}
trap sinal HUP TERM INT
trap mata_filhos EXIT

# quando roda o proximo tick pesado: agora num lancamento novo; num relancamento, o horario que o ultimo tick pesado
# deixou em next_heavy (uma acordada do Planou nao o empurra; sem o arquivo, o comportamento antigo: um intervalo a partir de agora)
agora=$(date +%s)
WAKE_FIRST=   # PLN0247: the first heavy tick of a session (not of a cycle or a relaunch) never keeps blocks waiting
if [ "${FIRST_NOW:-${PRIMEIRO_IMEDIATO:-1}}" = 1 ]; then prox=$agora; WAKE_FIRST=--first
else prox=$(cat "$D/next_heavy" 2>/dev/null); case "$prox" in ''|*[!0-9]*) prox=$(( agora + INTERVALO ));; esac; fi

# PLN0155 (d-145: 117 wakes in 24 h that only read and relaunched): lines that ask nothing of the session never wake it
# alone; they wait in $D/adiado and go at the end of the next tick.out that wakes, under `== SEM ACAO`, indented (no line
# of it starts like a token acorda looks for: `== PLANOU (`, `-- MENSAGEM`, `-- FILA `). What waits:
#   - the heavy tick's `== PLANOU` header and its `PLANOU: <PID> alterada pelo usuario|Planou ...` lines (the edit is
#     applied to the state already; concluida, reaberta, apagada, atribuida and every other line still wake);
#   - `-- ALERTA VISTO ...` (the person saw an alert), from the light loop or the heavy tick;
#   - `== DEPLOY JA AVISADO (n)` and its lines: a deploy of the release this session closed (`--release-queue done`), which
#     the session announced when the integrator came back (hook deploy_log). `== DEPLOY NOVO` still wakes.
#   - `== ALERTAS SEM ACORDAR (...)` and its indented lines (PLN0160): alert rules the instance listed in the alertas_azure
#     hook's nao_acordar, and the blocks of nao_acordar_junto they alone set off. They also go to avisos.log every tick.
# A cycle (`== RUNNER CICLO`) leaves them waiting. At most ADIADO_MAX lines are kept (the oldest go).
QUIET_WAIT=(-e '^== PLANOU$' -e '^PLANOU: [^ ]* alterada pelo \(usuario\|Planou\)' -e '^-- ALERTA VISTO ')
ADIADO_MAX=200
adia() {   # stdin: lines that wait for the next wake
  local h l; h=$(date +%H:%M)
  while IFS= read -r l; do [ -n "${l//[[:space:]]/}" ] && printf '   %s %s\n' "$h" "$l"; done >> "$D/adiado"
  [ "$(wc -l < "$D/adiado" 2>/dev/null || echo 0)" -gt "$ADIADO_MAX" ] && tail -n "$ADIADO_MAX" "$D/adiado" > "$D/adiado.tmp" && mv "$D/adiado.tmp" "$D/adiado"
  return 0
}
# the tick without what waits and without the email-only-automatic block: what is left asks for the session
acao() {   # $1 = file
  awk '/^== E-MAIL \(0 de pessoas, [0-9]+ automaticos\)$/ {so_auto=1; next} so_auto && /^   · / {next} {so_auto=0}
       /^== DEPLOY JA AVISADO \([0-9]+\)$/ {ja=1; next} ja && /^(-- |   )/ {next} {ja=0}
       /^== ALERTAS SEM ACORDAR \(/ {sa=1; next} sa && /^   / {next} {sa=0; print}' "$1" | grep -v "${QUIET_WAIT[@]}"
}
sem_acordar() {   # $1 = file: the quiet alert blocks (PLN0160)
  awk '/^== ALERTAS SEM ACORDAR \(/ {sa=1; print; next} sa && /^   / {print; next} {sa=0}' "$1"
}
quietas() {   # $1 = file: the lines of it that wait (what acao leaves out, minus the header and the automatic email)
  awk '/^== DEPLOY JA AVISADO \([0-9]+\)$/ {ja=1; print; next} ja && /^(-- |   )/ {print; next} {ja=0}
       /^PLANOU: [^ ]* alterada pelo (usuario|Planou)/ || /^-- ALERTA VISTO / {print}' "$1"
  sem_acordar "$1"
}

acorda() {   # $1 = exit code para o tick.out; o conteudo ja esta em tick.tmp
  local solto=   # the grouped blocks (PLN0247) go first, as content; the buffer goes only after tick.out was written
  [ -s "$ROOT/data/retido.json" ] && timeout "$(ate_hard 60)" python3 "$S/wake.py" release --root "$ROOT" > "$D/retido.out" 2>> "$D/avisos.log" && solto=1
  { [ "$solto" = 1 ] && cat "$D/retido.out"
    cat "$D/tick.tmp"
    if [ -s "$D/adiado" ]; then echo; echo "== SEM ACAO ($(wc -l < "$D/adiado") linhas adiadas: so leitura, nada a fazer)"; cat "$D/adiado"; fi
    echo "EXIT=$1 $(date +%H:%M)"; } > "$D/tick.out"
  [ "$solto" = 1 ] && rm -f "$ROOT/data/retido.json" "$D/retido.out"
  rm -f "$D/adiado"
  if [ "$planou" = 1 ]; then
    grep -q '^== PLANOU (' "$D/tick.out" && PL cursor-commit >/dev/null 2>&1
    PL heartbeat woke --next-heavy "$prox" >/dev/null 2>&1
  fi
  rm -f "$D/runner.pid"
  cat "$D/tick.out"
  # a mensagem do usuario (e a tarefa da fila, o comentario com @) so' e' confirmada ao Planou depois de impressa para a sessao
  [ "$planou" = 1 ] && grep -q -e '^-- MENSAGEM do usuario pelo Planou' -e '^-- FILA ' -e '^-- COMENTARIO ' "$D/tick.out" && PL ack-delivered >/dev/null 2>> "$D/planou.err"
  exit 0
}

# PLN0087: the snapshot this runner runs (~/.cache/team/plugins/agent@<version>) was removed under it. bash goes on
# reading this script from the open file, but the tick script and watch_core are gone: instead of a heavy tick that
# breaks with exit 2 (and Planou calls with ModuleNotFoundError), back to the stable copy on disk (ROLLOUT_LIVE) for the
# Planou calls of acorda and wake the session with `== RELANCAR`; the relaunch picks a snapshot again. $1 = keep: after a
# heavy tick (its output stays in tick.tmp).
sem_copia() {
  [ -n "$ROLLOUT_LIVE" ] || return 1
  [ -f "$V" ] && [ -d "$S/watch_core" ] && return 1
  local de=${S#*/agent@}; de=${de%%/*}; [ "$de" = "$S" ] && de='?'
  S=$ROLLOUT_LIVE/skills/agent/scripts
  [ "$1" = keep ] || : > "$D/tick.tmp"
  { echo "== RELANCAR (agent $de, copia fixa apagada do cache)"
    echo "-- a copia fixa que este runner rodava sumiu de ~/.cache/team/plugins: relancar o runner como sempre (stop e start); nada mais a fazer."
  } >> "$D/tick.tmp"
  printf '%s\t%s\n' "$(date +%FT%T)" "copia fixa $de apagada do cache: RELANCAR" >> "$D/avisos.log"
  acorda 0
}

# Relaunch on its own (PLN0155): a heavy tick with nothing for the session whose rollout says only `== RELANCAR` (a new
# version for this agent) used to wake the session just for the stop and start. The runner now does it itself when it
# is safe: exec of the runner on disk (same pid, so still the session's child, same runner.lock holder, same cycle clock,
# next_heavy kept), which picks the snapshot again, exactly as the session's start would. Safe = the instructions the
# session holds did not change: every .md of the skill (SKILL.md, behaviors) and of the plugin's docs/ is the same in the version it runs and in
# the one it goes to (the version on disk, or a snapshot already in the cache). Otherwise, a refusal (VERSAO RECUSADA),
# anything else in the tick, or a 3rd relaunch of one process: wake as before. The switch goes to avisos.log and, as a
# line of `== SEM ACAO`, with the next wake.
docs_sig() {   # $1 = plugin folder: one md5 of the instructions (every .md of skills/agent and of docs/; the CHANGELOG and
  # the READMEs change with every version and are not read by the session). Fails when the skill is missing.
  [ -d "$1/skills/agent" ] || return 1
  ( cd "$1" && find skills/agent docs -name '*.md' -type f 2>/dev/null | LC_ALL=C sort | xargs -r md5sum ) | md5sum | cut -d' ' -f1
}
so_relancar() {   # $1 = rollout output: ACTION=relaunch with a plain `== RELANCAR (<p> <de> -> <para>, ...)` block
  [ "$(printf '%s\n' "$1" | sed -n 's/^ACTION=//p')" = relaunch ] || return 1
  printf '%s\n' "$1" | grep -v -e '^ACTION=' -e '^== RELANCAR (' -e '^-- versao nova para este agente' -e '^-- rollout: ' | grep -q . && return 1
  return 0
}
relanca_sozinho() {   # $1 = rollout output; returns (and the session is woken as before) when it is not safe
  [ -n "$ROLLOUT_LIVE" ] && [ -f "$ROLLOUT_LIVE/skills/agent/scripts/runner.sh" ] || return 1
  [ "${RUNNER_SELF_RELAUNCH:-0}" -lt 2 ] 2>/dev/null || return 1
  [ "$(cycle_elapsed)" -lt "$CYCLE_S" ] || return 1   # PLN0235: the new start (rollout included) must fit before HARD
  local head para disk alvo a b
  head=$(printf '%s\n' "$1" | grep -m1 '^== RELANCAR (')
  para=$(printf '%s\n' "$head" | sed -n 's/^== RELANCAR ([^ ]* [^ ]* -> \([^,)]*\).*/\1/p')
  disk=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ROLLOUT_LIVE/.claude-plugin/plugin.json" 2>/dev/null | head -1)
  [ -n "$para" ] || return 1
  if [ "$para" = "$disk" ]; then alvo=$ROLLOUT_LIVE
  else alvo=${XDG_CACHE_HOME:-$HOME/.cache}/team/plugins/agent@$para; fi
  a=$(docs_sig "$S/../../..") && b=$(docs_sig "$alvo") && [ "$a" = "$b" ] || return 1
  echo "$prox" > "$D/next_heavy"
  printf '%s\t%s\n' "$(date +%FT%T)" "relancado sozinho: ${head#== }" >> "$D/avisos.log"
  printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" relancou 0 0 >> "$D/metricas.tsv"
  quietas "$D/tick.tmp" | adia
  printf '%s\n' "runner relancado sozinho: ${head#== } (mesmas instrucoes)" | adia
  rm -f "$D/runner.pid"
  exec 3<&-
  RUNNER_SELF_RELAUNCH=$(( ${RUNNER_SELF_RELAUNCH:-0} + 1 )) RUNNER_LOCK_HELD=$$ FIRST_NOW=0 PRIMEIRO_IMEDIATO= \
    exec env -u ROLLOUT_LIVE bash "$ROLLOUT_LIVE/skills/agent/scripts/runner.sh" "${RUNNER_ARGS[@]}"
}

# Cycle (PLN0144, see the header): the start of this process in seconds since boot, the same clock as /proc/uptime
CYCLE_S=${RUNNER_MAX_S:-1500}; case "$CYCLE_S" in ''|*[!0-9]*|0) CYCLE_S=1500;; esac
# PLN0235: CYCLE_CAP = HARD, the line every exit comes before; RESERVE = the time kept after a heavy tick
MARGIN=${RUNNER_MARGIN_S:-240}; case "$MARGIN" in ''|*[!0-9]*) MARGIN=240;; esac
CYCLE_CAP=$(( CYCLE_S + MARGIN ))
RESERVE=${RUNNER_RESERVE_S:-120}; case "$RESERVE" in ''|*[!0-9]*) RESERVE=120;; esac
[ "$RESERVE" -gt $(( CYCLE_CAP / 2 )) ] && RESERVE=$(( CYCLE_CAP / 2 ))
TICK_END=$(( CYCLE_CAP - RESERVE ))                      # a heavy tick ends before this, or it is stuck
m=$(( TICK_END - 20 - 60 )); [ "$m" -lt 10 ] && m=10      # a runner that just started (< 60 s) always fits a tick
[ "$TEMPO" -gt "$m" ] && TEMPO=$m
CYCLE_T0=$(( $(pstat $$ | cut -d' ' -f20) / $(getconf CLK_TCK 2>/dev/null || echo 100) ))
cycle_elapsed() { local up; read -r up _ < /proc/uptime; echo $(( ${up%.*} - CYCLE_T0 )); }
cycle_due() {   # 0 = leave now for a new cycle
  local e; e=$(cycle_elapsed)
  if [ "$(date +%s)" -ge "$prox" ]; then [ "$e" -ge 60 ] && [ $(( e + TEMPO + 20 )) -gt "$TICK_END" ]
  else [ "$e" -ge "$CYCLE_S" ]; fi
}
# Stuck tick (PLN0241, see the header)
TICK_STUCK_S=${RUNNER_TICK_STUCK_S:-}; case "$TICK_STUCK_S" in ''|*[!0-9]*|0) TICK_STUCK_S=$(( TEMPO + 30 ));; esac
PRESO=$D/tick.preso; TICK=; PRESO_VISTO=
preso_vivo() {   # 0 = the tick left behind is alive (same pid and start time); a dead one is cleared once, with a line
  local p st de es w
  read -r p st de es w 2>/dev/null < "$PRESO" || return 1
  vivo "$p" && [ "$(pstat "$p" | cut -d' ' -f20)" = "$st" ] && return 0
  rm -f "$PRESO"
  printf '%s\t%s\n' "$(date +%FT%T)" "tick preso (pid $p, $w) terminou: os ticks pesados voltaram" >> "$D/avisos.log"
  echo "tick preso desde $(date -d "@$de" +%H:%M 2>/dev/null) (pid $p, $w) terminou: os ticks pesados voltaram" | adia
  return 1
}
abandona_tick() {   # the heavy tick passed its limit: leave it behind, one alert, one wake (acorda exits)
  local p es w st desde
  read -r p es w <<< "$(abandona "$TICK")"; TICK=
  st=$(pstat "$p" | cut -d' ' -f20)
  mv -f "$D/tick.tmp" "$D/tick.preso.out" 2>/dev/null   # it keeps writing to its own file, never to the next tick's
  echo "$p ${st:-0} $t0 $es $w" > "$PRESO"
  desde=$(date -d "@$t0" +%H:%M)
  printf '%s\t%s\n' "$(date +%FT%T)" "tick preso: pid $p ($es, $w) desde $desde, abandonado apos $(( $(date +%s) - t0 ))s" >> "$D/avisos.log"
  printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" preso 0 "$(( $(date +%s) - t0 ))" >> "$D/metricas.tsv"
  [ "$planou" = 1 ] && PL alert --title "tick de $INST preso ($w)" --severity medium --code tick_stuck --ref "tick-preso:$p:${st:-0}" \
    --context "O tick pesado de $INST nao termina desde $desde (pid $p, estado $es, wchan $w). O runner seguiu sem ele e nao abre outro tick enquanto esse processo viver. Estado D com p9_client_rpc: um drive do Windows (/mnt/<letra>) parou de responder." \
    >/dev/null 2>> "$D/planou.err"
  { echo "== TICK PRESO (pid $p, desde $desde, $w)"
    echo "-- o tick pesado passou de ${TICK_STUCK_S}s sem terminar (estado $es). O runner seguiu sem ele: nao abre outro tick enquanto o pid $p viver, e a volta vai no avisos.log. Alerta aberto no Planou. Estado D (p9_client_rpc) = drive do Windows sem resposta; so' volta quando o drive responder ou o WSL reiniciar. Relancar o runner como sempre."
  } > "$D/tick.tmp"
  acorda 0
}
cycle_out() {   # no Planou call: nothing was delivered, nothing to acknowledge; the EXIT trap takes the children down
  echo "$prox" > "$D/next_heavy"
  printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" ciclo 0 "$(cycle_elapsed)" >> "$D/metricas.tsv"
  { echo '== RUNNER CICLO (relancar sem tratar)'; echo "EXIT=0 $(date +%H:%M)"; } > "$D/tick.out"
  [ "$(cat "$D/runner.pid" 2>/dev/null)" = "$$" ] && rm -f "$D/runner.pid"
  echo '== RUNNER CICLO (relancar sem tratar)'
  exit 0
}

while true; do
  sem_copia
  cycle_due && cycle_out
  agora=$(date +%s)
  if [ "$agora" -lt "$prox" ]; then
    falta=$(( prox - agora ))
    if [ "$planou" = 1 ]; then
      # light loop: sign of life + the person's answers and messages; never the sources, never the model.
      # Long poll (0.27.0): Planou holds the request until something comes (a message right when its 5 s undo closes)
      # or WAIT s pass, always ending before the heavy tick; it runs in the background while the checks go on every STEP s.
      w=$(( falta - 2 )); [ "$w" -gt "$WAIT" ] && w=$WAIT
      if [ "$w" -lt "$STEP" ]; then      # the heavy tick is a few seconds away: just wait for it
        dorme "$falta"; sessao_viva || fecha_sessao; continue
      fi
      rm -f "$D/planou.rc"; read -r up _ < /proc/uptime; t_poll=${up%.*}
      POLL_DONE=0; while read -r -t 0 -u 3 2>/dev/null; do read -r -u 3 _; done   # nothing left from a poll cut short
      ( PLW "$w" > "$D/planou.tmp" 2>> "$D/planou.err"; echo $? > "$D/planou.rc"; echo done >&3 ) &
      POLL_PID=$!
      # conversation: re-resolve the session once per poll, then a stat every STEP s; Python only when the transcript changed
      # the stat before the read: a write during it is read next time (the first time, when the transcript was not known
      # yet, the next check reads it again)
      pre=$(tsig); PT; sig_t=${pre:-unknown}
      while [ "$POLL_DONE" != 1 ] && kill -0 "$POLL_PID" 2>/dev/null; do
        r=$(( $prox - $(date +%s) ))
        if [ "$r" -le 0 ]; then stop_poll; echo 0 > "$D/planou.rc"; break; fi   # the heavy tick never waits for Planou
        # PLN0235: a poll past every normal length at the cycle's end (a hung one) never holds the runner past HARD
        if [ "$(cycle_elapsed)" -ge "$TICK_END" ]; then stop_poll; echo 0 > "$D/planou.rc"; break; fi
        dorme $(( r < STEP ? r : STEP )) poll
        sessao_viva || fecha_sessao
        s_t=$(tsig); if [ -n "$s_t" ] && [ "$s_t" != "$sig_t" ]; then sig_t=$s_t; PT; fi
      done
      wait "$POLL_PID" 2>/dev/null; POLL_PID=; POLL_DONE=0
      rc=$(cat "$D/planou.rc" 2>/dev/null); rc=${rc:-4}
      date +%FT%T > "$D/ultimo_poll"
      if grep -q '^== PLANOU (' "$D/planou.tmp" && ! grep -v -e '^== PLANOU ([0-9]*)$' -e '^-- ALERTA VISTO ' -e '^[[:space:]]*$' "$D/planou.tmp" | grep -q .; then
        # only lines that wait (PLN0155): printed for good (never again in a poll), kept for the next wake, no wake now
        PL cursor-commit >/dev/null 2>&1
        quietas "$D/planou.tmp" | adia
        printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" "planou-adiado" "$(wc -c < "$D/planou.tmp")" 0 >> "$D/metricas.tsv"
      elif grep -q '^== PLANOU (' "$D/planou.tmp"; then
        cp "$D/planou.tmp" "$D/tick.tmp"
        printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" "planou" "$(wc -c < "$D/tick.tmp")" 0 >> "$D/metricas.tsv"
        acorda 0
      fi
      [ "$(wc -l < "$D/planou.err" 2>/dev/null || echo 0)" -gt 2000 ] && tail -500 "$D/planou.err" > "$D/planou.err.tmp" && mv "$D/planou.err.tmp" "$D/planou.err"
      # 0: Planou waited, ask again now (a quick empty answer: STEP first, never a tight loop). 3: a Planou without long
      # polling, the short interval as before. Anything else (Planou down, 429, timeout): back off from STEP to POLL,
      # never waking the session (a lasting failure comes from the heavy tick as FONTE QUEBRADA (planou)).
      case "$rc" in
        0) fails=0; pause=0; read -r up _ < /proc/uptime; [ $(( ${up%.*} - t_poll )) -lt "$STEP" ] && pause=$STEP;;
        3) fails=0; pause=$POLL;;
        *) fails=$(( ${fails:-0} + 1 )); pause=$(( STEP << (fails < 8 ? fails - 1 : 7) )); [ "$pause" -gt "$POLL" ] && pause=$POLL;;
      esac
      r=$(( prox - $(date +%s) )); [ "$pause" -gt "$r" ] && pause=$r     # never past the heavy tick
      # the pause on a monotonic clock (/proc/uptime): the WSL clock steps back and forth by tens of seconds
      read -r up _ < /proc/uptime; until_m=$(( ${up%.*} + pause ))
      while read -r up _ < /proc/uptime && [ "${up%.*}" -lt "$until_m" ]; do
        [ "$(cycle_elapsed)" -ge "$CYCLE_S" ] && break   # the cycle check is at the top of the loop
        r=$(( until_m - ${up%.*} )); dorme $(( r < STEP ? r : STEP ))
        sessao_viva || fecha_sessao
        s_t=$(tsig); if [ -n "$s_t" ] && [ "$s_t" != "$sig_t" ]; then sig_t=$s_t; PT; fi
      done
    else
      dorme $(( falta < STEP ? falta : STEP ))
      sessao_viva || fecha_sessao
    fi
    continue
  fi

  # a tick left behind still alive (PLN0241): no new one; look again in a minute (or the interval), never wake
  if preso_vivo; then
    agora=$(date +%s); prox=$(( agora + (INTERVALO < 12 * STEP ? INTERVALO : 12 * STEP) )); echo "$prox" > "$D/next_heavy"
    if [ -z "$PRESO_VISTO" ]; then
      PRESO_VISTO=1; printf '%s\t%s\n' "$(date +%FT%T)" "tick pesado adiado: o tick preso (pid $(cut -d' ' -f1 "$PRESO")) ainda vive" >> "$D/avisos.log"
    fi
    continue
  fi
  # heavy tick: agent.py talks to Planou itself (answers acknowledged, tools, sign of life "tick"). Polled, never a
  # blocking wait (PLN0241): past its limit (and never past the cycle's end) it is left behind
  t0=$(date +%s)
  timeout -k 20 "$TEMPO" python3 "$V" "${ARGS[@]}" > "$D/tick.tmp" 2>&1 & TICK=$!
  lim=$TICK_STUCK_S; m=$(( TICK_END - $(cycle_elapsed) )); [ "$m" -lt "$lim" ] && lim=$m; [ "$lim" -lt 1 ] && lim=1
  espera "$TICK" "$lim" || abandona_tick
  rc=$ESPERA_RC; TICK=
  prox=$(( $(date +%s) + INTERVALO )); echo "$prox" > "$D/next_heavy"
  # AVISO de degradacao parcial (429/500 do Teams em alguns chats) sozinho nao acorda o modelo: so' registra
  # Linhas so' informativas nao acordam sozinhas (24/09/2026): transicao de presenca, republicacao automatica da daily e
  # estados de gravacao que nao pedem nada (NAO GRAVADA, GRAVANDO, FINALIZANDO, TRANSCREVENDO); ATA PENDENTE e ACAO SUA acordam.
  # Chat de sala sem mencao (26/09/2026, aprovado pelo usuario: 8 de 17 acordares de 25/09 eram so' isso): o cabecalho
  # `== CHAT (N msgs novas em M conversas)` e as linhas `+N msgs sem mencao em X` nao acordam; conversa com demanda
  # (`-- Nome (dm|grupo|sala) ...`) continua acordando.
  # "AVISO (planou): ..." sozinho nunca acorda (uma falha do Planou que dura vem como FONTE QUEBRADA (planou))
  # E-mail so' de automaticos (28/09/2026, decisao d-36: uma newsletter acordou a sessao): o cabecalho
  # `== E-MAIL (0 de pessoas, N automaticos)` e as linhas `   · ...` logo abaixo dele nao acordam, como a sala sem mencao.
  # E-mail de pessoa (`-- `), qualquer outra linha dentro do bloco e qualquer outro bloco no mesmo tick continuam acordando.
  # (o awk so' tira esse trecho da contagem; o tick.tmp fica inteiro e vai todo para o tick.out quando outra coisa acorda)
  # Ganchos so' informativos (29/09/2026, PLN0102: um tick so' com `LUNCH: status ... ate 13:30` acordou a sessao): o status de
  # almoco posto ou mantido (`LUNCH: status ...`), a presenca do Slack trocada (`PRESENCA: away|auto no Slack`) e
  # `AVISO (sugestoes): ...` (vai no proximo tick, como o AVISO (planou)) nao acordam sozinhos. Falha do gancho
  # (`LUNCH:`/`PRESENCA: nao consegui setar ...`) continua acordando. Essas linhas vao para o avisos.log e, quando outra
  # coisa acorda, saem junto no tick.out.
  # Grouped blocks (PLN0247, see the header): out of tick.tmp into the buffer; DUE= when the buffer must go now
  due=
  if [ "$WAKE" = 1 ]; then
    due=$(timeout "$(ate_hard 60)" python3 "$S/wake.py" hold --root "$ROOT" --dir "$D" $WAKE_FIRST 2>> "$D/avisos.log" | sed -n 's/^DUE=//p')
    [ -n "$due" ] && printf '%s\t%s\n' "$(date +%FT%T)" "agrupadas acordam: $due" >> "$D/avisos.log"
  fi
  WAKE_FIRST=
  QUIET_HOOKS=(-e '^LUNCH: status ' -e '^PRESENCA: \(away\|auto\) no Slack' -e '^AVISO (sugestoes):')
  n=$(acao "$D/tick.tmp" | grep -v -e '^AVISO (teams):' -e '^AVISO (planou):' -e '^[[:space:]]*$' -e '^== CHAT ([0-9]* msgs\{0,1\} novas\{0,1\} em [0-9]* conversas\{0,1\})$' -e '^[[:space:]]*+[0-9]* msgs\{0,1\} sem mencao em ' -e '^== PRESENCA$' -e '^-- APPEAR OFFLINE' -e '^[·-]* *BUSY ligado' -e '^-- daily republicada no Notion' -e '^== GRAVACOES$' -e '^-- \(NAO GRAVADA\|GRAVANDO\|FINALIZANDO\|TRANSCREVENDO\|AGUARDANDO GPU\)' -e '^ *✓ video movido' "${QUIET_HOOKS[@]}" | wc -c)
  [ -n "$due" ] && n=$(( n + $(wc -c < "$ROOT/data/retido.json" 2>/dev/null || echo 1) ))
  grep "${QUIET_HOOKS[@]}" "$D/tick.tmp" | sed "s/^/$(date +%FT%T)\t/" >> "$D/avisos.log"
  sem_acordar "$D/tick.tmp" | sed "s/^/$(date +%FT%T)\t/" >> "$D/avisos.log"
  grep -q '^AVISO (teams):' "$D/tick.tmp" && grep '^AVISO (teams):' "$D/tick.tmp" | sed "s/^/$(date +%FT%T)\t/" >> "$D/avisos.log"
  printf '%s\t%s\t%s\t%s\n' "$(date +%FT%T)" "$rc" "$n" "$(( $(date +%s) - t0 ))" >> "$D/metricas.tsv"
  sem_copia keep
  # Rollout (PLN0056): after the heavy tick (a safe point), the version on disk against the one this runner runs. The canary
  # tests a new version on this tick (released, or refused with a high alert); a runner whose version changed ends with
  # `== RELANCAR` and the session does the usual stop and start (never the runner relaunching itself: it is the session's child).
  forca=0
  rt=$(( $(ate_hard 9999) - 40 )); [ "$rt" -gt 180 ] && rt=180   # PLN0235: short of time, the rollout waits a tick
  if [ -n "$ROLLOUT_LIVE" ] && [ "$rt" -ge 20 ]; then
    RA=$(PYTHONPATH="$S" timeout "$rt" python3 -m watch_core.rollout tick --scripts "$S" --live "$ROLLOUT_LIVE" --agent "$INST" \
      --runner-dir "$D" --rc "$rc" --tick "$D/tick.tmp" 2>> "$D/rollout.err")
    case "$(printf '%s\n' "$RA" | sed -n 's/^ACTION=//p')" in
      relaunch|refused) forca=1; printf '%s\n' "$RA" | grep -v -e '^ACTION=' -e '^ALERT=' >> "$D/tick.tmp";;
      released) printf '%s\n' "$RA" | grep '^-- rollout' | sed "s/^/$(date +%FT%T)\t/" >> "$D/avisos.log";;
    esac
    al=$(printf '%s\n' "$RA" | sed -n 's/^ALERT=//p')
    [ -n "$al" ] && [ "$planou" = 1 ] && PL alert --title "$al" --severity high --code rollout >/dev/null 2>> "$D/rollout.err"
  fi
  # Fonte quebrada (23/09/2026, o Power Automate oscilou 500 <-> ok a cada tick): acorda uma vez por assinatura
  # (quais fontes) a cada 60 min no maximo; a mesma quebra dentro disso so' registra. A volta nao acorda sozinha:
  # vai no avisos.log e sai junto com o proximo tick que tiver conteudo de verdade.
  # Token: FONTE QUEBRADA (ate a 0.18.x, VIGIA QUEBRADO; os dois valem, e a assinatura grava so' o novo)
  sig=$(grep -o -e '^FONTE QUEBRADA ([a-z]*)' -e '^VIGIA QUEBRADO ([a-z]*)' "$D/tick.tmp" | sed 's/^VIGIA QUEBRADO/FONTE QUEBRADA/' | sort -u | tr '\n' ' ')
  outro=$(acao "$D/tick.tmp" | grep -v -e '^FONTE QUEBRADA' -e '^VIGIA QUEBRADO' -e '^AVISO (teams):' -e '^AVISO (planou):' -e '^== PRESENCA$' -e '^-- APPEAR OFFLINE' -e '^-- daily republicada no Notion' -e '^[[:space:]]*$' -e '^ ' -e '^[{}]' "${QUIET_HOOKS[@]}" | wc -c)
  [ -n "$due" ] && outro=$(( outro + 1 ))
  ant=$(sed -n 1p "$D/quebrado.sig" 2>/dev/null | sed 's/VIGIA QUEBRADO/FONTE QUEBRADA/g'); ant_t=$(sed -n 2p "$D/quebrado.sig" 2>/dev/null); agora=$(date +%s)
  # the repeat window: BROKEN_S from the rules (PLN0247), 0 = never again until it comes back; a source that broke again
  # after coming back (`voltou` in quebrado.sig) is new
  rep_s=${BROKEN_S:-3600}; case "$rep_s" in ''|*[!0-9]*) rep_s=3600;; esac
  [ "$rep_s" = 0 ] && grep -q '^voltou' "$D/quebrado.sig" 2>/dev/null && ant=
  if [ -n "$sig" ]; then
    novo=0; for f in $(echo "$sig" | grep -o '([a-z]*)'); do case "$ant" in *"$f"*) ;; *) novo=1;; esac; done   # fonte que ainda nao tinha quebrado
    if [ "$novo" -eq 0 ] && { [ "$rep_s" = 0 ] || [ $(( agora - ${ant_t:-0} )) -lt "$rep_s" ]; } && [ "$outro" -eq 0 ]; then
      printf '%s\t%s\n' "$(date +%FT%T)" "$sig (repetido, nao acordou)" >> "$D/avisos.log"; n=0; rc=0
    else
      printf '%s\n%s\n' "$(echo "$ant $sig" | grep -o -e 'FONTE QUEBRADA ([a-z]*)' -e 'VIGIA QUEBRADO ([a-z]*)' | sed 's/^VIGIA QUEBRADO/FONTE QUEBRADA/' | sort -u | tr '\n' ' ')" "$agora" > "$D/quebrado.sig"
    fi
  elif [ -n "$ant" ] && ! grep -q '^voltou' "$D/quebrado.sig"; then
    echo "voltou $(date +%H:%M)" >> "$D/quebrado.sig"
    printf '%s\t%s\n' "$(date +%FT%T)" "RECUPERADO: $ant" >> "$D/avisos.log"
    [ "$n" -gt 0 ] && echo "FONTE RECUPERADA: $ant voltou a responder" >> "$D/tick.tmp"
  fi
  [ "$forca" = 1 ] && [ "$n" -eq 0 ] && [ "$rc" -eq 0 ] && so_relancar "$RA" && relanca_sozinho "$RA"
  if [ "$n" -gt 0 ] || [ "$rc" -ne 0 ] || [ "$forca" = 1 ]; then acorda "$rc"; fi
  quietas "$D/tick.tmp" | adia     # nothing woke: what waits goes with the next wake (PLN0155)
  date +%FT%T > "$D/ultimo_vazio"
done
