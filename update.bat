@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install -q --upgrade yt-dlp
.venv\Scripts\python.exe -c "import yt_dlp;print('yt-dlp',yt_dlp.version.__version__)"
pause
