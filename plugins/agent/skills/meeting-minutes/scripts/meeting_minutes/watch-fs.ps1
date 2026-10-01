# Monitora a pasta de gravacoes pelo lado do WINDOWS e dispara o pipeline no WSL
# assim que o OBS termina de escrever o arquivo.
#
# POR QUE do lado do Windows: o inotify do Linux NAO funciona no disco do Windows montado no WSL -- o Windows
# nao propaga eventos de arquivo para o WSL. Entao quem escuta tem de ser o Windows.
#
# Este script e so o GATILHO. Quem decide se o video esta pronto continua sendo o
# watch.sh (moov atom + tamanho estavel). O cron de 5 min segue ativo como rede de
# seguranca: se este processo morrer, nada se perde -- so volta a demorar 5 min.
#
# ATENCAO: arquivo em ASCII puro de proposito. O PowerShell 5.1 le .ps1 como ANSI
# quando nao ha BOM, e acento/travessao viram lixo e quebram o parser.

param(
  [string]$Dir    = "$env:USERPROFILE\Videos",
  [string]$Distro = "Ubuntu-22.04",
  [Parameter(Mandatory=$true)][string]$Cmd,   # caminho do watch.sh no WSL (ex.: ~/.claude/skills/meeting-minutes/scripts/meeting_minutes/watch.sh, expandido)
  [int]$Quiet     = 45,     # segundos sem escrita ate considerar que o OBS parou
  [string]$Log    = "$env:LOCALAPPDATA\ata-reuniao\watch-fs.log"
)

function Write-Log($msg) {
  "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -Append -FilePath $Log -Encoding utf8
}

# Instancia unica: o VBS do Startup roda a cada logon, e sem isso os monitores
# acumulam -- cada um disparando o watch.sh de novo a cada gravacao.
$mutex = New-Object System.Threading.Mutex($false, "ata-reuniao-watch-fs")
if (-not $mutex.WaitOne(0)) {
  Write-Log "ja existe um monitor rodando - saindo"
  exit 0
}

$fsw = New-Object System.IO.FileSystemWatcher
$fsw.Path   = $Dir
$fsw.Filter = "*.mp4"
$fsw.IncludeSubdirectories = $false
$fsw.NotifyFilter = [IO.NotifyFilters]::LastWrite -bor [IO.NotifyFilters]::FileName -bor [IO.NotifyFilters]::Size
$fsw.EnableRaisingEvents = $true

Write-Log "iniciado - monitorando $Dir (quiet=$Quiet s, distro=$Distro)"

$pendente = $false
$ultimo   = Get-Date

while ($true) {
  # bloqueia ate 5s esperando qualquer mudanca em *.mp4
  $r = $fsw.WaitForChanged([System.IO.WatcherChangeTypes]::All, 5000)

  if (-not $r.TimedOut) {
    # o OBS escreve o tempo todo durante a gravacao: so marco que "algo mexeu" e
    # reinicio o relogio. O disparo real acontece quando o arquivo ESFRIA.
    if (-not $pendente) { Write-Log "mudanca detectada: $($r.Name)" }
    $pendente = $true
    $ultimo   = Get-Date
    continue
  }

  if ($pendente -and ((Get-Date) - $ultimo).TotalSeconds -ge $Quiet) {
    $pendente = $false
    Write-Log "arquivo esfriou ($Quiet s sem escrita) - acionando o WSL"
    try {
      & wsl.exe -d $Distro -e /bin/bash $Cmd --now 2>&1 | ForEach-Object { Write-Log "  wsl: $_" }
      Write-Log "wsl retornou $LASTEXITCODE"
    } catch {
      Write-Log "ERRO ao acionar o WSL: $_"
    }
  }
}
