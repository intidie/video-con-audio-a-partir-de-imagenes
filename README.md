# Subtítulos estilo cartel (y lip-sync PNGtuber)

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

El video se guarda en `output/` con las **3 primeras palabras del guion** como nombre (`hola_amigos_que.mp4`).

## Qué hay
- **Estilo cartel serif + Bebas Neue** por defecto, con los valores de Premiere (escala, tracking, posición...).
- **Animaciones** de entrada (pop, rebote, zoom, giro, desliza, cae, parpadeo, letras en cascada), de salida
  y movimiento continuo (latido, flotar). **Mosaico** solo sobre el subtítulo (entrada/salida/siempre).
- **Visor de animaciones**: control de «instante» cuadro a cuadro y botón ▶ para ver el clip en movimiento.
- Los ajustes se guardan en `ajustes.json`; «Restablecer valores» vuelve a los de fábrica.

## Rendimiento (por qué ya no se cuelga)
- El video se parte en trozos que se renderizan **en paralelo** (un FFmpeg por núcleo, con prioridad baja)
  y se unen sin recodificar; con barra de progreso real.
- x264 `veryfast`, composición 4:2:0 para H.264, mosaico y granulado solo en la franja del subtítulo.
- La voz (TTS) y las vistas previas se cachean; la edición de estilo es un `st.fragment` que no recalcula la página.
- `.streamlit/config.toml` desactiva el vigilante de archivos y la telemetría.
