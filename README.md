# Subtítulos estilo cartel (y lip-sync PNGtuber)

<p align="center">
  <a href="https://github.com/intidie/video-con-audio-a-partir-de-imagenes/releases/latest/download/SubtitulosCartel-Instalador.exe">
    <img alt="Descargar para Windows 11" src="https://img.shields.io/badge/%E2%AC%87%20Descargar%20para%20Windows%2011-Instalador%20.exe-0078D4?style=for-the-badge&logo=windows11&logoColor=white" height="48">
  </a>
</p>

**Instalación en un clic (Windows 11):** pulsa el botón: descarga solo `SubtitulosCartel-Instalador.exe` (no hace falta
bajar el repositorio). Ábrelo y espera: instala Python, FFmpeg, Node.js y ExifTool si faltan (con `winget`), descarga la
última versión de la app, crea el acceso directo **Subtitulos Cartel** en el Escritorio y en el menú Inicio, y la abre.
Para actualizar, vuelve a ejecutarlo (se conservan tus ajustes y tus fuentes). Si Windows avisa de «SmartScreen»:
*Más información > Ejecutar de todos modos* (el .exe no está firmado). El .exe se compila solo con GitHub Actions
(`.github/workflows/instalador.yml`) cada vez que cambia el instalador.

Genera un video con subtítulos palabra por palabra sobre pantalla azul oscura (#00008B), listo para aplicar
**Ultra Key** en Premiere Pro. Pensado para ejecutarse **en local**, como una app de escritorio.

## Instalar como aplicación (Windows 11)
1. Instala **Python 3.10+** y **FFmpeg** (`winget install ffmpeg`).
2. Doble clic en `instalar.bat`: crea el entorno, instala todo y genera el acceso directo **Subtitulos Cartel**
   (en la carpeta y en el Escritorio).
3. Clic derecho en el acceso directo > *Mostrar más opciones* > **Anclar a la barra de tareas**.

Se abre en su propia ventana (Edge/Chrome en modo aplicación, sin pestañas) y **al cerrarla se apaga el servidor**,
sin dejar nada consumiendo recursos.

## Uso alternativo (consola)
1. Instala **Python 3.10+** y **FFmpeg** (Windows: `winget install ffmpeg`).
2. Doble clic en `iniciar.bat` (Windows) o `./iniciar.sh` (Linux/macOS). La primera vez instala las dependencias.
3. Pega el guion, ajusta el estilo con la previsualización y pulsa **Generar video con subtítulos**.

El video se guarda con las **3 primeras palabras del guion** como nombre (`hola_amigos_que.mp4`) en `output/` o en la
**carpeta de salida** que elijas arriba de las pestañas (botón **Examinar…** o escribiendo la ruta; se crea si no existe
y se recuerda en `ajustes.json`).

## Voz robótica (meSpeak.js) y estilo meme
- **Voz «Robot (meSpeak.js)»** en el selector de voz, con el mismo menú de meSpeak (Amplitude, Pitch, Speed, Word gap,
  Variant y Voice con todos los idiomas) y botón **Probar voz**: usan `mespeak/mespeak-core.js` (eSpeak en JavaScript) ejecutado con
  **Node.js** (`winget install OpenJS.NodeJS.LTS`), sin internet. Cada palabra se sintetiza por separado, así los tiempos
  de los subtítulos son exactos. Velocidad y tono usan los mismos sliders. Voces/variantes en `robot_tts.py`
  (puedes copiar más `.json` de voces a `mespeak/voices/`).
- **Estilo meme** (pestaña de estilo): texto simple, palabras repartidas de borde a borde, líneas apretadas y de baja
  calidad, con la fuente «Stamp RSPK One-Medium» si copias el .ttf/.otf en `fonts/` (si no, Cousine Bold). La fuente NO viene en el repo (la licencia de LRC Type Foundry no permite redistribuirla): usa **Subir fuente** en la pestaña de estilo y sube el `.zip` de Stamp RSPK (o los `.otf`); se instala sola en `fonts/` y se usa automáticamente. Color de letra =
  «Rellenar»; marco sólido centrado opcional y su color = «Fondo»; slider de ancho del bloque.

## Qué hay
- **Estilo cartel serif + Bebas Neue** por defecto, con los valores de Premiere (escala, tracking, posición...).
- **Animaciones** de entrada (pop, rebote, zoom, giro, desliza, cae, parpadeo, letras en cascada), de salida
  y movimiento continuo (latido, flotar). **Mosaico** solo sobre el subtítulo (entrada/salida/siempre).
- **Visor de animaciones**: control de «instante» cuadro a cuadro y botón ▶ para ver el clip en movimiento.
- **Compatible con Premiere Pro**: H.264 High (avc1, yuv420p, BT.709, GOP de 1 s, audio 48 kHz, fps constante), ProRes 422 HQ y DNxHR HQ.
- Los ajustes (última configuración usada) se guardan en `ajustes.json`; «Restablecer valores» vuelve a los de fábrica.

## Limpieza automática de metadatos (ExifCleaner integrado)
Se activa/desactiva en la barra lateral (**🧹 Limpiar metadatos al exportar**, activado por defecto) y ahí mismo puedes
limpiar un video que ya tengas (**Limpiar un video que ya tengo**). El instalador añade ExifTool, el motor de ExifCleaner.
Todo video exportado (subtítulos y lip-sync, en H.264, ProRes y DNxHR) pasa por `media.strip_metadata()`, **sin
recodificar** (no pierde calidad). Elimina las huellas que deja FFmpeg: etiquetas `encoder` (Lavf/Lavc), `creation_time`,
capítulos, nombres de handler y el SEI de x264 (`x264 - core ... options`), y evita que el codificador AAC escriba su
versión en el audio. Si tienes `exiftool` en el PATH (es lo que usa ExifCleaner por debajo), se aplica además
`exiftool -all=`. Si algo falla, se conserva el video original intacto.

> Ojo: esto limpia lo que genera **esta app**. Si luego editas el video (Premiere, CapCut, etc.) o usas herramientas
> con IA, esas apps pueden añadir sus propias marcas (p. ej. Content Credentials / C2PA) al exportar; revisa su
> configuración de exportación.

## Rendimiento (por qué ya no se cuelga)
- El video se parte en trozos que se renderizan **en paralelo** (un FFmpeg por núcleo, con prioridad baja)
  y se unen sin recodificar; con barra de progreso real.
- x264 `veryfast`, composición 4:2:0 para H.264, mosaico y granulado solo en la franja del subtítulo.
- La voz (TTS) y las vistas previas se cachean; la edición de estilo es un `st.fragment` que no recalcula la página.
- `.streamlit/config.toml` desactiva el vigilante de archivos y la telemetría.
