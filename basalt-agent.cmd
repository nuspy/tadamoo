@echo off
rem basalt-agent: the Basalt agent from any folder (python -m harness with this folder's Python and packages).
rem   basalt-agent -p "fix the failing test" --workspace D:\work\proj --json
rem   basalt-agent acp --workspace D:\work\proj        the same agent for an editor that speaks ACP over stdio
rem   basalt-agent --help
setlocal
set "BASALT_ROOT=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%BASALT_ROOT%;%PYTHONPATH%"
if exist "%BASALT_ROOT%.venv\Scripts\python.exe" (
    "%BASALT_ROOT%.venv\Scripts\python.exe" -m harness %*
) else (
    python -m harness %*
)
exit /b %ERRORLEVEL%
