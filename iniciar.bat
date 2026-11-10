@echo off
setlocal
cd /d "%~dp0"
where ffmpeg >nul 2>nul || (echo [!] Falta FFmpeg. Instalalo con:  winget install ffmpeg & pause & exit /b 1)
if not exist ".venv\Scripts\activate.bat" (
  echo Primera vez: creando entorno e instalando dependencias...
  python -m venv .venv || (echo [!] Falta Python 3.10 o superior & pause & exit /b 1)
  call ".venv\Scripts\activate.bat"
  python -m pip install -q -r requirements.txt
) else (
  call ".venv\Scripts\activate.bat"
)
streamlit run app.py --server.port 8501 --server.headless false
