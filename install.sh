#!/bin/sh
# One-command installer of the agent plugin for Linux, WSL and macOS (PLN0192). On Windows, install.ps1 runs this same
# script inside WSL.
#
#   curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh -s -- --base-url URL
#
# It installs, and a second run updates:
#   0. Python (PLN0297): the system's python3 when it is 3.8 or newer; without it, a portable CPython 3.12
#      (python-build-standalone, the release and the SHA-256 of each platform pinned below) in
#      ~/.local/share/planou/python, next to the plugin copy (never inside it: the update swaps that folder whole). The
#      launchers, the runner, the provisioner and its service find it first in PATH;
#   1. the agent plugin: a copy of davibauer/planou-agent (public: the agent as published by the CI of the plugins
#      repository) in ~/.local/share/planou/claude-plugins (the folder keeps its old name: the Windows task and the
#      extension point at it). By default the .tar.gz of its latest GitHub Release, checked against the .sha256 of that
#      release, without git (the provisioner then keeps it up to date the same way: "auto_update"). A copy that is a
#      git clone keeps being updated by git (--git, --repo or --ref ask for git on a new copy). A copy cloned from the
#      old repository (claude-plugins) moves to the public one on its own when it has no local changes,
#      the skills ~/.claude/skills/agent and ~/.claude/skills/work-watch pointing at it, the launchers `team` and
#      `planou-agent` (the employee in this terminal, reopened when the session ends) in ~/.local/bin and
#      ~/.config/team/agents.json (empty) when missing;
#   2. only with --vscode: the Team Terminals extension of VS Code, from
#      extensions/vscode-team-terminals/dist/team-terminals.vsix, with `code --install-extension` (the extension then
#      updates itself from the same copy);
#   3. the local provisioner as a user service: systemd --user on Linux and WSL, launchd on macOS, with "auto_update"
#      on in a new config.json;
#   4. last, it asks for the pairing code of Planou (Configuracoes > Computadores > Conectar este computador), hidden, and
#      turns the service on once this computer is connected. A computer already connected keeps its credential.
# No account is created and no secret is shown: the code goes to `provisioner.py pair` on stdin, the credential goes
# from Planou straight to a 0600 file. Existing links, launchers or copies that are not this installer's stay as they are,
# with one exception (PLN0336): a skill link to an older copy of the plugin that is not a git clone, or is a clone with
# no local work (no change, no stash, every commit on a remote), moves to the installed copy; the line printed says how
# to go back. The planou-agent launcher runs the installed copy when the one in use has no employee.sh or is older, and
# so does the service (the unit's ExecStart, the launchd plist) when it is older or has no provisioner.py.
#
# Options (each one also as an environment variable):
#   --base-url URL     Planou (PLANOU_BASE_URL, default https://app.planou.com)
#   --dir DIR          where the copy lives (PLANOU_INSTALL_DIR, default ~/.local/share/planou/claude-plugins)
#   --git              a new copy by git clone instead of the release archive (PLANOU_INSTALL_GIT=1)
#   --ref REF          branch or tag, by git or by its archive (PLANOU_INSTALL_REF; asks for the git way)
#   --repo URL         git repository (PLANOU_INSTALL_REPO; asks for the git way)
#   --tarball URL      archive used instead of the release (PLANOU_INSTALL_TARBALL; no checksum to check)
#   --no-git           use the archive even with git (PLANOU_INSTALL_NO_GIT=1)
#   --service MODE     auto | none | wsl-task (PLANOU_INSTALL_SERVICE; wsl-task: the Windows task runs the provisioner)
#   --vscode           also install the Team Terminals extension (PLANOU_INSTALL_VSCODE=1; off by default)
#   --no-vscode        skip the extension (the default; kept for older callers)
#   --no-pair          skip the pairing (PLANOU_INSTALL_PAIR=0)
#   --code-stdin       read the pairing code from stdin instead of the terminal (only when running a saved copy)
# Exit: 0 done, 1 an error (the message says which), 3 installed but the pairing failed (run again with a new code).
# Test hooks: PLANOU_INSTALL_OS (linux|wsl|macos), PLANOU_INSTALL_TTY, SYSTEMCTL, LAUNCHCTL, PLANOU_CODE_CLI,
# PLANOU_INSTALL_RELEASES (the releases page of planou-agent), PLANOU_PYTHON_BASE_URL and PLANOU_PYTHON_SHA256 (where
# the portable Python comes from and its checksum), PLANOU_PYTHON_DIR, PLANOU_INSTALL_NO_SYSTEM_PYTHON=1 (act as if
# the system had no python3).
# The text is ASCII on purpose: install.ps1 and old terminals pass it through without mangling accents.
set -eu

BASE_URL=${PLANOU_BASE_URL:-https://app.planou.com}
DEST=${PLANOU_INSTALL_DIR:-$HOME/.local/share/planou/claude-plugins}
REF=${PLANOU_INSTALL_REF:-main}
REPO=${PLANOU_INSTALL_REPO:-https://github.com/davibauer/planou-agent.git}
TARBALL=${PLANOU_INSTALL_TARBALL:-}
NO_GIT=${PLANOU_INSTALL_NO_GIT:-0}
# git only when asked for (a new copy) or when the copy already is a clone; otherwise the release archive
USE_GIT=${PLANOU_INSTALL_GIT:-0}
[ -z "${PLANOU_INSTALL_REPO:-}${PLANOU_INSTALL_REF:-}" ] || USE_GIT=1
RELEASES=${PLANOU_INSTALL_RELEASES:-https://github.com/davibauer/planou-agent/releases}
SERVICE=${PLANOU_INSTALL_SERVICE:-auto}
VSCODE=${PLANOU_INSTALL_VSCODE:-0}
PAIR=${PLANOU_INSTALL_PAIR:-1}
CODE_STDIN=0

say() { printf '%s\n' "$*"; }
warn() { printf 'aviso: %s\n' "$*" >&2; }
die() { printf 'erro: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,49p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) [ $# -ge 2 ] || die "--base-url sem valor"; BASE_URL=$2; shift 2;;
    --base-url=*) BASE_URL=${1#*=}; shift;;
    --dir) [ $# -ge 2 ] || die "--dir sem valor"; DEST=$2; shift 2;;
    --git) USE_GIT=1; shift;;
    --ref) [ $# -ge 2 ] || die "--ref sem valor"; REF=$2; USE_GIT=1; shift 2;;
    --repo) [ $# -ge 2 ] || die "--repo sem valor"; REPO=$2; USE_GIT=1; shift 2;;
    --tarball) [ $# -ge 2 ] || die "--tarball sem valor"; TARBALL=$2; shift 2;;
    --no-git) NO_GIT=1; shift;;
    --service) [ $# -ge 2 ] || die "--service sem valor"; SERVICE=$2; shift 2;;
    --service=*) SERVICE=${1#*=}; shift;;
    --vscode) VSCODE=1; shift;;
    --no-vscode) VSCODE=0; shift;;
    --no-pair) PAIR=0; shift;;
    --code-stdin) CODE_STDIN=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "opcao desconhecida: $1 (veja --help)";;
  esac
done
case "$SERVICE" in auto|none|wsl-task) ;; *) die "--service: use auto, none ou wsl-task";; esac
# the archive of a branch or tag (no checksum to check) only when a ref was asked for; otherwise the release
[ -n "$TARBALL" ] || [ "$USE_GIT" != 1 ] || TARBALL="https://codeload.github.com/davibauer/planou-agent/tar.gz/$REF"
TARBALL_SHA=
BASE_URL=${BASE_URL%/}
case "$BASE_URL" in */v1) ;; *) BASE_URL="$BASE_URL/v1";; esac
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "HOME nao definido"

OS=${PLANOU_INSTALL_OS:-}
if [ -z "$OS" ]; then
  case "$(uname -s)" in
    Linux) if grep -qi microsoft /proc/version 2>/dev/null; then OS=wsl; else OS=linux; fi;;
    Darwin) OS=macos;;
    *) die "sistema nao suportado: $(uname -s). No Windows, use o install.ps1 (PowerShell).";;
  esac
fi

PROV_DIR="$HOME/.config/agent-provisioner"
CRED="$PROV_DIR/secrets/planou.env"
OURS="$DEST/plugins/agent/skills/agent"
SKILLS="$HOME/.claude/skills"
BIN="$HOME/.local/bin"
MARK='planou-install'
LABEL=app.planou.agent-provisioner
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SYSTEMCTL=${SYSTEMCTL:-systemctl}
LAUNCHCTL=${LAUNCHCTL:-launchctl}

# ------------------------------------------------------------------------------------------------ helpers
# download <url> <file>: curl, else wget; nothing else is needed (no git, no python yet)
download() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 2 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "precisa do curl ou do wget para baixar"; fi
}

# sha256_of <file>: its SHA-256 in hex (sha256sum on Linux, shasum on macOS, openssl elsewhere)
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v openssl >/dev/null 2>&1; then openssl dgst -sha256 "$1" | sed 's/^.*= *//'
  fi
}

# ------------------------------------------------------------------------------------------------ 0. python
# The portable CPython (PLN0297): python-build-standalone, release 20261001, CPython 3.12.15, install_only_stripped.
# Each SHA-256 was checked by downloading the file. glibc builds: Alpine and other musl systems need their own python3.
PBS_TAG=20261001
PBS_VERSION=3.12.15
PY_DIR=${PLANOU_PYTHON_DIR:-$HOME/.local/share/planou/python}
PBS_BASE=${PLANOU_PYTHON_BASE_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG}

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; }

system_python() {
  [ "${PLANOU_INSTALL_NO_SYSTEM_PYTHON:-0}" != 1 ] || return 1
  p=$(command -v python3 2>/dev/null) || return 1
  case "$p" in "$PY_DIR"/*) return 1;; esac      # the portable one is checked below, with its marker
  # macOS without the Command Line Tools: /usr/bin/python3 only opens a dialog that offers to install them
  if [ "$OS" = macos ] && [ "$p" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then return 1; fi
  py_ok "$p" || return 1
  printf '%s' "$p"
}

portable_python() {
  case "$(uname -s)/$(uname -m)" in
    Linux/x86_64|Linux/amd64) triple=x86_64-unknown-linux-gnu; sum=7bb1659e3235077b7f63d5b6eb6ce653c6fcd6c5041e9d5f73b42ce10421464d;;
    Linux/aarch64|Linux/arm64) triple=aarch64-unknown-linux-gnu; sum=0b35f4dc08d58534eb82e024989e2db9873885dccd4f2a316ff55c0cec146123;;
    Darwin/x86_64) triple=x86_64-apple-darwin; sum=d101ac54bc34afff54741406261325dc896b7b646a36a58fff4845ef0a00b2ce;;
    Darwin/arm64) triple=aarch64-apple-darwin; sum=10cab8f6ed6202fdd81637aa6eda4af8d5b7eaa8fc42f9df3c6bea4923de0d93;;
    *) die "sem python3 3.8 ou mais novo e sem Python portatil para $(uname -s) $(uname -m): instale o python3 e rode de novo";;
  esac
  [ -z "${PLANOU_PYTHON_SHA256:-}" ] || sum=$PLANOU_PYTHON_SHA256
  file="cpython-$PBS_VERSION+$PBS_TAG-$triple-install_only_stripped.tar.gz"
  if [ -x "$PY_DIR/bin/python3" ] && [ "$(cat "$PY_DIR/.planou-python" 2>/dev/null)" = "$file" ] && py_ok "$PY_DIR/bin/python3"; then
    say "python: portatil $PBS_VERSION em $PY_DIR"
    PY="$PY_DIR/bin/python3"; return
  fi
  say "python: python3 3.8 ou mais novo nao encontrado; baixando o Python portatil $PBS_VERSION (uns 30 MB, so desta vez)"
  tmp="$PY_DIR.new.$$"
  rm -rf "$tmp"; mkdir -p "$tmp"
  url="$PBS_BASE/$(printf '%s' "$file" | sed 's/+/%2B/g')"
  download "$url" "$tmp/python.tar.gz" || { rm -rf "$tmp"; die "nao consegui baixar o Python portatil ($url)"; }
  got=$(sha256_of "$tmp/python.tar.gz")
  [ -n "$got" ] || { rm -rf "$tmp"; die "sem sha256sum, shasum ou openssl para conferir o Python portatil"; }
  [ "$got" = "$sum" ] || { rm -rf "$tmp"; die "o Python portatil baixado nao confere (SHA-256 $got); nada foi instalado"; }
  mkdir -p "$tmp/x"
  tar -xzf "$tmp/python.tar.gz" -C "$tmp/x" || { rm -rf "$tmp"; die "nao consegui extrair o Python portatil"; }
  [ -x "$tmp/x/python/bin/python3" ] || { rm -rf "$tmp"; die "o arquivo do Python portatil nao tem python/bin/python3"; }
  printf '%s\n' "$file" > "$tmp/x/python/.planou-python"
  rm -rf "$PY_DIR.old"
  [ ! -e "$PY_DIR" ] || mv "$PY_DIR" "$PY_DIR.old"
  mkdir -p "$(dirname "$PY_DIR")"
  mv "$tmp/x/python" "$PY_DIR"
  rm -rf "$tmp" "$PY_DIR.old"
  py_ok "$PY_DIR/bin/python3" || die "o Python portatil nao roda neste computador ($PY_DIR/bin/python3)"
  say "python: portatil $PBS_VERSION conferido (SHA-256) em $PY_DIR"
  PY="$PY_DIR/bin/python3"
}

say "== Planou: instalando o plugin agent ($OS)"
if PY=$(system_python); then :; else portable_python; fi
# everything below (and the scripts it starts) finds this python first as python3
PATH="$(dirname "$PY"):$PATH"; export PATH

# ------------------------------------------------------------------------------------------------ 1. the plugin copy
have_git() { [ "$NO_GIT" != 1 ] && git --version >/dev/null 2>&1; }

# release_archive: TARBALL and TARBALL_SHA of the latest release of planou-agent (the tag comes from the redirect of
# releases/latest: no GitHub API, no rate limit), the same files the provisioner's update reads
release_archive() {
  command -v curl >/dev/null 2>&1 || die "precisa do curl para baixar o plugin"
  final=$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$RELEASES/latest") \
    || die "nao consegui ler a versao publicada do plugin ($RELEASES/latest)"
  tag=${final##*/}
  case "$tag" in v[0-9]*.[0-9]*.[0-9]*) ;; *) die "nao achei a versao publicada do plugin em $RELEASES/latest";; esac
  TARBALL="$RELEASES/download/$tag/planou-agent-$tag.tar.gz"
  TARBALL_SHA=$(curl -fsSL "$TARBALL.sha256" 2>/dev/null | cut -d' ' -f1)
  [ -n "$TARBALL_SHA" ] || die "a versao $tag do plugin nao tem o .sha256; nada foi instalado"
}

fetch_tarball() {
  tmp="$DEST.new.$$"
  rm -rf "$tmp" "$tmp.tar.gz"; mkdir -p "$tmp"
  download "$TARBALL" "$tmp.tar.gz" || { rm -rf "$tmp" "$tmp.tar.gz"; die "nao consegui baixar o plugin ($TARBALL)"; }
  if [ -n "$TARBALL_SHA" ]; then
    got=$(sha256_of "$tmp.tar.gz")
    [ "$got" = "$TARBALL_SHA" ] || { rm -rf "$tmp" "$tmp.tar.gz"; die "o plugin baixado nao confere (SHA-256 $got); nada foi instalado"; }
  fi
  tar -xzf "$tmp.tar.gz" -C "$tmp" --strip-components=1 || { rm -rf "$tmp" "$tmp.tar.gz"; die "nao consegui extrair o plugin ($TARBALL)"; }
  rm -f "$tmp.tar.gz"
  [ -f "$tmp/plugins/agent/skills/agent/scripts/provisioner.py" ] || { rm -rf "$tmp"; die "arquivo baixado sem o plugin agent ($TARBALL)"; }
  printf '%s\n' "$TARBALL" > "$tmp/.$MARK"
  rm -rf "$DEST.old"
  [ ! -e "$DEST" ] || mv "$DEST" "$DEST.old"
  mv "$tmp" "$DEST"
  rm -rf "$DEST.old"
}

mkdir -p "$(dirname "$DEST")"
if [ -d "$DEST/.git" ] && have_git; then
  was=$(git -C "$DEST" remote get-url origin 2>/dev/null || true)
  git -C "$DEST" remote set-url origin "$REPO" 2>/dev/null || git -C "$DEST" remote add origin "$REPO"
  export GIT_TERMINAL_PROMPT=0
  git -C "$DEST" fetch -q origin "$REF" || die "nao consegui buscar $REF em $REPO"
  if git -C "$DEST" merge -q --ff-only FETCH_HEAD 2>/dev/null; then
    say "plugin: atualizado em $DEST ($(git -C "$DEST" rev-parse --short HEAD))"
  elif [ "$was" != "$REPO" ] && ! git -C "$DEST" merge-base HEAD FETCH_HEAD >/dev/null 2>&1; then
    # a copy of the old repository (claude-plugins, private now): the public one has another history, so the copy
    # moves over only when nothing of its own would be lost
    [ -z "$(git -C "$DEST" status --porcelain --untracked-files=no)" ] \
      || die "a copia em $DEST veio de $was e tem mudancas locais; resolva la (git status) e rode de novo"
    br=$(git -C "$DEST" symbolic-ref -q --short HEAD || echo main)
    git -C "$DEST" checkout -q -B "$br" FETCH_HEAD \
      || die "nao consegui trocar a copia em $DEST para $REPO; resolva la (git status) e rode de novo"
    say "plugin: copia em $DEST trocada de $was para $REPO ($(git -C "$DEST" rev-parse --short HEAD))"
  else
    die "a copia em $DEST tem mudancas locais ou saiu da $REF; resolva la (git status) e rode de novo"
  fi
elif [ ! -e "$DEST" ] && [ "$USE_GIT" = 1 ] && have_git; then
  git clone -q --branch "$REF" "$REPO" "$DEST" || die "nao consegui clonar $REPO"
  say "plugin: instalado em $DEST ($(git -C "$DEST" rev-parse --short HEAD))"
elif [ ! -e "$DEST" ] || [ -f "$DEST/.$MARK" ]; then
  tag=
  [ -n "$TARBALL" ] || release_archive
  fetch_tarball
  if [ -n "$tag" ]; then say "plugin: instalado em $DEST ($tag, arquivo conferido pelo SHA-256, sem git)"
  else say "plugin: instalado em $DEST (arquivo, sem git)"; fi
else
  die "$DEST existe e nao e uma copia deste instalador; apague ou use --dir"
fi
[ -f "$OURS/scripts/provisioner.py" ] || die "a copia em $DEST nao tem o plugin agent"
VERSION=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "$DEST/plugins/agent/.claude-plugin/plugin.json" 2>/dev/null || echo '?')

# the version of the plugin copy that holds a path (a skill folder or a provisioner.py; links resolved), or ?
copy_version() {
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || echo '?'
import json, os, sys
d = os.path.realpath(sys.argv[1])
for _ in range(6):
    f = os.path.join(d, '.claude-plugin', 'plugin.json')
    if os.path.isfile(f):
        print(json.load(open(f))['version']); break
    d = os.path.dirname(d)
else:
    print('?')
PYEOF
}
# true when version $1 is older than $2 (an unknown version is never older)
older() {
  "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import re, sys
def v(s):
    if not re.match(r'^\d+(\.\d+)*$', s): sys.exit(1)
    return tuple(int(x) for x in s.split('.'))
sys.exit(0 if v(sys.argv[1]) < v(sys.argv[2]) else 1)
PYEOF
}
# how to bring a kept copy up to date: git pull when it is a clone, and the link swap to the installed copy
stale_hint() {
  root=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null || true)
  [ -z "$root" ] || warn "  para atualizar a copia mantida: git -C \"$root\" pull --ff-only"
  [ -z "${2:-}" ] || warn "  ou, para usar a copia instalada: ln -sfn \"$3\" \"$2\""
}

# git_root <dir>: the folder above <dir> (itself included) holding a .git, by looking only (no git: a clone git refuses
# to read, "dubious ownership" on a Windows drive for one, is still a clone); empty when there is none
git_root() {
  d=$1
  while [ -n "$d" ] && [ "$d" != / ]; do
    if [ -e "$d/.git" ]; then printf '%s' "$d"; return; fi
    d=$(dirname "$d")
  done
}
# no_local_work <root>: true only when git says, without an error, that the clone has nothing of its own: no change
# (untracked files count), no stash and no commit that no remote has (a clone without a remote always has some)
no_local_work() {
  git --version >/dev/null 2>&1 || return 1
  st=$(git -C "$1" status --porcelain 2>/dev/null) || return 1
  [ -z "$st" ] || return 1
  ! git -C "$1" rev-parse -q --verify refs/stash >/dev/null 2>&1 || return 1
  n=$(git -C "$1" rev-list --count HEAD --branches --not --remotes 2>/dev/null) || return 1
  [ "$n" = 0 ]
}

# skills: a link this installer made (or a broken one) follows the copy; anything else stays (a clone of development);
# a kept link shows both versions and warns when it is older than the copy just installed (PLN0326). Older and with
# nothing that could be lost (not a clone, or a clone with no local work), it moves to the installed copy on its own
# (PLN0336: a link to a clone stopped at 0.72.0 kept the employee from opening); the old target goes to a file outside
# the skills folder (a second link there would be a second skill) and the line says how to go back.
PREV_LINKS="$HOME/.local/share/planou/previous-links"
link_skill() {
  name=$1 target=$2 link="$SKILLS/$1"
  mkdir -p "$SKILLS"
  if [ -L "$link" ] && [ -e "$link" ]; then
    cur=$(readlink "$link")
    if [ "$cur" = "$target" ]; then say "skill $name: ok ($target)"
    else
      kept=$(copy_version "$link")
      real=$(cd "$link" 2>/dev/null && pwd -P) || real=   # a link to a file: kept, never swapped
      root=$(git_root "$real")
      if [ -n "$real" ] && older "$kept" "$VERSION" && { [ -z "$root" ] || no_local_work "$root"; }; then
        mkdir -p "$PREV_LINKS"
        printf '%s\n' "$cur" > "$PREV_LINKS/$name"
        ln -sfn "$target" "$link"
        if [ -z "$root" ]; then what="uma copia sem git"; else what="um clone sem mudanca local ($root)"; fi
        say "skill $name: trocada para a copia instalada $VERSION ($target); a anterior, $cur na versao $kept, era $what. Para voltar: ln -sfn \"$cur\" \"$link\" (guardado em $PREV_LINKS/$name)"
        return
      fi
      say "skill $name: mantida (aponta para $cur, outra copia do plugin, versao $kept; a instalada em $DEST e a $VERSION)"
      if older "$kept" "$VERSION"; then
        warn "ATENCAO: a skill $name usa a copia em $cur, na versao $kept, mais antiga que a $VERSION recem instalada"
        if [ -z "$real" ]; then warn "  nao troquei sozinho: $cur nao e uma pasta"
        else warn "  nao troquei sozinho: $root tem trabalho local (mudancas, stash ou commits fora do origin) ou o git nao respondeu"; fi
        stale_hint "$real" "$link" "$target"
      fi
    fi
  elif [ -e "$link" ]; then
    say "skill $name: mantida ($link ja existe e nao e link)"
  else
    rm -f "$link"; ln -s "$target" "$link"; say "skill $name: $link -> $target"
  fi
}
link_skill agent "$OURS"
link_skill work-watch "$DEST/plugins/agent/skills/work-watch"
ACTIVE="$SKILLS/agent"
# PLN0336: the service runs the installed copy, not the skill's, while the skill's (a kept clone with local work) is
# older or has no provisioner; the next run of this installer, with the skill up to date, follows the skill again
SVC_WHY=
if [ ! -f "$ACTIVE/scripts/provisioner.py" ]; then SVC_WHY="nao tem o provisionador"
else svc_kept=$(copy_version "$ACTIVE"); if older "$svc_kept" "$VERSION"; then SVC_WHY="esta na versao $svc_kept"; fi; fi
SVC_DIR=$ACTIVE; [ -z "$SVC_WHY" ] || SVC_DIR=$OURS
SVC_BACK="Para voltar a seguir a skill: atualize a copia dela e rode o instalador de novo"

# the team launcher: a stub that runs team.sh of the copy in use (never replaces a launcher of someone else)
mkdir -p "$BIN"
if [ ! -e "$BIN/team" ] || grep -q "$MARK" "$BIN/team" 2>/dev/null; then
  cat > "$BIN/team.tmp.$$" <<'EOF'
#!/usr/bin/env bash
# planou-install: team launcher stub. Runs team.sh of the agent plugin copy in use; rewritten by install.sh.
T="${TEAM_SH:-$HOME/.claude/skills/agent/scripts/team.sh}"
if [ ! -f "$T" ]; then echo "team: nao achei $T (plugin agent)" >&2; exit 1; fi
exec bash "$T" "$@"
EOF
  chmod 755 "$BIN/team.tmp.$$"; mv "$BIN/team.tmp.$$" "$BIN/team"
  say "launcher: $BIN/team"
else
  say "launcher: mantido ($BIN/team ja existe)"
fi
# planou-agent <name>: the employee in this terminal (employee.sh of the copy in use), reopened when the session ends
# PLN0336: when the copy in use has no employee.sh or is older than the installed one, the stub runs the installed one
# (its path written in PLANOU_INSTALLED_AGENT, which join.sh looks for) and says so in one line; PLANOU_EMPLOYEE_SH wins
if [ ! -e "$BIN/planou-agent" ] || grep -q "$MARK" "$BIN/planou-agent" 2>/dev/null; then
  {
    printf '%s\n' '#!/usr/bin/env bash' \
      '# planou-install: planou-agent launcher stub. Runs employee.sh of the agent plugin copy in use; rewritten by install.sh.'
    printf "PLANOU_INSTALLED_AGENT='%s'\n" "$(printf '%s' "$OURS" | sed "s/'/'\\\\''/g")"
    cat <<'EOF'
ver() { sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$(dirname "$1")/../../../.claude-plugin/plugin.json" 2>/dev/null | head -n 1; }
older() {   # version $1 older than $2; an unknown one never is
  [[ $1 =~ ^[0-9]+(\.[0-9]+)*$ && $2 =~ ^[0-9]+(\.[0-9]+)*$ ]] || return 1
  local IFS=. i; local -a a=($1) b=($2)
  for i in 0 1 2 3; do
    [ "${a[i]:-0}" -lt "${b[i]:-0}" ] && return 0
    [ "${a[i]:-0}" -gt "${b[i]:-0}" ] && return 1
  done
  return 1
}
E="${PLANOU_EMPLOYEE_SH:-}"
if [ -z "$E" ]; then
  E="$HOME/.claude/skills/agent/scripts/employee.sh"
  I="$PLANOU_INSTALLED_AGENT/scripts/employee.sh"
  if [ -f "$I" ] && [ "$(cd "$(dirname "$E")" 2>/dev/null && pwd -P)" != "$(cd "$(dirname "$I")" && pwd -P)" ]; then
    why=
    if [ ! -f "$E" ]; then why="nao tem employee.sh"
    elif older "$(ver "$E")" "$(ver "$I")"; then why="esta na versao $(ver "$E")"; fi
    if [ -n "$why" ]; then
      echo "planou-agent: a copia em ~/.claude/skills/agent $why; abrindo pela instalada ($(ver "$I")). A skill /agent ainda vem da outra; para trocar: ln -sfn \"$PLANOU_INSTALLED_AGENT\" ~/.claude/skills/agent" >&2
      E=$I
    fi
  fi
fi
if [ ! -f "$E" ]; then echo "planou-agent: nao achei $E (plugin agent)" >&2; exit 1; fi
exec bash "$E" "$@"
EOF
  } > "$BIN/planou-agent.tmp.$$"
  chmod 755 "$BIN/planou-agent.tmp.$$"; mv "$BIN/planou-agent.tmp.$$" "$BIN/planou-agent"
  say "launcher: $BIN/planou-agent"
else
  say "launcher: mantido ($BIN/planou-agent ja existe)"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN nao esta no PATH: acrescente no ~/.profile para usar os comandos team e planou-agent";; esac

# the team list: the provisioner refuses to work without it; created empty (never overwritten)
if [ ! -e "$HOME/.config/team/agents.json" ]; then
  printf '[]' | "$PY" "$OURS/scripts/team_agents.py" migrate --seed - >/dev/null 2>&1 \
    || die "nao consegui criar ~/.config/team/agents.json"
  say "time: ~/.config/team/agents.json criado"
fi
command -v claude >/dev/null 2>&1 || warn "Claude Code (comando claude) nao encontrado: os agentes precisam dele. Veja https://docs.claude.com/claude-code"

# ------------------------------------------------------------------------------------------------ 2. VS Code
if [ "$VSCODE" = 1 ]; then
  CODE=${PLANOU_CODE_CLI:-}
  if [ -z "$CODE" ]; then
    CODE=code
    mac_code="/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
    command -v code >/dev/null 2>&1 || { [ -x "$mac_code" ] && CODE=$mac_code; }
  fi
  # the package the repository keeps (the extension's CI checks it matches the source); the extension then updates
  # itself from this copy (PLN0218)
  vsix="$DEST/extensions/vscode-team-terminals/dist/team-terminals.vsix"
  [ -f "$vsix" ] || vsix=
  if [ -z "$vsix" ]; then warn "a copia em $DEST nao tem extensions/vscode-team-terminals/dist/team-terminals.vsix"
  elif ! command -v "$CODE" >/dev/null 2>&1; then
    say "VS Code: comando code nao encontrado; depois de instalar: code --install-extension \"$vsix\""
  elif "$CODE" --install-extension "$vsix" --force >/dev/null 2>&1; then
    say "VS Code: extensao Team Terminals instalada ($(basename "$vsix"))"
  else
    warn "VS Code: a instalacao da extensao falhou; tente: code --install-extension \"$vsix\" --force"
  fi
fi

# ------------------------------------------------------------------------------------------------ 3. the service
umask 077
mkdir -p "$PROV_DIR/secrets" "$PROV_DIR/data"
chmod 700 "$PROV_DIR" "$PROV_DIR/secrets" "$PROV_DIR/data"
if [ ! -f "$PROV_DIR/config.json" ]; then
  printf '{\n  "base_url": "%s",\n  "interval_s": 30,\n  "auto_update": true\n}\n' "$BASE_URL" > "$PROV_DIR/config.json"
fi
umask 022

MODE=$SERVICE
if [ "$MODE" = auto ]; then
  if [ "$OS" = macos ]; then MODE=launchd
  elif "$SYSTEMCTL" --user show-environment >/dev/null 2>&1; then MODE=systemd
  else MODE=manual; fi
fi
case "$MODE" in
  systemd)
    # a unit whose ExecStart runs another copy of the plugin (a clone of development) keeps that ExecStart
    svc_script=   # %h for the home folder; a path systemd would split or expand keeps the skill's (and its warning)
    if [ -n "$SVC_WHY" ]; then
      case "$OURS" in
        *[[:space:]%\"\'\\\&]*) ;;   # & too: bash 5.2 expands it in install-provisioner.sh's replacement
        "$HOME"/*) svc_script="%h/${OURS#"$HOME"/}/scripts/provisioner.py";;
        *) svc_script="$OURS/scripts/provisioner.py";;
      esac
    fi
    out=$(SYSTEMCTL=$SYSTEMCTL PROVISIONER_SCRIPT=$svc_script bash "$OURS/provisioner/install-provisioner.sh") \
      || die "nao consegui gravar a unit do systemd"
    case "$out" in
      *"roda a copia instalada"*) say "servico: unit agent-provisioner gravada pela copia instalada ($VERSION, $OURS): a skill agent em $ACTIVE $SVC_WHY. $SVC_BACK, ou: PROVISIONER_SCRIPT= bash \"$OURS/provisioner/install-provisioner.sh\" && systemctl --user daemon-reload && systemctl --user restart agent-provisioner.service";;
      *"ExecStart mantido"*) say "servico: unit agent-provisioner gravada; ExecStart mantido (roda outra copia do plugin)";;
      *) say "servico: unit agent-provisioner gravada (systemd --user)";;
    esac
    # the copy the service runs (a kept ExecStart, else the skill link): older than the installed one, say so (PLN0326)
    unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agent-provisioner.service"
    svc_py=
    for t in $(sed -n 's/^ExecStart=//p' "$unit" 2>/dev/null | head -n 1); do
      case "$t" in *provisioner.py) svc_py=$(printf '%s\n' "$t" | sed "s|%h|$HOME|g"); break;; esac
    done
    if [ -n "$svc_py" ] && [ -f "$svc_py" ]; then
      svc_ver=$(copy_version "$svc_py")
      if older "$svc_ver" "$VERSION"; then
        svc_dir=$(cd "$(dirname "$svc_py")" && pwd -P)
        warn "ATENCAO: o servico agent-provisioner roda $svc_py, da versao $svc_ver, mais antiga que a $VERSION recem instalada; enquanto isso, o servico e este instalador rodam versoes diferentes"
        stale_hint "$svc_dir"
        warn "  depois: systemctl --user restart agent-provisioner.service"
      else
        say "servico: roda a versao $svc_ver ($svc_py)"
      fi
    fi;;
  launchd)
    mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
    xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
    h=$(xml "$HOME")
    svc_py="$h/.claude/skills/agent/scripts/provisioner.py"
    [ -z "$SVC_WHY" ] || svc_py=$(xml "$OURS/scripts/provisioner.py")
    cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- $MARK: local provisioner of the agent plugin (see docs/provisioner.md). Rewritten by install.sh. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$svc_py</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$h/.local/share/planou/python/bin:$h/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$h/Library/Logs/agent-provisioner.log</string>
  <key>StandardErrorPath</key><string>$h/Library/Logs/agent-provisioner.log</string>
</dict>
</plist>
EOF
    chmod 644 "$PLIST.tmp"; mv "$PLIST.tmp" "$PLIST"
    say "servico: $PLIST gravado (launchd)"
    [ -z "$SVC_WHY" ] || say "servico: roda a copia instalada ($VERSION, $OURS): a skill agent em $ACTIVE $SVC_WHY. $SVC_BACK";;
  wsl-task) say "servico: fica com a tarefa agendada do Windows (install.ps1)";;
  none) say "servico: pulado (--service none)";;
  manual)
    if [ "$OS" = wsl ]; then
      warn "o WSL esta sem systemd: enquanto a janela do funcionario (planou-agent) estiver aberta, ela faz o papel do servico; para o servico, ligue em /etc/wsl.conf ([boot] systemd=true)"
    else
      warn "systemctl --user nao responde: enquanto a janela do funcionario (planou-agent) estiver aberta, ela faz o papel do servico; ou rode: $PY \"$SVC_DIR/scripts/provisioner.py\" run"
    fi;;
esac

# ------------------------------------------------------------------------------------------------ 4. pairing
paired() { grep -q '^PLANOU_PROVISIONER_KEY=.' "$CRED" 2>/dev/null; }
PAIR_CMD="$PY $OURS/scripts/provisioner.py pair --base-url $BASE_URL"

send_code() {   # the code only on stdin: printf is a builtin, so it never shows up in a process list
  printf '%s\n' "$1" | "$PY" "$OURS/scripts/provisioner.py" pair --base-url "$BASE_URL"
}

pair_status=0
if paired; then
  say "conexao: este computador ja esta conectado ao Planou (trocar: $PAIR_CMD)"
elif [ "$PAIR" != 1 ]; then
  say "conexao: pulada. Para conectar: $PAIR_CMD"
elif [ "$CODE_STDIN" = 1 ]; then
  code=; IFS= read -r code || true
  if [ -z "$code" ]; then say "conexao: nenhum codigo na entrada. Para conectar: $PAIR_CMD"
  else send_code "$code" || pair_status=3; fi
  code=
else
  TTY=${PLANOU_INSTALL_TTY:-/dev/tty}
  if ! (exec <"$TTY") 2>/dev/null; then   # a subshell: a failed redirection on a builtin would end the script
    say "conexao: sem terminal para perguntar o codigo. Para conectar: $PAIR_CMD"
  else
    say ""
    say "Conecte este computador: no Planou, Configuracoes > Computadores > Conectar este computador."
    tries=0
    while :; do
      tries=$((tries + 1))
      printf 'Codigo de pareamento (Enter pula): ' >"$TTY"
      old=$(stty -g <"$TTY" 2>/dev/null) || old=
      trap 'stty echo <"$TTY" 2>/dev/null; exit 130' INT TERM
      stty -echo <"$TTY" 2>/dev/null || true
      code=; IFS= read -r code <"$TTY" || true
      if [ -n "$old" ]; then stty "$old" <"$TTY" 2>/dev/null || true; else stty echo <"$TTY" 2>/dev/null || true; fi
      trap - INT TERM
      printf '\n' >"$TTY"
      if [ -z "$code" ]; then say "conexao: pulada. Para conectar depois: $PAIR_CMD"; break; fi
      if send_code "$code"; then code=; break; fi
      code=
      [ "$tries" -lt 3 ] || { pair_status=3; break; }
    done
  fi
fi

# ------------------------------------------------------------------------------------------------ 5. turn it on
if paired; then
  case "$MODE" in
    systemd)
      "$SYSTEMCTL" --user daemon-reload
      "$SYSTEMCTL" --user enable -q agent-provisioner.service
      "$SYSTEMCTL" --user restart agent-provisioner.service
      say "servico: ligado (journalctl --user -u agent-provisioner -f)";;
    launchd)
      uid=$(id -u)
      "$LAUNCHCTL" bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
      "$LAUNCHCTL" bootstrap "gui/$uid" "$PLIST"
      say "servico: ligado (log em ~/Library/Logs/agent-provisioner.log)";;
  esac
else
  case "$MODE" in systemd|launchd) say "servico: instalado; liga quando este computador estiver conectado (rode o instalador de novo)";; esac
fi

say "== pronto: plugin agent $VERSION"
exit "$pair_status"
