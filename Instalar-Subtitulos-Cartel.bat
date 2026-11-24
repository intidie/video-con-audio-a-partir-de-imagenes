@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Instalador - Subtitulos Cartel
set "REPO=intidie/video-con-audio-a-partir-de-imagenes"
set "DEST=%LOCALAPPDATA%\SubtitulosCartel"
echo ============================================================
echo   Instalador de Subtitulos Cartel para Windows 11
echo   Instala Python, FFmpeg, Node.js y ExifTool si faltan, descarga la app
echo   y crea el acceso directo. Solo hay que esperar.
echo ============================================================
echo.

where winget >nul 2>nul || (echo [!] No se encontro winget. Actualiza "Instalador de aplicacion" desde Microsoft Store y vuelve a ejecutar. & pause & exit /b 1)

call :need python  Python.Python.3.12
call :need ffmpeg  Gyan.FFmpeg
call :need node    OpenJS.NodeJS.LTS
call :need exiftool OliverBetz.ExifTool
call :refresh_path
where python >nul 2>nul || (echo [!] Python no quedo disponible. Cierra esta ventana y vuelve a ejecutar el instalador. & pause & exit /b 1)
where ffmpeg >nul 2>nul || (echo [!] FFmpeg no quedo disponible. Cierra esta ventana y vuelve a ejecutar el instalador. & pause & exit /b 1)

echo.
echo [1/4] Descargando la ultima version de la app...
set "ZIP=%TEMP%\subtitulos_cartel.zip"
set "TMPX=%TEMP%\subtitulos_cartel_x"
if exist "%TMPX%" rmdir /s /q "%TMPX%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing 'https://github.com/%REPO%/archive/refs/heads/main.zip' -OutFile '%ZIP%'; Expand-Archive -Force '%ZIP%' '%TMPX%'" || (echo [!] No se pudo descargar. Revisa tu internet. & pause & exit /b 1)
for /d %%d in ("%TMPX%\*") do set "SRC=%%d"
if not exist "%DEST%" mkdir "%DEST%"
robocopy "!SRC!" "%DEST%" /E /NFL /NDL /NJH /NJS /NP /XD .venv output temp .perfil /XF ajustes.json >nul
del "%ZIP%" >nul 2>nul
rmdir /s /q "%TMPX%" >nul 2>nul

echo [2/4] Creando entorno e instalando dependencias (puede tardar unos minutos)...
cd /d "%DEST%"
if not exist ".venv\Scripts\pythonw.exe" python -m venv .venv || (echo [!] No se pudo crear el entorno. & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo [!] Fallo la instalacion de dependencias. & pause & exit /b 1)
".venv\Scripts\python.exe" crear_icono.py >nul 2>nul

echo [3/4] Creando accesos directos...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$d='%DEST%'; $w=New-Object -ComObject WScript.Shell;" ^
 "foreach($p in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){" ^
 " $s=$w.CreateShortcut(\"$p\Subtitulos Cartel.lnk\"); $s.TargetPath=\"$d\.venv\Scripts\pythonw.exe\"; $s.Arguments='launcher.pyw';" ^
 " $s.WorkingDirectory=$d; $s.IconLocation=\"$d\icono.ico\"; $s.Description='Subtitulos estilo cartel'; $s.Save() }"

echo [4/4] Listo. Abriendo la aplicacion...
start "" "%DEST%\.venv\Scripts\pythonw.exe" "%DEST%\launcher.pyw"
echo.
echo Se creo el acceso directo "Subtitulos Cartel" en el Escritorio y en el menu Inicio.
echo Para anclarlo: clic derecho ^> Mostrar mas opciones ^> Anclar a la barra de tareas.
echo Para actualizar en el futuro, vuelve a ejecutar este instalador (se conservan tus ajustes).
pause
exit /b 0

:need
where %1 >nul 2>nul && exit /b 0
echo Instalando %2 ...
winget install -e --id %2 --silent --accept-package-agreements --accept-source-agreements
call :refresh_path
exit /b 0

:refresh_path
for /f "delims=" %%i in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')"') do set "PATH=%%i"
exit /b 0
