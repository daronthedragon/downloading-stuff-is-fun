@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo first run: setting up...
  py -m venv .venv || goto :fail
  .venv\Scripts\python.exe -m pip install -q --upgrade pip -r requirements.txt || goto :fail
)
rem PO token helper - YouTube needs it more and more. Skipped if not built.
if exist pot-provider\build\main.js (
  tasklist /fi "windowtitle eq ineedit-pot" 2>nul | find /i "node.exe" >nul || (
    start "ineedit-pot" /min cmd /c "cd pot-provider && node build/main.js"
  )
)
start "" http://127.0.0.1:7788
.venv\Scripts\python.exe server.py
goto :eof
:fail
echo setup failed - is python installed?
pause
