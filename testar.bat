@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set TEST_MODE=1
if not exist ".venv\Scripts\python.exe" (
  echo Rode primeiro o instalar.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" monitor.py
echo.
echo Teste concluido. Confira o resumo no Telegram.
pause
