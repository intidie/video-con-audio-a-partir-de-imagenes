"""Instalador .exe de Subtitulos Cartel: ejecuta el instalador .bat incluido (instala Python, FFmpeg, Node.js y
ExifTool si faltan, descarga la última versión de la app, crea el entorno y el acceso directo en el Escritorio).
Se compila con PyInstaller en GitHub Actions (.github/workflows/instalador.yml)."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BAT = "Instalar-Subtitulos-Cartel.bat"


def main() -> int:
    src = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / BAT
    tmp = Path(tempfile.mkdtemp(prefix="subtitulos_cartel_"))
    bat = tmp / BAT
    # CRLF obligatorio: cmd.exe falla con «call :etiqueta» si el .bat tiene saltos de línea Unix.
    bat.write_bytes(src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    try:
        return subprocess.call(["cmd.exe", "/c", str(bat)], cwd=str(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
