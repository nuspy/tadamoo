@echo off
rem tadamoo-agent: the Basalt agent from any folder (python -m tadamoo with this folder's Python and packages).
rem   tadamoo-agent -p "fix the failing test" --workspace D:\work\proj --json
rem   tadamoo-agent acp --workspace D:\work\proj        the same agent for an editor that speaks ACP over stdio
rem   tadamoo-agent --help
setlocal
set "TADAMOO_ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%TADAMOO_ROOT%;%PYTHONPATH%"
if exist "%TADAMOO_ROOT%.venv\Scripts\python.exe" (
    "%TADAMOO_ROOT%.venv\Scripts\python.exe" -m tadamoo %*
) else (
    python -m tadamoo %*
)
exit /b %ERRORLEVEL%
