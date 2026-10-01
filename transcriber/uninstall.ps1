# Field Notes Transcriber - uninstall
#   powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\FieldNotesTranscriber\uninstall.ps1"
$Root = Join-Path $env:LOCALAPPDATA 'FieldNotesTranscriber'
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*FieldNotesTranscriber*server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
foreach ($d in [Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Programs')) {
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $d 'Field Notes Transcriber.lnk')
}
$ans = Read-Host "Remove $Root (program, models and finished-job cache)? [y/N]"
if ($ans -match '^[yY]') {
    Set-Location $env:TEMP
    Remove-Item -Recurse -Force $Root
    Write-Host 'Removed.'
} else {
    Write-Host "Stopped and removed from sign-in. Files left in $Root."
}
