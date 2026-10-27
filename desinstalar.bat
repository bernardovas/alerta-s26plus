@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'Alerta S26 Plus' -Confirm:$false -ErrorAction SilentlyContinue; Write-Host 'Agendamento removido. Voce pode apagar esta pasta se quiser.'"
pause
