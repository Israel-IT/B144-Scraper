# B144 scraper launcher for Windows (run.bat calls this; the .bat gets past PowerShell's script policy).
#   run.bat           start the web UI (restarts it if it's already running) and open the browser
#   run.bat stop      stop the web UI
#   run.bat status    show whether it's running
param([string]$Command = "start")

# Native tools (uv, taskkill) write progress to stderr; check their exit codes instead of failing on it.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

$Port = 8501
$Url = "http://localhost:$Port"
$PidFile = "data\app.pid"
$OutLog = "data\app.log"
$ErrLog = "data\app.err.log"
$Python = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path data | Out-Null
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
$env:PYTHONUTF8 = "1"

function Get-AppPid {
  if (Test-Path $PidFile) { (Get-Content $PidFile -Raw).Trim() }
}

function Test-Alive($id) {
  [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

function Get-PortPids {
  @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-Tree($id) {
  # The venv's python.exe starts the real interpreter as a child, so kill the whole tree.
  if (Test-Alive $id) { & taskkill.exe /PID $id /T /F 2>&1 | Out-Null }
}

function Stop-App {
  $id = Get-AppPid
  if (Test-Alive $id) {
    Write-Host "Stopping B144 scraper (PID $id)..."
    Stop-Tree $id
  }
  Remove-Item $PidFile -ErrorAction SilentlyContinue
  # Free the port if something from this app is still holding it.
  foreach ($p in Get-PortPids) {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue).CommandLine
    if ($cmd -match "streamlit") {
      Write-Host "Freeing port $Port (PID $p)..."
      Stop-Tree $p
    } else {
      Write-Warning "Port $Port is used by another program (PID $p). Not touching it."
    }
  }
}

function Show-Status {
  $id = Get-AppPid
  if (Test-Alive $id) {
    Write-Host "running  PID $id  $Url"
  } else {
    Write-Host "stopped"
    $held = Get-PortPids
    if ($held.Count) { Write-Host "note: port $Port is held by PID(s) $($held -join ' ')" }
  }
}

function Install-Uv {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (Python package manager)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv did not install; see the messages above" }
  }
}

function Start-App {
  Install-Uv
  # A .venv copied over from a Mac has no Scripts\python.exe; let uv build a Windows one.
  if ((Test-Path .venv) -and -not (Test-Path $Python)) {
    Write-Host "Removing a .venv from another operating system..."
    Remove-Item -Recurse -Force .venv
  }
  Write-Host "Syncing dependencies (uv sync; the first run downloads Python and takes a few minutes)..."
  & uv sync --quiet
  if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
  Stop-App
  Write-Host "Starting B144 scraper on $Url (log: $ErrLog)..."
  $proc = Start-Process -FilePath $Python -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
    -ArgumentList "-m", "streamlit", "run", "app.py", "--server.port", "$Port", "--server.address", "localhost",
                  "--server.headless", "true", "--browser.gatherUsageStats", "false"
  Set-Content -Path $PidFile -Value $proc.Id
  for ($i = 0; $i -lt 120; $i++) {
    try {
      Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$Url/_stcore/health" | Out-Null
      Write-Host "Ready: $Url  (PID $($proc.Id))"
      Start-Process $Url
      return
    } catch {}
    if ($proc.HasExited) {
      Write-Host "The app exited during startup. Last log lines:"
      Get-Content $ErrLog -Tail 30 -ErrorAction SilentlyContinue
      exit 1
    }
    Start-Sleep -Milliseconds 500
  }
  throw "The app didn't answer on $Url within 60 s; see $ErrLog"
}

try {
  switch ($Command) {
    { $_ -in "start", "restart" } { Start-App }
    "stop" { Stop-App; Write-Host "Stopped." }
    "status" { Show-Status }
    default { Write-Host "usage: run.bat [start|stop|status]"; exit 2 }
  }
} catch {
  Write-Host "Error: $_" -ForegroundColor Red
  exit 1
}
