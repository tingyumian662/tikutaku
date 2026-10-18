@echo off
cd /d "%~dp0"
rem Scoring shells out to WSL; a project-tree .venv runs with Low integrity
rem and breaks. Always prefer the D-drive venv (hard rule).
if exist "D:\tikutaku\venv\Scripts\python.exe" (
  "D:\tikutaku\venv\Scripts\python.exe" webapp\server.py
) else (
  echo [WARN] D:\tikutaku\venv not found, falling back to uv run
  uv run python webapp\server.py
)
pause
