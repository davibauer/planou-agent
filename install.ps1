# One-command installer of the agent plugin for Windows (PLN0192), in PowerShell:
#
#   irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/davibauer/planou-agent/main/install.ps1))) -BaseUrl URL
#
# The agents, the `team` launcher and the provisioner are bash and Python for Linux, so on Windows they live in WSL:
#   1. install.sh runs inside the WSL distribution (the agent plugin, the Team Terminals extension through the `code`
#      of WSL, which lands in the VS Code of the WSL windows, and, last, the hidden pairing code prompt);
#   2. a scheduled task of this user, at logon, runs the provisioner inside that distribution
#      (provisioner/wsl-provisioner.sh of the plugin), which also keeps WSL up. With the systemd unit on in WSL, the unit
#      runs it and the task only keeps WSL up.
# Running it again updates everything. No account is created and no secret is shown: the code goes to the provisioner
# on stdin, the credential from Planou straight to a 0600 file inside WSL.
# WSL with a distribution is a prerequisite (wsl --install, then open the distribution once to create the Linux user);
# this installer never installs one, since that needs an administrator, a reboot and a new Linux user.
#
# Parameters (each one also as an environment variable): -BaseUrl (PLANOU_BASE_URL), -Distro (PLANOU_WSL_DISTRO, the
# default distribution when empty), -Ref (PLANOU_INSTALL_REF), -TaskName (PLANOU_TASK_NAME), -NoPair, -NoVSCode,
# -NoTask. Tests: -InstallSh (a local install.sh instead of the download), -InstallArgs (more arguments to install.sh),
# PLANOU_WSL_EXE (another wsl.exe). A pairing code piped into the script goes to install.sh on stdin.
[CmdletBinding()]
param(
  [string]$BaseUrl = $(if ($env:PLANOU_BASE_URL) { $env:PLANOU_BASE_URL } else { 'https://app.planou.com' }),
  [string]$Distro = $env:PLANOU_WSL_DISTRO,
  [string]$Ref = $(if ($env:PLANOU_INSTALL_REF) { $env:PLANOU_INSTALL_REF } else { 'main' }),
  [string]$TaskName = $(if ($env:PLANOU_TASK_NAME) { $env:PLANOU_TASK_NAME } else { 'Planou agent-provisioner' }),
  [string]$InstallSh = $env:PLANOU_INSTALL_SH,
  [string[]]$InstallArgs = @(),
  [switch]$NoPair,
  [switch]$NoVSCode,
  [switch]$NoTask
)
# Continue, not Stop: Windows PowerShell 5.1 turns any stderr line of a native command into a terminating error under
# Stop. Native exit codes are checked by hand; the cmdlets that matter get -ErrorAction Stop.
$ErrorActionPreference = 'Continue'
$piped = @($input)                       # a pairing code piped in (tests); never a parameter, it would show up in argv
$Wsl = if ($env:PLANOU_WSL_EXE) { $env:PLANOU_WSL_EXE } else { 'wsl.exe' }
$Url = "https://raw.githubusercontent.com/davibauer/planou-agent/$Ref/install.sh"

function Say($text) { Write-Host $text }
# throw, never exit: under `irm | iex` an exit would close the person's PowerShell window
function Fail($text) { throw "erro: $text" }

Say '== Planou: instalando o plugin agent (Windows, pelo WSL)'
if (-not (Get-Command $Wsl -ErrorAction SilentlyContinue)) {
  Fail 'o WSL nao esta instalado. Rode "wsl --install" num PowerShell de administrador, reinicie, abra o Ubuntu uma vez para criar o seu usuario e rode este comando de novo.'
}

# wsl.exe answers in UTF-16 unless WSL_UTF8 is set; the NUL bytes are dropped either way
$env:WSL_UTF8 = '1'
function Wsl-Text([string[]]$wslArgs) {
  $out = & $Wsl @wslArgs 2>$null
  if ($LASTEXITCODE -ne 0) { return $null }
  return ((@($out) -join "`n") -replace "`0", '').Trim()
}
if (-not $Distro) { $Distro = Wsl-Text @('-e', 'sh', '-c', 'printf %s $WSL_DISTRO_NAME') }
if (-not $Distro) {
  Fail 'nenhuma distribuicao do WSL. Rode "wsl --install -d Ubuntu", abra o Ubuntu uma vez para criar o seu usuario e rode este comando de novo.'
}
$names = @((Wsl-Text @('-l', '-q')) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($names.Count -gt 0 -and $names -notcontains $Distro) { Fail "a distribuicao $Distro nao existe no WSL ($($names -join ', '))" }
Say "WSL: $Distro"

# 1. install.sh inside WSL
$shArgs = @('--base-url', $BaseUrl, '--ref', $Ref, '--service', 'wsl-task')
# VS Code stays the Windows way of seeing the employees (install.sh leaves it off by default since PLN0297)
if ($NoVSCode) { $shArgs += '--no-vscode' } else { $shArgs += '--vscode' }
if ($NoPair) { $shArgs += '--no-pair' }
if ($piped.Count -gt 0) { $shArgs += '--code-stdin' }
$shArgs += $InstallArgs
if ($InstallSh) {
  $shPath = Wsl-Text @('-d', $Distro, '-e', 'wslpath', '-a', ($InstallSh -replace '\\', '/'))
  if (-not $shPath) { Fail "nao achei $InstallSh dentro do WSL" }
  $cmd = @('-d', $Distro, '-e', 'sh', $shPath) + $shArgs
} else {
  # downloaded here and run through its Windows path: Windows PowerShell 5.1 drops the inner double quotes of a native
  # argument, so a `sh -c '... "$0" ...'` would not reach WSL as written
  $InstallSh = Join-Path $env:TEMP 'planou-install.sh'
  try { Invoke-WebRequest -Uri $Url -OutFile $InstallSh -UseBasicParsing -ErrorAction Stop }
  catch { Fail "nao consegui baixar $Url ($($_.Exception.Message))" }
  $shPath = Wsl-Text @('-d', $Distro, '-e', 'wslpath', '-a', ($InstallSh -replace '\\', '/'))
  if (-not $shPath) { Fail "nao achei $InstallSh dentro do WSL" }
  $cmd = @('-d', $Distro, '-e', 'sh', $shPath) + $shArgs
}
if ($piped.Count -gt 0) { $piped[0] | & $Wsl @cmd } else { & $Wsl @cmd }
$installStatus = $LASTEXITCODE
if ($installStatus -ne 0 -and $installStatus -ne 3) { Fail "install.sh saiu com $installStatus dentro do WSL (veja as mensagens acima)" }
# no double quotes in the WSL commands (5.1 would drop them); these paths have no spaces
$check = Wsl-Text @('-d', $Distro, '-e', 'sh', '-c', 'test -f $HOME/.local/share/planou/claude-plugins/plugins/agent/skills/agent/provisioner/wsl-provisioner.sh && echo ok')
if ($check -ne 'ok') { Fail 'o plugin nao ficou instalado dentro do WSL (veja as mensagens acima)' }

# 2. the scheduled task (this user, at logon, no administrator)
if (-not $NoTask) {
  # the task hands this string to wsl.exe as is (Windows argv rules: no single quotes, no shell), so no quoting at all
  $wslArgs = "-d $Distro --cd ~ --exec sh .local/share/planou/claude-plugins/plugins/agent/skills/agent/provisioner/wsl-provisioner.sh"
  $conhost = Join-Path $env:WINDIR 'System32\conhost.exe'
  # conhost --headless runs wsl.exe without a console window on the desktop for the whole session
  if (Test-Path $conhost) { $exe = $conhost; $arg = "--headless wsl.exe $wslArgs" } else { $exe = 'wsl.exe'; $arg = $wslArgs }
  $user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
  $action = New-ScheduledTaskAction -Execute $exe -Argument $arg
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Provisionador local do Planou (plugin agent) dentro do WSL. Instalado por install.ps1.' -Force `
    -ErrorAction Stop | Out-Null
  Say "servico: tarefa agendada `"$TaskName`" (ao entrar no Windows)"
  $paired = Wsl-Text @('-d', $Distro, '-e', 'sh', '-c', 'grep -q ^PLANOU_PROVISIONER_KEY=. $HOME/.config/agent-provisioner/secrets/planou.env && echo ok')
  if ($paired -eq 'ok') {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    Say 'servico: ligado'
  } else {
    Say 'servico: liga quando este computador estiver conectado (rode o instalador de novo)'
  }
}
if ($installStatus -eq 3) { Fail 'a conexao com o Planou falhou (veja acima); rode de novo com um codigo novo' }
Say '== pronto'
