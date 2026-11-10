@echo off
setlocal
cd /d "%~dp0"
echo === Instalacion de Subtitulos Cartel ===
where ffmpeg >nul 2>nul || (echo [!] Falta FFmpeg. Instalalo con:  winget install ffmpeg   y vuelve a ejecutar este archivo. & pause & exit /b 1)
where python >nul 2>nul || (echo [!] Falta Python 3.10 o superior: https://www.python.org/downloads/ & pause & exit /b 1)
if not exist ".venv\Scripts\pythonw.exe" python -m venv .venv || (pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
".venv\Scripts\python.exe" crear_icono.py
rem Acceso directo (apunta a un .exe, por eso Windows 11 permite anclarlo a la barra de tareas)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$d=(Get-Location).Path; $s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\Subtitulos Cartel.lnk\");" ^
 "$s.TargetPath=\"$d\.venv\Scripts\pythonw.exe\"; $s.Arguments='launcher.pyw'; $s.WorkingDirectory=$d;" ^
 "$s.IconLocation=\"$d\icono.ico\"; $s.Description='Subtitulos estilo cartel'; $s.Save();" ^
 "Copy-Item \"$d\Subtitulos Cartel.lnk\" ([Environment]::GetFolderPath('Desktop')) -Force"
echo.
echo Listo. Se creo "Subtitulos Cartel" en esta carpeta y en el Escritorio.
echo Para anclarlo: clic derecho sobre el acceso directo ^> Mostrar mas opciones ^> Anclar a la barra de tareas.
pause
