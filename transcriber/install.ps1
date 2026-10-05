# Field Notes Transcriber - Windows installer
# Installs a local transcription helper for the Field Notes app. Everything runs on this computer.
#
# Run in PowerShell (no admin needed):
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/martinmfranklin/meeting-recorder/main/transcriber/install.ps1 | iex"
#
# What it does:
#   1. Installs Python 3.12 for your user (via winget) if no suitable Python is found
#   2. Creates %LOCALAPPDATA%\FieldNotesTranscriber with its own Python environment
#   3. Installs faster-whisper and sherpa-onnx (plus NVIDIA GPU libraries if an NVIDIA card is present)
#   4. Downloads the speech and speaker models (about 1.8 GB, once)
#   5. Starts the transcriber now and at every sign-in (hidden; the model loads only when there is work)
#   6. Adds right-click > Send to > Transcribe (Field Notes) for any audio or video file

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Base = 'https://raw.githubusercontent.com/martinmfranklin/meeting-recorder/main/transcriber'
$Root = Join-Path $env:LOCALAPPDATA 'FieldNotesTranscriber'
$Venv = Join-Path $Root 'venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$PyW = Join-Path $Venv 'Scripts\pythonw.exe'

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }

# pip's Windows certificate check (truststore) crashes on some PCs with "access violation";
# if a pip call fails, retry it once with the older certificate handling.
function Pip([string[]]$PipArgs) {
    $ErrorActionPreference = 'Continue'   # pip writes warnings to stderr; do not treat them as fatal
    & $Py -m pip @PipArgs --disable-pip-version-check 2>&1 | Out-Host
    if ($LASTEXITCODE -eq 0) { return $true }
    Write-Host 'pip failed; retrying with legacy certificate handling...' -ForegroundColor Yellow
    & $Py -m pip @PipArgs --disable-pip-version-check --use-deprecated=legacy-certs 2>&1 | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    foreach ($v in '3.12', '3.11', '3.10') {
        try { $p = (& py "-$v" -c "import sys;print(sys.executable)" 2>$null); if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    try {
        $p = (& python -c "import sys;print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,12) else '')" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $p -and $p.Trim()) { return $p.Trim() }
    } catch {}
    return $null
}

New-Item -ItemType Directory -Force -Path $Root | Out-Null

Step 'Checking Python'
$Sys = Find-Python
if (-not $Sys) {
    Write-Host 'Python 3.10-3.12 not found. Installing Python 3.12 for your user with winget...'
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $Sys = Find-Python
    if (-not $Sys) {
        $cand = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $cand) { $Sys = $cand } else { throw 'Python install did not complete. Install Python 3.12 from python.org, then run this again.' }
    }
}
Write-Host "Using $Sys"

Step 'Stopping any running transcriber'
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*FieldNotesTranscriber*server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Step 'Creating environment'
if (-not (Test-Path $Py)) { & $Sys -m venv $Venv; if ($LASTEXITCODE) { throw 'Could not create the Python environment.' } }
Pip @('install', '--upgrade', 'pip', '--quiet') | Out-Null
if (-not (Pip @('install', '--upgrade', '--progress-bar', 'off', 'faster-whisper', 'sherpa-onnx', 'av<19'))) { throw 'Package install failed (see the pip messages above).' }

$HasNvidia = $false
try { if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi -L | Out-Null; $HasNvidia = ($LASTEXITCODE -eq 0) } } catch {}
if ($HasNvidia) {
    Step 'NVIDIA GPU found: installing GPU libraries (about 1 GB)'
    if (-not (Pip @('install', '--upgrade', '--progress-bar', 'off', 'nvidia-cublas-cu12', 'nvidia-cudnn-cu12==9.*'))) { Write-Warning 'GPU libraries failed to install; the transcriber will use the CPU.' }
} else {
    Write-Host 'No NVIDIA GPU found: transcription will run on the CPU.'
}

Step 'Downloading transcriber'
foreach ($f in 'engine.py', 'server.py', 'inbox.py', 'formats.py', 'transcribe.py', 'uninstall.ps1') {
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$f" -OutFile (Join-Path $Root $f)
}

Step 'Downloading models (about 1.8 GB the first time; this can take a while)'
Push-Location $Root
& $Py server.py --prefetch
$ok = ($LASTEXITCODE -eq 0)
Pop-Location
if (-not $ok) { throw "Model download or loading failed. See the messages above." }

Step 'Starting at sign-in'
$ws = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Startup')) 'Field Notes Transcriber.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Field Notes Transcriber.lnk')
)
foreach ($lnk in $targets) {
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $PyW
    $s.Arguments = "`"$(Join-Path $Root 'server.py')`""
    $s.WorkingDirectory = $Root
    $s.Description = 'Local transcription helper for Field Notes'
    $s.Save()
}
$st = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('SendTo')) 'Transcribe (Field Notes).lnk'))
$st.TargetPath = $Py
$st.Arguments = "`"$(Join-Path $Root 'transcribe.py')`" --pause"
$st.WorkingDirectory = $Root
$st.Description = 'Transcribe audio or video with speaker labels (on this computer)'
$st.Save()

Start-Process -FilePath $PyW -ArgumentList "`"$(Join-Path $Root 'server.py')`"" -WorkingDirectory $Root -WindowStyle Hidden

Start-Sleep -Seconds 8
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 5
    Write-Host "`nField Notes Transcriber is running: $($h.status) on $($h.device) ($($h.model))." -ForegroundColor Green
} catch {
    Write-Host "`nInstalled. The transcriber is still starting; check http://127.0.0.1:8787 in a minute." -ForegroundColor Yellow
}
Write-Host 'Open Field Notes in Chrome or Edge, open a recording''s Details and click Transcribe.'
Write-Host 'Or right-click any audio or video file > Send to > Transcribe (Field Notes).'
Write-Host 'If the browser asks to allow access to apps on this device, choose Allow.'
$od = @($env:OneDriveCommercial, $env:OneDrive) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($od) { Write-Host "OneDrive inbox: $(Join-Path $od 'Field Notes Inbox') (recordings saved there are transcribed automatically)." }
