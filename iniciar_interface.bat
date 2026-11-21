@echo off
rem Abra com dois cliques no Windows.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente Python nao encontrado. Execute uv sync --extra desktop nesta pasta primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m spotdl.gui %*
if errorlevel 1 pause
