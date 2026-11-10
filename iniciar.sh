#!/usr/bin/env bash
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "[!] Falta FFmpeg (instálalo con tu gestor de paquetes)"; exit 1; }
if [ ! -d .venv ]; then
  echo "Primera vez: creando entorno e instalando dependencias..."
  python3 -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt
else
  . .venv/bin/activate
fi
exec streamlit run app.py --server.port 8501 --server.headless false
