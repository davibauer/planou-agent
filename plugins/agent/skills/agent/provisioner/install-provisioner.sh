#!/usr/bin/env bash
# Installs the local provisioner (PLN0111) as a systemd user service. Writes the unit and the folders; turns the
# service on only with --enable. Never reads nor prints the credential: it checks only that the file exists and is 0600.
#
#   install-provisioner.sh [--enable]
#   install-provisioner.sh --uninstall
#
#   --enable  systemctl --user daemon-reload + enable --now (without it, the commands are printed)
#
# The unit runs ~/.claude/skills/agent/scripts/provisioner.py (the plugin copy in use), never a snapshot of its own: new
# versions arrive with the plugin (see the unit and docs/provisioner.md). Exception, PROVISIONER_SCRIPT=<path> (install.sh,
# PLN0336: the skill's copy is older or has no provisioner): the unit runs that provisioner.py, the installed copy's,
# until a run without it; %h stands for the home folder.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
unit_src="$here/agent-provisioner.service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/agent-provisioner.service"
prov="$HOME/.config/agent-provisioner"
cred="$prov/secrets/planou.env"
systemctl=${SYSTEMCTL:-systemctl}
enable=0 uninstall=0
script="$HOME/.claude/skills/agent/scripts/provisioner.py"

while [ $# -gt 0 ]; do
  case "$1" in
    --enable) enable=1; shift;;
    --uninstall) uninstall=1; shift;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "opcao desconhecida: $1" >&2; exit 2;;
  esac
done

if [ "$uninstall" = 1 ]; then
  if [ -f "$unit" ]; then
    "$systemctl" --user disable --now agent-provisioner.service || true
    rm -f "$unit"; "$systemctl" --user daemon-reload || true
    echo "servico removido ($unit). A pasta $prov fica (credencial, estado e instancias arquivadas)."
  else
    echo "servico nao instalado ($unit nao existe)"
  fi
  exit 0
fi

[ -f "$script" ] || echo "aviso: $script nao existe; a unit roda a copia do plugin em uso (instale o plugin agent e o link da skill)"

umask 077
mkdir -p "$prov/secrets" "$prov/data" "$unit_dir"
chmod 700 "$prov" "$prov/secrets" "$prov/data"
if [ ! -f "$prov/config.json" ]; then
  printf '{\n  "base_url": "http://127.0.0.1:5068/v1",\n  "interval_s": 30\n}\n' > "$prov/config.json"
  echo "config criado: $prov/config.json (base_url da producao local; em desenvolvimento, a porta da API)"
fi
# an ExecStart that runs another copy of the plugin that still exists (a clone of development, say) stays: the unit
# follows the template but never swaps the copy someone chose (the same rule as maintain_unit in provisioner.py). This
# copy's own provisioner.py (written by PROVISIONER_SCRIPT) is no choice: it is decided again on every run
own=$(cd "$here/../scripts" && pwd -P)/provisioner.py
alt=${PROVISIONER_SCRIPT:-}
keep=
if [ -f "$unit" ]; then
  have=$(sed -n 's/^ExecStart=//p' "$unit" | head -n 1)
  want=$(sed -n 's/^ExecStart=//p' "$unit_src" | head -n 1)
  if [ -n "$have" ] && [ "$have" != "$want" ]; then
    py=
    for t in $have; do case "$t" in *provisioner.py) py=$t; break;; esac; done
    py=${py//%h/$HOME}
    real=$(cd "$(dirname "$py")" 2>/dev/null && pwd -P)/provisioner.py || real=
    if [ -n "$py" ] && [ -f "$py" ] && [ "$real" != "$own" ]; then keep=$have; fi
  fi
fi
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ExecStart=*) if [ -n "$keep" ]; then printf 'ExecStart=%s\n' "$keep"
                 elif [ -n "$alt" ]; then printf '%s\n' "${line/\%h\/.claude\/skills\/agent\/scripts\/provisioner.py/$alt}"
                 else printf '%s\n' "$line"; fi;;
    *) printf '%s\n' "$line";;
  esac
done < "$unit_src" > "$unit.tmp"
chmod 644 "$unit.tmp" && mv "$unit.tmp" "$unit"
if [ -n "$keep" ]; then echo "unit gravada: $unit (ExecStart mantido, outra copia do plugin: $keep)"
elif [ -n "$alt" ]; then echo "unit gravada: $unit (roda a copia instalada: $alt)"
else echo "unit gravada: $unit (roda $script)"; fi

cred_ok=0
if [ -f "$cred" ]; then
  mode=$(stat -c %a "$cred" 2>/dev/null || stat -f %Lp "$cred")   # GNU, else BSD (macOS)
  if [ "$mode" = 600 ]; then cred_ok=1; echo "credencial: ok ($cred, 0600)"
  else echo "credencial: $cred esta $mode; rode: chmod 600 $cred"; fi
else
  echo "credencial: falta. No repositorio do Planou: ./scripts/connect-provisioner.sh <seu e-mail> $cred"
fi

if [ "$enable" = 1 ]; then
  [ "$cred_ok" = 1 ] || { echo "nao ligo sem a credencial em 0600" >&2; exit 1; }
  "$systemctl" --user daemon-reload
  "$systemctl" --user enable --now agent-provisioner.service
  echo "servico ligado: journalctl --user -u agent-provisioner -f"
else
  echo "para ligar: systemctl --user daemon-reload && systemctl --user enable --now agent-provisioner.service"
  echo "conferir antes: python3 $script check"
fi
