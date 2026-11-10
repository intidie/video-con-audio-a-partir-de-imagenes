"""Abre la app como una aplicación de escritorio: arranca el servidor en segundo plano y muestra la interfaz
en una ventana propia (Edge/Chrome en modo --app, sin pestañas ni barra de direcciones).
Al cerrar la ventana se detiene el servidor y se libera toda la memoria. Se ejecuta con pythonw (sin consola)."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
NOWIN = 0x08000000 if os.name == "nt" else 0


def free_port(start: int = 8501) -> int:
    for p in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def find_browser() -> str | None:
    cands = []
    for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            cands += [Path(root, "Microsoft/Edge/Application/msedge.exe"), Path(root, "Google/Chrome/Application/chrome.exe")]
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")


def main() -> None:
    port = free_port()
    log = open(BASE / "temp_servidor.log", "w")
    server = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(port), "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        cwd=BASE, stdout=log, stderr=log, creationflags=NOWIN,
    )
    url = f"http://localhost:{port}"
    try:
        for _ in range(120):  # hasta ~30 s
            try:
                if urllib.request.urlopen(f"{url}/_stcore/health", timeout=1).status == 200:
                    break
            except Exception:
                if server.poll() is not None:
                    return
                time.sleep(0.25)
        browser = find_browser()
        if browser:
            # Perfil propio: ventana independiente que se puede anclar y cuyo cierre termina el proceso.
            win = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={BASE / '.perfil'}",
                                    "--window-size=1400,900", "--no-first-run", "--no-default-browser-check"])
            win.wait()
        else:
            webbrowser.open(url)
            while server.poll() is None:  # sin Edge/Chrome: el servidor sigue hasta cerrarlo a mano
                time.sleep(2)
    finally:
        if server.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], creationflags=NOWIN, capture_output=True)
            else:
                server.terminate()
        log.close()


if __name__ == "__main__":
    main()
