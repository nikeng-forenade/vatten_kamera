@echo off
rem Kor projektets Python utan att PowerShell-citat staller till det.
rem Exempel:
rem   run.cmd -m pytest tests -q
rem   run.cmd main.py --version
rem   run.cmd main.py snapshot
cd /d "%~dp0"
".venv\Scripts\python.exe" %*
