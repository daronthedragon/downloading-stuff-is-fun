@echo off
rem Optional YouTube helper (PO token provider). Needs Node. Downloads from upstream.
setlocal
cd /d "%~dp0"
set VER=1.3.1
echo Fetching bgutil-ytdlp-pot-provider %VER%...
curl -sL -o pot.tar.gz "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/%VER%.tar.gz" || goto :fail
tar xzf pot.tar.gz || goto :fail
if exist pot-provider rmdir /s /q pot-provider
move "bgutil-ytdlp-pot-provider-%VER%\server" pot-provider >nul
rmdir /s /q "bgutil-ytdlp-pot-provider-%VER%"
del pot.tar.gz
cd pot-provider
call npm install --no-audit --no-fund || goto :fail
call npx tsc || goto :fail
cd ..
.venv\Scripts\python.exe -m pip install -q bgutil-ytdlp-pot-provider
echo.
echo Done. Restart start.bat.
pause
exit /b
:fail
echo Failed - is Node installed? (https://nodejs.org)
pause
