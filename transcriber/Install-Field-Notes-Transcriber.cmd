@echo off
title Field Notes Transcriber - installer
echo Installing Field Notes Transcriber. This downloads about 1.8 GB the first time and can take 10-20 minutes.
echo Progress is also written to %USERPROFILE%\Downloads\FieldNotes-install-log.txt
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Transcript -Path \"$env:USERPROFILE\Downloads\FieldNotes-install-log.txt\" -Force | Out-Null; try { irm https://raw.githubusercontent.com/martinmfranklin/meeting-recorder/main/transcriber/install.ps1 | iex; Write-Host \"INSTALL FINISHED\" } catch { Write-Host (\"INSTALL FAILED: \" + $_) -ForegroundColor Red }; Stop-Transcript | Out-Null"
echo.
pause
