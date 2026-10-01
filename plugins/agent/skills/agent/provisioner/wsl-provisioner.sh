#!/bin/sh
# Runs the local provisioner inside WSL for the Windows scheduled task that install.ps1 registers (PLN0192). The task
# starts it at logon through wsl.exe, which also keeps the WSL distribution up while the person is logged on.
#   - with the systemd unit on (a WSL with systemd, install-provisioner.sh), the unit runs the provisioner: only wait;
#   - otherwise it runs provisioner.py of the plugin copy in use and starts it again when it leaves (75 = a new version
#     to run, the same as Restart=always of the unit). Another provisioner already running makes it leave at once:
#     the pause keeps that from spinning.
SCRIPT="$HOME/.claude/skills/agent/scripts/provisioner.py"
while :; do
  if systemctl --user is-active --quiet agent-provisioner.service 2>/dev/null; then sleep 300; continue; fi
  if [ -f "$SCRIPT" ]; then python3 "$SCRIPT" run; else echo "provisionador: $SCRIPT nao existe" >&2; fi
  sleep 30
done
