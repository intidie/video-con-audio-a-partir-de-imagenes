
import streamlit as st
import subprocess
import re
import shutil
import uuid
from pathlib import Path
import asyncio
import edge_tts
import pyttsx3
import random
import itertools
from pathlib import Path

import json
from dataclasses import asdict

from subtitles import (
    CODECS, EXIT_STYLES, CaptionStyle, build_ass, estimate_word_timings,
    font_substitutions, list_font_families, prepare_fonts_dir, preview_block_count,
    render_green_video, render_preview_png, speech_segments_from_intervals,
    synthesize_with_timings, system_font_families, tokenize,
)

def generate_loquendo_speech(text, voice_id, rate, output_file):
    engine = pyttsx3.init()
    
    # Selecciona la voz de Loquendo
    engine.setProperty('voice', voice_id)
    
    # Ajusta la velocidad (Por defecto suele ser 200)
    engine.setProperty('rate', rate)
    
    # Guarda el resultado en un archivo de audio WAV
    engine.save_to_file(text, str(output_file))
    engine.runAndWait()


st.set_page_config(page_title="Lip-Sync Video Generator", layout="wide")

CUSTOM_CSS = """
<style>
:root { --bg:#FFFFFF; --border:#E5E5E5; --text:#111111; }
.stApp { background-color: var(--bg); color: var(--text); }
section[data-testid="stSidebar"] { background-color:#FAFAFA; border-right:1px solid var(--border); }
div[data-testid="stFileUploader"] { border:1px solid var(--border); border-radius:4px; padding:12px; background:#FFFFFF; }
.stButton > button { background-color:#111111; color:#FFFFFF; border:1px solid #111111; border-radius:4px; padding:0.5rem 1.5rem; font-weight:500; box-shadow:none; }
.stButton > button:hover { background-color:#333333; border:1px solid #333333; }
h1,h2,h3 { font-weight:600; color:#111111; }
hr { border-color: var(--border); }
div[data-baseweb="select"] > div { border-radius:4px; border:1px solid var(--border); box-shadow:none; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Caché de configuraciones: se guardan en settings_cache.json y se restauran al abrir la app.
SETTINGS_FILE = BASE_DIR / "settings_cache.json"
_NO_CACHE = {"sub_mode", "sub_prev_block", "sub_generate", "sub_download", "sub_text", "sub_audio",
             "sub_font_files", "sub_result"}


def _persistable(k, v=0):
    return isinstance(k, str) and k.startswith(("sub_", "cfg_")) and k not in _NO_CACHE and isinstance(v, (bool, int, float, str))


def _read_cfg():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


if "_cfg_loaded" not in st.session_state:
    st.session_state["_cfg_loaded"] = True
    _saved = {k: v for k, v in _read_cfg().items() if _persistable(k, v)}
    if _saved:
        st.session_state.update(_saved)
        st.rerun()


VOICES = {
    "Español (España) - Álvaro (Masculino)": "es-ES-AlvaroNeural",
    "Español (España) - Elvira (Femenino)": "es-ES-ElviraNeural",
    "Español (México) - Jorge (Masculino)": "es-MX-JorgeNeural",
    "Español (México) - Dalia (Femenino)": "es-MX-DaliaNeural",
    "Español (Colombia) - Gonzalo (Masculino)": "es-CO-GonzaloNeural",
    "Español (Colombia) - Salome (Femenino)": "es-CO-SalomeNeural",
}



RES_MAP = {
    "720p HD (1280x720)": (1280, 720),
    "1080p Full HD (1920x1080)": (1920, 1080),
    "2K (2560x1440)": (2560, 1440),
    "4K Ultra HD (3840x2160)": (3840, 2160),
}
ASPECT_MAP = {
    "16:9 (Horizontal)": (16, 9),
    "9:16 (Vertical)": (9, 16),
    "1:1 (Cuadrado)": (1, 1),
}
CRF_MAP = {"Máxima (CRF 18)": 18, "Alta (CRF 23)": 23, "Estándar (CRF 28)": 28}


async def generate_speech(text, voice, rate_str, pitch_str, output_file):
    communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
    await communicate.save(output_file)


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def get_duration(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    r = run(cmd)
    return float(r.stdout.strip())


def detect_silence(path, noise_db, min_dur):
    cmd = ["ffmpeg", "-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    log = run(cmd).stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    intervals = []
    for i, s in enumerate(starts):
        intervals.append((s, ends[i] if i < len(ends) else None))
    return intervals


def build_intervals(duration, silences):
    segments = []
    cursor = 0.0
    for s, e in silences:
        e = e if e is not None else duration
        if s > cursor:
            segments.append((cursor, s, "talking"))
        segments.append((s, e, "silent"))
        cursor = e
    if cursor < duration:
        segments.append((cursor, duration, "talking"))
    segments = [(a, b, lbl) for a, b, lbl in segments if b - a > 0.01]
    return segments if segments else [(0.0, duration, "talking")]


def preprocess_image(src, dst, width, height):
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    run(["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", str(dst)])


def build_filelist(segments, talking_imgs, silent_imgs, filelist_path, frame_rate=0.18):
    # Cabecera obligatoria: sin ella ffmpeg no trata el script como
    # "ffconcat" extendido y el directive "duration" se ignora salvo
    # en el primer/último bloque (causa del bug reportado).
    lines = ["ffconcat version 1.0"]
    # Round-robin: un ciclo por pool, persistente entre segmentos, para
    # que la rotación siga el orden de subida sin reiniciar en cada corte.
    talking_cycle = itertools.cycle(talking_imgs) if talking_imgs else None
    silent_cycle = itertools.cycle(silent_imgs) if silent_imgs else None

    for start, end, label in segments:
        duration = end - start
        cycle = talking_cycle if label == "talking" else silent_cycle
        if cycle is None:
            continue
        current = 0.0
        while current < duration - 1e-6:
            step = min(frame_rate, duration - current)
            img = next(cycle)
            lines.append(f"file '{img.name}'")
            lines.append(f"duration {round(step, 3)}")
            current += step

    last_cycle = talking_cycle if segments[-1][2] == "talking" else silent_cycle
    if last_cycle:
        lines.append(f"file '{next(last_cycle).name}'")
    filelist_path.write_text("\n".join(lines), encoding="utf-8")


def render_video(filelist_name, audio_name, out_name, fps, crf, work_dir):
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", filelist_name,
        "-i", audio_name,
        "-r", str(fps), "-fps_mode", "cfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", out_name,
    ]
    return run(cmd, cwd=work_dir)


def target_dims(res_label, aspect_label):
    base_w, base_h = RES_MAP[res_label]
    ar_w, ar_h = ASPECT_MAP[aspect_label]
    ratio = ar_w / ar_h
    if abs((base_w / base_h) - ratio) < 0.001:
        return base_w, base_h
    if ratio >= 1:
        width = max(base_w, base_h)
        height = round(width / ratio)
    else:
        height = max(base_w, base_h)
        width = round(height * ratio)
    return width + width % 2, height + height % 2


st.title("Generador de Video Lip-Sync (PNGtuber)")
st.caption("Genera video sincronizado a partir de audio y dos imágenes de estado.")


st.subheader("0. Generador de Voz (Texto a Voz / TTS)")
text_file = st.file_uploader("Cargar guion (.txt)", type=["txt"])
default_text = text_file.read().decode("utf-8") if text_file else ""
user_text = st.text_area("Texto a sintetizar:", value=default_text, height=120)

tc1, tc2, tc3 = st.columns(3)
with tc1:
    selected_voice_label = st.selectbox("Voz", list(VOICES.keys()), key="cfg_voice")
with tc2:
    speed = st.slider("Velocidad (%)", -50, 50, 0, step=5, key="cfg_speed")
with tc3:
    pitch = st.slider("Tono (Hz)", -50, 50, 0, step=5, key="cfg_pitch")

if st.button("Generar Audio de Voz", use_container_width=True):
    if user_text.strip():
        tts_work = TEMP_DIR / f"tts_{uuid.uuid4().hex[:6]}.mp3"
        rate_str = f"{'+' if speed >= 0 else ''}{speed}%"
        pitch_str = f"{'+' if pitch >= 0 else ''}{pitch}Hz"
        
        with st.spinner("Generando audio..."):
            asyncio.run(generate_speech(user_text, VOICES[selected_voice_label], rate_str, pitch_str, str(tts_work)))
            
        st.session_state["generated_audio_bytes"] = tts_work.read_bytes()
        st.success("Audio generado.")

if "generated_audio_bytes" in st.session_state:
    st.audio(st.session_state["generated_audio_bytes"], format="audio/mp3")
    st.download_button("Descargar MP3", st.session_state["generated_audio_bytes"], "narracion.mp3", "audio/mp3")

st.markdown("---")


st.subheader("1. Archivos")
col1, col2, col3 = st.columns(3)
with col1:
    audio_file = st.file_uploader("Audio (opcional si generaste voz arriba)", type=["mp3", "wav", "m4a"])
    use_gen_audio = st.checkbox("Usar audio generado en la Sección 0", value=True) if "generated_audio_bytes" in st.session_state else False
with col2:
    talking_files = st.file_uploader("Imágenes — Hablando", type=["png", "jpg", "jpeg"], accept_multiple_files=True)
with col3:
    silent_files = st.file_uploader("Imágenes — Silencio", type=["png", "jpg", "jpeg"], accept_multiple_files=True)


st.subheader("2. Detección de Audio")
c1, c2 = st.columns(2)
with c1:
    noise_db = st.slider(
        "Umbral de Ruido (dB)", -50, -10, -35, key="cfg_noise",
        help="Más alto (-25 dB): evita falsos positivos por ruido de fondo. Más bajo (-45 dB): detecta susurros o voces suaves.",
    )
with c2:
    min_silence = st.slider(
        "Sensibilidad de Detección (s)", 0.05, 0.50, 0.10, step=0.01, key="cfg_minsil",
        help="Bajo (0.05s): cambio de imagen muy reactivo. Alto (0.25s): evita parpadeos entre sílabas.",
    )

st.subheader("3. Formato y Calidad")
d1, d2, d3, d4 = st.columns(4)
with d1:
    res_label = st.selectbox("Resolución", list(RES_MAP.keys()), index=1, key="cfg_res")
with d2:
    aspect_label = st.selectbox(
        "Relación de Aspecto", list(ASPECT_MAP.keys()), key="cfg_aspect",
        help="Escala y centra la imagen automáticamente con padding, sin deformarla.",
    )
with d3:
    fps = st.selectbox("FPS", [24, 30, 60], index=1, key="cfg_fps")
with d4:
    crf_label = st.selectbox("Calidad", list(CRF_MAP.keys()), index=1, key="cfg_crf")

st.subheader("4. Generación")
generate = st.button("Generar Video", use_container_width=True)
status = st.empty()
result_slot = st.empty()

if generate:
    has_audio = (use_gen_audio and "generated_audio_bytes" in st.session_state) or (audio_file is not None)
    if not (has_audio and talking_files and silent_files):
        st.error("Asegúrate de contar con un audio (generado o subido) y ambas imágenes.")
    else:
        session_id = uuid.uuid4().hex[:8]
        work_dir = TEMP_DIR / session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            status.info("Guardando archivos...")
            if use_gen_audio and "generated_audio_bytes" in st.session_state:
                audio_path = work_dir / "audio.mp3"
                audio_path.write_bytes(st.session_state["generated_audio_bytes"])
            else:
                audio_path = work_dir / f"audio{Path(audio_file.name).suffix}"
                audio_path.write_bytes(audio_file.getbuffer())
            talking_srcs = []
            for talking_file in talking_files:
                talking_src = work_dir / f"talking_src{Path(talking_file.name).suffix}"
                talking_src.write_bytes(talking_file.getbuffer())
                talking_srcs.append(talking_src)
            silent_srcs = []
            for silent_file in silent_files:
                silent_src = work_dir / f"silent_src{Path(silent_file.name).suffix}"
                silent_src.write_bytes(silent_file.getbuffer())
                silent_srcs.append(silent_src)

            width, height = target_dims(res_label, aspect_label)

            status.info("Analizando audio...")
            duration = get_duration(audio_path)
            silences = detect_silence(audio_path, noise_db, min_silence)
            segments = build_intervals(duration, silences)

            status.info("Procesando imágenes...")
            talking_imgs = []
            for talking_src in talking_srcs:
                talking_img = work_dir / f"talking_{uuid.uuid4().hex[:8]}.png"
                preprocess_image(talking_src, talking_img, width, height)
                talking_imgs.append(talking_img)

            silent_imgs = []
            for silent_src in silent_srcs:
                silent_img = work_dir / f"silent_{uuid.uuid4().hex[:8]}.png"
                preprocess_image(silent_src, silent_img, width, height)
                silent_imgs.append(silent_img)

            status.info("Construyendo lista de segmentos...")
            filelist_path = work_dir / "filelist.txt"
            build_filelist(segments, talking_imgs, silent_imgs, filelist_path)

            status.info("Renderizando video con FFmpeg...")
            out_name = f"output_{session_id}.mp4"
            result = render_video(filelist_path.name, audio_path.name, out_name, fps, CRF_MAP[crf_label], work_dir)

            out_path = work_dir / out_name
            if out_path.exists() and out_path.stat().st_size > 0:
                final_path = OUTPUT_DIR / out_name
                shutil.copy(out_path, final_path)
                video_bytes = final_path.read_bytes()
                status.success(f"Video generado. {len(segments)} segmentos procesados.")
                result_slot.video(video_bytes)
                st.download_button(
                    "Descargar MP4", data=video_bytes, file_name=out_name, mime="video/mp4",
                    use_container_width=True,
                )
            else:
                status.error("Fallo en el renderizado.")
                st.code(result.stderr[-3000:])
        except Exception as exc:
            status.error(f"Error: {exc}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Subtítulos estilo Hormozi sobre pantalla azul oscura
# ---------------------------------------------------------------------------
FONTS_DIR = BASE_DIR / "fonts"
MAX_DOWNLOAD_MB = 500

st.markdown("---")
st.subheader("5. Subtítulos estilo Hormozi (pantalla azul oscura)")
st.caption(
    "Genera un video con fondo azul oscuro (#00008B) y subtítulos palabra por palabra, listo para "
    "aplicar Ultra Key en Premiere Pro y colocarlo sobre tu metraje."
)

st.markdown(
    """<style>
    div[data-testid="stColumn"]:has(.sub-preview-anchor),
    div[data-testid="column"]:has(.sub-preview-anchor) {position: sticky; top: 3.5rem; align-self: flex-start;}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def cached_system_fonts():
    return system_font_families()


@st.cache_data(show_spinner=False, max_entries=64)
def cached_preview(style_json: str, text: str, fonts_dir: str, w: int, h: int, block: int):
    return render_preview_png(CaptionStyle(**json.loads(style_json)), text, Path(fonts_dir), w, h, block)


def style_toggle(label: str, key: str, default_on: bool, default_color: str):
    """Casilla + selector de color en una fila (como las secciones Rellenar/Trazo/Fondo/Sombra de Premiere)."""
    c1, c2 = st.columns([4, 1])
    with c1:
        on = st.checkbox(label, value=default_on, key=f"{key}_on")
    with c2:
        color = st.color_picker(label, default_color, key=f"{key}_color", disabled=not on, label_visibility="collapsed")
    return on, color


sub_text = st.text_area("Texto de los subtítulos:", value=user_text, height=120, key="sub_text")

audio_modes = ["Generar voz con TTS (tiempos exactos por palabra)"]
if "generated_audio_bytes" in st.session_state:
    audio_modes.append("Usar el audio generado en la Sección 0 (tiempos estimados)")
audio_modes.append("Subir un audio (tiempos estimados)")
sub_mode = st.radio("Fuente del audio", audio_modes, key="sub_mode")

sub_audio_file = None
if sub_mode.startswith("Subir"):
    sub_audio_file = st.file_uploader("Audio", type=["mp3", "wav", "m4a"], key="sub_audio")
if sub_mode.startswith("Generar"):
    st.caption(f"Usa la voz, velocidad y tono de la Sección 0: {selected_voice_label} · {speed:+d}% · {pitch:+d} Hz")
else:
    with st.expander("Detección de pausas (para estimar tiempos)"):
        sp1, sp2 = st.columns(2)
        with sp1:
            sub_noise_db = st.slider("Umbral de ruido (dB)", -50, -10, -35, key="sub_noise")
        with sp2:
            sub_min_silence = st.slider("Pausa mínima (s)", 0.10, 0.80, 0.25, step=0.05, key="sub_minsil")

st.markdown("**Estilo y previsualización en tiempo real**")
ctrl_col, prev_col = st.columns([3, 2], gap="large")
with prev_col:
    st.markdown('<span class="sub-preview-anchor"></span>', unsafe_allow_html=True)
    preview_slot = st.container()

with ctrl_col:
    st.markdown("##### Fuente")
    sub_font_files = st.file_uploader(
        "Subir fuentes propias (.ttf / .otf): SimSun, Lucida Console, Georgia...",
        type=["ttf", "otf"], accept_multiple_files=True, key="sub_font_files",
    )
    sub_fonts_dir = prepare_fonts_dir(FONTS_DIR, [(f.name, f.getvalue()) for f in sub_font_files], TEMP_DIR)
    bundled_fonts = list_font_families(sub_fonts_dir)
    other_fonts = [f for f in cached_system_fonts() if f not in bundled_fonts]
    OTHER_FONT = "Otra (escribir el nombre)..."
    font_options = bundled_fonts + other_fonts + [OTHER_FONT]
    default_font = next((f for f in ("Georgia", "Gelasio") if f in font_options), font_options[0])
    if st.session_state.get("sub_font") not in font_options:
        st.session_state.pop("sub_font", None)  # fuente guardada que ya no existe
    sub_font_choice = st.selectbox("Fuente", font_options, index=font_options.index(default_font), key="sub_font")
    if sub_font_choice == OTHER_FONT:
        sub_font_family = st.text_input(
            "Nombre exacto de la fuente instalada", value="SimSun", key="sub_font_custom",
            help="Debe estar instalada en el equipo/servidor; si no, súbela arriba y aparecerá en la lista.",
        ).strip() or default_font
    else:
        sub_font_family = sub_font_choice

    t1, t2, t3, t4, t5 = st.columns([2, 1.3, 1.3, 1.5, 1.7])
    with t1:
        sub_size = st.number_input("Tamaño", 10.0, 400.0, 100.0, key="sub_size")
    with t2:
        sub_bold = st.checkbox("Negrita", value=False, key="sub_bold")
    with t3:
        sub_italic = st.checkbox("Cursiva", value=True, key="sub_italic")
    with t4:
        sub_underline = st.checkbox("Subrayado", value=False, key="sub_underline")
    with t5:
        sub_upper = st.checkbox("MAYÚSCULAS", value=False, key="sub_upper")

    st.markdown("##### Rellenar · Trazo · Fondo · Sombra")
    sub_fill_on, sub_fill = style_toggle("Rellenar", "sub_fill", True, "#FFFFFF")

    sub_stroke_on, sub_stroke_color = style_toggle("Trazo (contorno exterior)", "sub_stroke", True, "#000000")
    sub_stroke = st.number_input("Grosor del trazo (px)", 0.0, 30.0, 4.0, step=0.5, key="sub_stroke", disabled=not sub_stroke_on)

    sub_box_on, sub_box_color = style_toggle("Fondo (marco sólido de color)", "sub_box", False, "#000000")
    b1, b2, b3 = st.columns([2, 1, 1])
    with b1:
        sub_box_opacity = st.slider("Opacidad del fondo (%)", 0, 100, 100, key="sub_box_op", disabled=not sub_box_on)
    with b2:
        sub_box_pad = st.number_input("Margen (px)", 0.0, 100.0, 10.0, step=1.0, key="sub_box_pad", disabled=not sub_box_on)
    with b3:
        sub_box_radius = st.number_input("Esquinas (px)", 0.0, 200.0, 0.0, step=2.0, key="sub_box_radius", disabled=not sub_box_on)

    sub_shadow_on, sub_shadow_color = style_toggle("Sombra", "sub_shadow", False, "#000000")
    sd1, sd2 = st.columns(2)
    with sd1:
        sub_shadow_dist = st.number_input("Distancia de la sombra (px)", 0.0, 60.0, 6.0, step=1.0, key="sub_shadow_dist", disabled=not sub_shadow_on)
    with sd2:
        sub_shadow_op = st.slider("Opacidad de la sombra (%)", 0, 100, 60, key="sub_shadow_op", disabled=not sub_shadow_on)

    st.markdown("##### Bloques y animación")
    e1, e2 = st.columns(2)
    with e1:
        sub_words = st.slider("Palabras por bloque", 1, 4, 2, key="sub_words")
    with e2:
        sub_chars = st.slider("Máx. caracteres por línea", 6, 24, 10, key="sub_chars")
    sub_exit_label = st.selectbox("Animación de salida", list(EXIT_STYLES.keys()), key="sub_exit")
    an1, an2 = st.columns(2)
    with an1:
        sub_letters = st.checkbox("Animar letras alternas (1 s)", value=True, key="sub_letters")
    with an2:
        sub_wobble = st.checkbox("Vaivén del recuadro cada 0,5 s (±2 px)", value=True, key="sub_wobble")
    st.markdown("##### Estilo cartel (SADE · SUPER STAR · INTI-NET)")
    ps1, ps2, ps3 = st.columns(3)
    with ps1:
        sub_poster = st.checkbox("Estilo cartel serif", value=False, key="sub_poster")
    with ps2:
        sub_invert = st.checkbox("Invertir colores", value=False, key="sub_invert", disabled=not sub_poster)
    with ps3:
        sub_grain = st.checkbox("Granulado", value=False, key="sub_grain")
    h1, h2 = st.columns([3, 1])
    with h1:
        sub_highlight = st.checkbox("Resaltar palabra activa", value=True, key="sub_hl")
    with h2:
        sub_hl_color = st.color_picker("Color del resaltado", "#FFE600", key="sub_hlc", disabled=not sub_highlight, label_visibility="collapsed")

    with st.expander("Transformar (valores de Premiere Pro)"):
        a1, a2 = st.columns(2)
        with a1:
            sub_sx = st.number_input("Escala horizontal (%)", 10.0, 600.0, 135.0, key="sub_sx")
            sub_track = st.number_input("Tracking", -500.0, 500.0, -55.0, key="sub_track")
            sub_px = st.number_input("Posición X (sobre 1080)", 0.0, 4000.0, 535.1, key="sub_px")
        with a2:
            sub_sy = st.number_input("Escala vertical (%)", 10.0, 600.0, 308.0, key="sub_sy")
            sub_lead = st.number_input("Interlineado", -300.0, 300.0, -54.0, key="sub_lead")
            sub_py = st.number_input("Posición Y (sobre 1920)", 0.0, 4000.0, 800.4, key="sub_py")

st.markdown("**Formato, calidad y códec**")
f1, f2, f3, f4 = st.columns(4)
with f1:
    sub_res = st.selectbox("Resolución", list(RES_MAP.keys()), index=1, key="sub_res")
with f2:
    sub_aspect = st.selectbox("Relación de aspecto", list(ASPECT_MAP.keys()), index=1, key="sub_aspect")
with f3:
    sub_fps = st.selectbox("FPS", [24, 30, 60], index=1, key="sub_fps")
with f4:
    sub_codec = st.selectbox(
        "Códec", list(CODECS.keys()), key="sub_codec",
        help="H.264 pesa poco. ProRes/DNxHR conservan mejor los bordes para el chroma key pero generan archivos "
             "muy grandes: úsalos con textos cortos o descarga el .mov desde una ejecución local.",
    )
g1, g2 = st.columns(2)
with g1:
    sub_crf_label = st.selectbox(
        "Calidad (solo H.264)", list(CRF_MAP.keys()), index=0, key="sub_crf",
        disabled=not CODECS[sub_codec]["uses_crf"],
    )
with g2:
    sub_include_audio = st.checkbox("Incluir el audio en el video", value=True, key="sub_inc_audio")

sub_style = CaptionStyle(
    font_family=sub_font_family, font_size=sub_size, italic=sub_italic, bold=sub_bold,
    underline=sub_underline, uppercase=sub_upper,
    fill_on=sub_fill_on, fill=sub_fill,
    stroke_on=sub_stroke_on, stroke=sub_stroke_color, stroke_px=sub_stroke,
    box_on=sub_box_on, box_color=sub_box_color, box_opacity=sub_box_opacity, box_pad=sub_box_pad, box_radius=sub_box_radius,
    shadow_on=sub_shadow_on, shadow_color=sub_shadow_color, shadow_dist=sub_shadow_dist, shadow_opacity=sub_shadow_op,
    scale_x=sub_sx, scale_y=sub_sy, tracking=sub_track, leading=sub_lead, pos_x=sub_px, pos_y=sub_py,
    words_per_block=sub_words, max_chars_line=sub_chars,
    highlight=sub_highlight, highlight_color=sub_hl_color, exit_style=EXIT_STYLES[sub_exit_label],
    letter_anim=sub_letters, wobble=sub_wobble, poster=sub_poster, invert=sub_invert, grain=sub_grain,
)

# Previsualización en tiempo real: Streamlit vuelve a ejecutar el script al tocar cualquier control
# y este fotograma se dibuja con el mismo motor (libass) que el video final.
with preview_slot:
    st.markdown("##### Previsualización")
    n_blocks = preview_block_count(sub_text, sub_style)
    if st.session_state.get("sub_prev_block", 1) > n_blocks:
        st.session_state["sub_prev_block"] = n_blocks
    if n_blocks > 1:
        prev_block = st.slider("Bloque de texto a previsualizar", 1, n_blocks, 1, key="sub_prev_block") - 1
    else:
        prev_block = 0
    pw, ph = target_dims(sub_res, sub_aspect)
    pscale = 640 / max(pw, ph)
    pw, ph = int(pw * pscale) // 2 * 2, int(ph * pscale) // 2 * 2
    prev_png, prev_log, _ = cached_preview(json.dumps(asdict(sub_style)), sub_text, str(sub_fonts_dir), pw, ph, prev_block)
    if prev_png:
        st.image(prev_png, caption="Fondo azul oscuro #00008B. Se actualiza al instante al cambiar cualquier control.")
    else:
        st.error("No se pudo generar la previsualización.")
        st.code(prev_log[-1500:])
    for req_font, used_font in font_substitutions(prev_log):
        st.warning(
            f"La fuente «{req_font}» no está instalada: se está usando «{used_font}». "
            "Súbela en «Subir fuentes propias» y aparecerá en la lista."
        )

if st.button("Generar video con subtítulos", use_container_width=True, key="sub_generate"):
    st.session_state.pop("sub_result", None)
    if not sub_text.strip():
        st.error("Escribe el texto de los subtítulos.")
    elif sub_mode.startswith("Subir") and sub_audio_file is None:
        st.error("Sube un audio o cambia la fuente del audio.")
    else:
        sub_session = uuid.uuid4().hex[:8]
        sub_dir = TEMP_DIR / f"sub_{sub_session}"
        sub_dir.mkdir(parents=True, exist_ok=True)
        sub_status = st.empty()
        try:
            if sub_mode.startswith("Generar"):
                sub_status.info("Generando voz y tiempos por palabra...")
                audio_path = sub_dir / "audio.mp3"
                rate_str = f"{'+' if speed >= 0 else ''}{speed}%"
                pitch_str = f"{'+' if pitch >= 0 else ''}{pitch}Hz"
                words, timing_mode = synthesize_with_timings(
                    sub_text, VOICES[selected_voice_label], rate_str, pitch_str, audio_path
                )
            else:
                sub_status.info("Analizando audio y estimando tiempos...")
                if sub_mode.startswith("Usar"):
                    audio_path = sub_dir / "audio.mp3"
                    audio_path.write_bytes(st.session_state["generated_audio_bytes"])
                else:
                    audio_path = sub_dir / f"audio{Path(sub_audio_file.name).suffix}"
                    audio_path.write_bytes(sub_audio_file.getbuffer())
                audio_len = get_duration(audio_path)
                segs = speech_segments_from_intervals(
                    build_intervals(audio_len, detect_silence(audio_path, sub_noise_db, sub_min_silence))
                )
                words = estimate_word_timings(tokenize(sub_text), segs)
                timing_mode = "estimados"

            if not words:
                raise RuntimeError("No se obtuvieron palabras con tiempos. Revisa el texto y el audio.")

            audio_len = get_duration(audio_path)
            total = audio_len + 0.4
            width, height = target_dims(sub_res, sub_aspect)
            style = sub_style
            (sub_dir / "captions.ass").write_text(build_ass(words, style, width, height, total, sub_fonts_dir), encoding="utf-8")

            sub_status.info("Renderizando video con FFmpeg...")
            ext = CODECS[sub_codec]["ext"]
            out_name = f"subtitulos_{sub_session}.{ext}"
            res = render_green_video(
                "captions.ass", audio_path.name if sub_include_audio else None, out_name,
                width, height, sub_fps, total, sub_codec, CRF_MAP[sub_crf_label], sub_fonts_dir, sub_dir,
                grain=style.grain,
            )
            out_path = sub_dir / out_name
            if out_path.exists() and out_path.stat().st_size > 0:
                final_path = OUTPUT_DIR / out_name
                shutil.copy(out_path, final_path)
                size_mb = final_path.stat().st_size / 1e6
                if size_mb > MAX_DOWNLOAD_MB:
                    sub_status.warning(
                        f"El archivo pesa {size_mb:.0f} MB, demasiado para descargarlo desde la app. "
                        "Usa H.264, un texto más corto o ejecuta la app en local."
                    )
                else:
                    st.session_state["sub_result"] = {
                        "bytes": final_path.read_bytes(), "name": out_name, "ext": ext,
                        "info": f"{len(words)} palabras · tiempos {timing_mode} · {width}x{height} @ {sub_fps} fps · {size_mb:.1f} MB",
                    }
                    sub_status.success("Video con subtítulos generado.")
            else:
                sub_status.error("Fallo en el renderizado.")
                st.code(res.stderr[-3000:])
        except Exception as exc:
            sub_status.error(f"Error: {exc}")
        finally:
            shutil.rmtree(sub_dir, ignore_errors=True)

if "sub_result" in st.session_state:
    r = st.session_state["sub_result"]
    st.caption(r["info"])
    if r["ext"] == "mp4":
        st.video(r["bytes"])
    else:
        st.info("Los .mov (ProRes/DNxHR) no se previsualizan en el navegador; descárgalo para verlo en Premiere.")
    st.download_button(
        f"Descargar {r['ext'].upper()}", data=r["bytes"], file_name=r["name"],
        mime="video/mp4" if r["ext"] == "mp4" else "video/quicktime", use_container_width=True,
        key="sub_download",
    )


# Guarda las configuraciones actuales (se combinan con las ya guardadas) para la próxima visita.
_cur = {k: v for k, v in st.session_state.items() if _persistable(k, v)}
_old = _read_cfg()
if _cur and {**_old, **_cur} != _old:
    try:
        SETTINGS_FILE.write_text(json.dumps({**_old, **_cur}, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
