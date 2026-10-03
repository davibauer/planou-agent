#!/bin/sh
# One-command installer of the agent plugin for Linux, WSL and macOS (PLN0192). On Windows, install.ps1 runs this same
# script inside WSL.
#
#   curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/davibauer/planou-agent/main/install.sh | sh -s -- --base-url URL
#
# It installs, and a second run updates:
#   1. the agent plugin: a copy of davibauer/planou-agent (public: the agent as published by the CI of the plugins
#      repository; git, or the tarball without git) in ~/.local/share/planou/claude-plugins (the folder keeps its old
#      name: the Windows task and the extension point at it). A copy cloned from the old repository (claude-plugins) moves
#      to the public one on its own when it has no local changes,
#      the skills ~/.claude/skills/agent and ~/.claude/skills/work-watch pointing at it, the `team` launcher in
#      ~/.local/bin and ~/.config/team/agents.json (empty) when missing;
#   2. the Team Terminals extension of VS Code, from extensions/vscode-team-terminals/dist/team-terminals.vsix, with
#      `code --install-extension` (the extension then updates itself from the same copy);
#   3. the local provisioner as a user service: systemd --user on Linux and WSL, launchd on macOS;
#   4. last, it asks for the pairing code of Planou (Configuracoes > Computadores > Conectar este computador), hidden, and
#      turns the service on once this computer is connected. A computer already connected keeps its credential.
# No account is created and no secret is shown: the code goes to `provisioner.py pair` on stdin, the credential goes
# from Planou straight to a 0600 file. Existing links, launchers or copies that are not this installer's stay as they are.
#
# Options (each one also as an environment variable):
#   --base-url URL     Planou (PLANOU_BASE_URL, default https://app.planou.com)
#   --dir DIR          where the copy lives (PLANOU_INSTALL_DIR, default ~/.local/share/planou/claude-plugins)
#   --ref REF          branch or tag (PLANOU_INSTALL_REF, default main)
#   --repo URL         git repository (PLANOU_INSTALL_REPO)
#   --tarball URL      archive used without git (PLANOU_INSTALL_TARBALL)
#   --no-git           use the archive even with git (PLANOU_INSTALL_NO_GIT=1)
#   --service MODE     auto | none | wsl-task (PLANOU_INSTALL_SERVICE; wsl-task: the Windows task runs the provisioner)
#   --no-vscode        skip the extension (PLANOU_INSTALL_VSCODE=0)
#   --no-pair          skip the pairing (PLANOU_INSTALL_PAIR=0)
#   --code-stdin       read the pairing code from stdin instead of the terminal (only when running a saved copy)
# Exit: 0 done, 1 an error (the message says which), 3 installed but the pairing failed (run again with a new code).
# Test hooks: PLANOU_INSTALL_OS (linux|wsl|macos), PLANOU_INSTALL_TTY, SYSTEMCTL, LAUNCHCTL, PLANOU_CODE_CLI.
# The text is ASCII on purpose: install.ps1 and old terminals pass it through without mangling accents.
set -eu

BASE_URL=${PLANOU_BASE_URL:-https://app.planou.com}
DEST=${PLANOU_INSTALL_DIR:-$HOME/.local/share/planou/claude-plugins}
REF=${PLANOU_INSTALL_REF:-main}
REPO=${PLANOU_INSTALL_REPO:-https://github.com/davibauer/planou-agent.git}
TARBALL=${PLANOU_INSTALL_TARBALL:-}
NO_GIT=${PLANOU_INSTALL_NO_GIT:-0}
SERVICE=${PLANOU_INSTALL_SERVICE:-auto}
VSCODE=${PLANOU_INSTALL_VSCODE:-1}
PAIR=${PLANOU_INSTALL_PAIR:-1}
CODE_STDIN=0

say() { printf '%s\n' "$*"; }
warn() { printf 'aviso: %s\n' "$*" >&2; }
die() { printf 'erro: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,32p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) [ $# -ge 2 ] || die "--base-url sem valor"; BASE_URL=$2; shift 2;;
    --base-url=*) BASE_URL=${1#*=}; shift;;
    --dir) [ $# -ge 2 ] || die "--dir sem valor"; DEST=$2; shift 2;;
    --ref) [ $# -ge 2 ] || die "--ref sem valor"; REF=$2; shift 2;;
    --repo) [ $# -ge 2 ] || die "--repo sem valor"; REPO=$2; shift 2;;
    --tarball) [ $# -ge 2 ] || die "--tarball sem valor"; TARBALL=$2; shift 2;;
    --no-git) NO_GIT=1; shift;;
    --service) [ $# -ge 2 ] || die "--service sem valor"; SERVICE=$2; shift 2;;
    --service=*) SERVICE=${1#*=}; shift;;
    --no-vscode) VSCODE=0; shift;;
    --no-pair) PAIR=0; shift;;
    --code-stdin) CODE_STDIN=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "opcao desconhecida: $1 (veja --help)";;
  esac
done
case "$SERVICE" in auto|none|wsl-task) ;; *) die "--service: use auto, none ou wsl-task";; esac
[ -n "$TARBALL" ] || TARBALL="https://codeload.github.com/davibauer/planou-agent/tar.gz/$REF"
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

# ------------------------------------------------------------------------------------------------ prerequisites
command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  2>/dev/null || die "precisa do python3 (3.8 ou mais novo)"
say "== Planou: instalando o plugin agent ($OS)"

# ------------------------------------------------------------------------------------------------ 1. the plugin copy
have_git() { [ "$NO_GIT" != 1 ] && git --version >/dev/null 2>&1; }

fetch_tarball() {
  tmp="$DEST.new.$$"
  rm -rf "$tmp"; mkdir -p "$tmp"
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$TARBALL" | tar -xzf - -C "$tmp" --strip-components=1
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$TARBALL" | tar -xzf - -C "$tmp" --strip-components=1
  else rm -rf "$tmp"; die "precisa do git, do curl ou do wget para baixar o plugin"; fi
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
elif [ ! -e "$DEST" ] && have_git; then
  git clone -q --branch "$REF" "$REPO" "$DEST" || die "nao consegui clonar $REPO"
  say "plugin: instalado em $DEST ($(git -C "$DEST" rev-parse --short HEAD))"
elif [ ! -e "$DEST" ] || [ -f "$DEST/.$MARK" ]; then
  fetch_tarball
  say "plugin: instalado em $DEST (arquivo, sem git)"
else
  die "$DEST existe e nao e uma copia deste instalador; apague ou use --dir"
fi
[ -f "$OURS/scripts/provisioner.py" ] || die "a copia em $DEST nao tem o plugin agent"
VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "$DEST/plugins/agent/.claude-plugin/plugin.json" 2>/dev/null || echo '?')

# skills: a link this installer made (or a broken one) follows the copy; anything else stays
link_skill() {
  name=$1 target=$2 link="$SKILLS/$1"
  mkdir -p "$SKILLS"
  if [ -L "$link" ] && [ -e "$link" ]; then
    cur=$(readlink "$link")
    if [ "$cur" = "$target" ]; then say "skill $name: ok ($target)"
    else say "skill $name: mantida (aponta para $cur, outra copia do plugin)"; fi
  elif [ -e "$link" ]; then
    say "skill $name: mantida ($link ja existe e nao e link)"
  else
    rm -f "$link"; ln -s "$target" "$link"; say "skill $name: $link -> $target"
  fi
}
link_skill agent "$OURS"
link_skill work-watch "$DEST/plugins/agent/skills/work-watch"
ACTIVE="$SKILLS/agent"

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
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN nao esta no PATH: acrescente no ~/.profile para usar o comando team";; esac

# the team list: the provisioner refuses to work without it; created empty (never overwritten)
if [ ! -e "$HOME/.config/team/agents.json" ]; then
  printf '[]' | python3 "$OURS/scripts/team_agents.py" migrate --seed - >/dev/null 2>&1 \
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
  printf '{\n  "base_url": "%s",\n  "interval_s": 30\n}\n' "$BASE_URL" > "$PROV_DIR/config.json"
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
    out=$(SYSTEMCTL=$SYSTEMCTL bash "$OURS/provisioner/install-provisioner.sh") \
      || die "nao consegui gravar a unit do systemd"
    case "$out" in
      *"ExecStart mantido"*) say "servico: unit agent-provisioner gravada; ExecStart mantido (roda outra copia do plugin)";;
      *) say "servico: unit agent-provisioner gravada (systemd --user)";;
    esac;;
  launchd)
    mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
    xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
    h=$(xml "$HOME")
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
    <string>$h/.claude/skills/agent/scripts/provisioner.py</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$h/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
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
    say "servico: $PLIST gravado (launchd)";;
  wsl-task) say "servico: fica com a tarefa agendada do Windows (install.ps1)";;
  none) say "servico: pulado (--service none)";;
  manual)
    if [ "$OS" = wsl ]; then
      warn "o WSL esta sem systemd: ligue em /etc/wsl.conf ([boot] systemd=true) e rode de novo, ou instale pelo install.ps1 no Windows"
    else
      warn "systemctl --user nao responde: rode o provisionador por conta propria: python3 $ACTIVE/scripts/provisioner.py run"
    fi;;
esac

# ------------------------------------------------------------------------------------------------ 4. pairing
paired() { grep -q '^PLANOU_PROVISIONER_KEY=.' "$CRED" 2>/dev/null; }
PAIR_CMD="python3 $OURS/scripts/provisioner.py pair --base-url $BASE_URL"

send_code() {   # the code only on stdin: printf is a builtin, so it never shows up in a process list
  printf '%s\n' "$1" | python3 "$OURS/scripts/provisioner.py" pair --base-url "$BASE_URL"
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
