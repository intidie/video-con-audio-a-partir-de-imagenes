
import streamlit as st
import subprocess
import re
import shutil
import uuid
from pathlib import Path

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


def build_filelist(segments, talking_img, silent_img, filelist_path):
    lines = []
    for start, end, label in segments:
        img = talking_img if label == "talking" else silent_img
        lines.append(f"file '{img.name}'")
        lines.append(f"duration {round(end - start, 3)}")
    last_img = talking_img if segments[-1][2] == "talking" else silent_img
    lines.append(f"file '{last_img.name}'")
    filelist_path.write_text("\n".join(lines), encoding="utf-8")


def render_video(filelist_name, audio_name, out_name, fps, crf, work_dir):
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", filelist_name,
        "-i", audio_name,
        "-r", str(fps),
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

st.subheader("1. Archivos")
col1, col2, col3 = st.columns(3)
with col1:
    audio_file = st.file_uploader("Audio", type=["mp3", "wav", "m4a"])
with col2:
    talking_file = st.file_uploader("Imagen — Hablando", type=["png", "jpg", "jpeg"])
with col3:
    silent_file = st.file_uploader("Imagen — Silencio", type=["png", "jpg", "jpeg"])

st.subheader("2. Detección de Audio")
c1, c2 = st.columns(2)
with c1:
    noise_db = st.slider(
        "Umbral de Ruido (dB)", -50, -10, -35,
        help="Más alto (-25 dB): evita falsos positivos por ruido de fondo. Más bajo (-45 dB): detecta susurros o voces suaves.",
    )
with c2:
    min_silence = st.slider(
        "Sensibilidad de Detección (s)", 0.05, 0.50, 0.10, step=0.01,
        help="Bajo (0.05s): cambio de imagen muy reactivo. Alto (0.25s): evita parpadeos entre sílabas.",
    )

st.subheader("3. Formato y Calidad")
d1, d2, d3, d4 = st.columns(4)
with d1:
    res_label = st.selectbox("Resolución", list(RES_MAP.keys()), index=1)
with d2:
    aspect_label = st.selectbox(
        "Relación de Aspecto", list(ASPECT_MAP.keys()),
        help="Escala y centra la imagen automáticamente con padding, sin deformarla.",
    )
with d3:
    fps = st.selectbox("FPS", [24, 30, 60], index=1)
with d4:
    crf_label = st.selectbox("Calidad", list(CRF_MAP.keys()), index=1)

st.subheader("4. Generación")
generate = st.button("Generar Video", use_container_width=True)
status = st.empty()
result_slot = st.empty()

if generate:
    if not (audio_file and talking_file and silent_file):
        st.error("Sube el audio y las dos imágenes antes de continuar.")
    else:
        session_id = uuid.uuid4().hex[:8]
        work_dir = TEMP_DIR / session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            status.info("Guardando archivos...")
            audio_path = work_dir / f"audio{Path(audio_file.name).suffix}"
            audio_path.write_bytes(audio_file.getbuffer())
            talking_src = work_dir / f"talking_src{Path(talking_file.name).suffix}"
            talking_src.write_bytes(talking_file.getbuffer())
            silent_src = work_dir / f"silent_src{Path(silent_file.name).suffix}"
            silent_src.write_bytes(silent_file.getbuffer())

            width, height = target_dims(res_label, aspect_label)

            status.info("Analizando audio...")
            duration = get_duration(audio_path)
            silences = detect_silence(audio_path, noise_db, min_silence)
            segments = build_intervals(duration, silences)

            status.info("Procesando imágenes...")
            talking_img = work_dir / "talking.png"
            silent_img = work_dir / "silent.png"
            preprocess_image(talking_src, talking_img, width, height)
            preprocess_image(silent_src, silent_img, width, height)

            status.info("Construyendo lista de segmentos...")
            filelist_path = work_dir / "filelist.txt"
            build_filelist(segments, talking_img, silent_img, filelist_path)

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
