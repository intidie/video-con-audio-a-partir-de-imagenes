"""Generador de subtítulos estilo cartel (y lip-sync PNGtuber). Pensado para ejecutarse en local."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from media import (
    ASPECT_MAP, CRF_MAP, RES_MAP, build_intervals, detect_silence, ffmpeg_available, get_duration,
    make_lipsync, speech_segments, target_dims,
)
from subtitles import (
    CODECS, ENTRY_STYLES, EXIT_STYLES, MOSAIC_MODES, MOTION_STYLES, CaptionStyle, Word, block_duration_ms,
    estimate_word_timings, font_substitutions, list_font_families, name_from_text, preview_block_count,
    render_captions, render_preview_clip, render_preview_png, synthesize_with_timings, system_font_families,
    tokenize, unique_path,
)

st.set_page_config(page_title="Subtítulos estilo cartel", page_icon="🎬", layout="wide")

BASE_DIR = Path(__file__).parent
TEMP_DIR, OUTPUT_DIR, FONTS_DIR = BASE_DIR / "temp", BASE_DIR / "output", BASE_DIR / "fonts"
SETTINGS_FILE = BASE_DIR / "ajustes.json"
MAX_PREVIEW_MB = 80
OTHER_FONT = "Otra (escribir el nombre)..."

VOICES = {
    "Español (España) - Álvaro (Masculino)": "es-ES-AlvaroNeural",
    "Español (España) - Elvira (Femenino)": "es-ES-ElviraNeural",
    "Español (México) - Jorge (Masculino)": "es-MX-JorgeNeural",
    "Español (México) - Dalia (Femenino)": "es-MX-DaliaNeural",
    "Español (Colombia) - Gonzalo (Masculino)": "es-CO-GonzaloNeural",
    "Español (Colombia) - Salome (Femenino)": "es-CO-SalomeNeural",
}
SRC_TTS = "Generar voz con TTS (tiempos exactos por palabra)"
SRC_UPLOAD = "Subir un audio (tiempos estimados)"
FIRST_CODEC = next(iter(CODECS))

# Valores por defecto (los de las capturas). Las claves son las de los widgets.
DEFAULTS = {
    "voice": "Español (Colombia) - Salome (Femenino)", "speed": 50, "pitch": -25, "src_mode": SRC_TTS,
    "noise": -35, "minsil": 0.25,
    "font": "Bebas Neue", "size": 150.0, "bold": False, "italic": True, "underline": False, "upper": False,
    "fill_on": True, "fill_color": "#FFFFFF", "stroke_on": True, "stroke_color": "#000000", "stroke": 4.0,
    "box_on": False, "box_color": "#000000", "box_op": 100, "box_pad": 10.0, "box_radius": 0.0,
    "shadow_on": False, "shadow_color": "#000000", "shadow_dist": 6.0, "shadow_op": 60,
    "words": 1, "chars": 10, "entry": "Pop: crece", "exit": "Sin animación de salida",
    "motion": "Sin movimiento continuo", "in_ms": 110, "out_ms": 160, "letters": True, "wobble": True,
    "mosaic": "Sin mosaico", "mosaic_px": 36, "mosaic_ms": 300,
    "poster": True, "invert": False, "grain": False, "hl": True, "hl_color": "#FFE600",
    "sx": 135.0, "sy": 308.0, "track": -55.0, "lead": -54.0, "px": 535.1, "py": 500.0,
    "res": "2K (2560x1440)", "aspect": "9:16 (Vertical)", "fps": 60, "codec": FIRST_CODEC,
    "crf": "Máxima (CRF 18)", "inc_audio": True,
    "ls_noise": -35, "ls_minsil": 0.10, "ls_res": "1080p Full HD (1920x1080)", "ls_aspect": "16:9 (Horizontal)",
    "ls_fps": 30, "ls_crf": "Alta (CRF 23)",
}
OPTIONS = {
    "voice": list(VOICES), "src_mode": [SRC_TTS, SRC_UPLOAD], "entry": list(ENTRY_STYLES), "exit": list(EXIT_STYLES),
    "motion": list(MOTION_STYLES), "mosaic": list(MOSAIC_MODES), "res": list(RES_MAP), "aspect": list(ASPECT_MAP),
    "fps": [24, 30, 60], "codec": list(CODECS), "crf": list(CRF_MAP), "ls_res": list(RES_MAP),
    "ls_aspect": list(ASPECT_MAP), "ls_fps": [24, 30, 60], "ls_crf": list(CRF_MAP),
}
ss = st.session_state

st.markdown(
    """<style>
    .block-container {padding-top: 2rem; max-width: 1500px;}
    div[data-testid="stTextArea"] textarea {resize: vertical; min-height: 240px; font-size: 1.05rem; line-height: 1.5;}
    .stButton > button[kind="secondary"] {background:#111; color:#fff; border:1px solid #111; border-radius:4px; font-weight:500;}
    .stButton > button[kind="secondary"]:hover {background:#333; border-color:#333; color:#fff;}
    div[data-testid="stColumn"]:has(.prev-anchor) {position: sticky; top: 3.5rem; align-self: flex-start;}
    </style>""",
    unsafe_allow_html=True,
)

if not ffmpeg_available():
    st.error("No se encontró FFmpeg/ffprobe. Instálalo y agrégalo al PATH (Windows: `winget install ffmpeg`).")
    st.stop()
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# Ajustes persistentes (ajustes.json): se restauran al abrir la app
# --------------------------------------------------------------------------- #
def _load_saved() -> dict:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    ok = {}
    for k, v in raw.items():
        d = DEFAULTS.get(k)
        if d is None or isinstance(v, bool) != isinstance(d, bool):
            continue
        if isinstance(d, (int, float)) and not isinstance(d, bool) and isinstance(v, (int, float)):
            v = type(d)(v)
        if type(v) is type(d) and (k not in OPTIONS or v in OPTIONS[k]):
            ok[k] = v
    return ok


def save_settings() -> None:
    cur = {k: ss[k] for k in DEFAULTS if k in ss}
    if cur and cur != ss.get("_saved"):
        ss["_saved"] = cur
        try:
            SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


if "_init" not in ss:
    ss["_init"] = True
    ss.update({**DEFAULTS, **_load_saved()})
    ss["script"] = ""
    for old in TEMP_DIR.glob("job_*"):  # restos de ejecuciones interrumpidas
        shutil.rmtree(old, ignore_errors=True)


def reset_settings() -> None:
    SETTINGS_FILE.unlink(missing_ok=True)
    ss.pop("_saved", None)
    ss.update(DEFAULTS)


# --------------------------------------------------------------------------- #
# Caché
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False, max_entries=6)
def cached_tts(text: str, voice_id: str, rate: str, pitch: str):
    """Voz + tiempos por palabra. Se cachea: regenerar el video con otro estilo no vuelve a llamar al TTS."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.mp3"
        words, mode = synthesize_with_timings(text, voice_id, rate, pitch, out)
        return out.read_bytes(), tuple((w.text, w.start, w.end) for w in words), mode


@st.cache_data(show_spinner=False, max_entries=6)
def cached_estimate(audio: bytes, suffix: str, text: str, noise: int, minsil: float):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f"a{suffix}"
        p.write_bytes(audio)
        dur = get_duration(p)
        segs = speech_segments(build_intervals(dur, detect_silence(p, noise, minsil)))
        words = estimate_word_timings(tokenize(text), segs)
        return tuple((w.text, w.start, w.end) for w in words), dur


@st.cache_data(show_spinner=False, max_entries=120)
def cached_preview(style_json: str, text: str, w: int, h: int, block: int, t_ms: int):
    return render_preview_png(CaptionStyle(**json.loads(style_json)), text, FONTS_DIR, w, h, block, t_ms)


@st.cache_data(show_spinner="Renderizando la animación...", max_entries=6)
def cached_clip(style_json: str, text: str, w: int, h: int):
    return render_preview_clip(CaptionStyle(**json.loads(style_json)), text, FONTS_DIR, w, h)


@st.cache_data(show_spinner=False)
def font_options() -> list[str]:
    bundled = list_font_families(str(FONTS_DIR))
    return bundled + [f for f in system_font_families() if f not in bundled] + [OTHER_FONT]


# --------------------------------------------------------------------------- #
# Estilo a partir de los widgets
# --------------------------------------------------------------------------- #
def style_from_state() -> CaptionStyle:
    font = (ss.get("font_custom") or "").strip() if ss["font"] == OTHER_FONT else ss["font"]
    return CaptionStyle(
        font_family=font or DEFAULTS["font"], font_size=ss["size"], italic=ss["italic"], bold=ss["bold"],
        underline=ss["underline"], uppercase=ss["upper"], fill_on=ss["fill_on"], fill=ss["fill_color"],
        stroke_on=ss["stroke_on"], stroke=ss["stroke_color"], stroke_px=ss["stroke"],
        box_on=ss["box_on"], box_color=ss["box_color"], box_opacity=ss["box_op"], box_pad=ss["box_pad"],
        box_radius=ss["box_radius"], shadow_on=ss["shadow_on"], shadow_color=ss["shadow_color"],
        shadow_dist=ss["shadow_dist"], shadow_opacity=ss["shadow_op"],
        scale_x=ss["sx"], scale_y=ss["sy"], tracking=ss["track"], leading=ss["lead"], pos_x=ss["px"], pos_y=ss["py"],
        words_per_block=ss["words"], max_chars_line=ss["chars"], highlight=ss["hl"], highlight_color=ss["hl_color"],
        entry_style=ENTRY_STYLES[ss["entry"]], exit_style=EXIT_STYLES[ss["exit"]], motion=MOTION_STYLES[ss["motion"]],
        in_ms=ss["in_ms"], out_ms=ss["out_ms"], letter_anim=ss["letters"], wobble=ss["wobble"],
        mosaic=MOSAIC_MODES[ss["mosaic"]], mosaic_px=ss["mosaic_px"], mosaic_ms=ss["mosaic_ms"],
        poster=ss["poster"], invert=ss["invert"], grain=ss["grain"],
    )


def toggle(label: str, key: str):
    """Casilla + selector de color en la misma fila (como Rellenar / Trazo / Fondo / Sombra de Premiere)."""
    c1, c2 = st.columns([4, 1])
    with c1:
        on = st.checkbox(label, key=f"{key}_on")
    with c2:
        st.color_picker(label, key=f"{key}_color", disabled=not on, label_visibility="collapsed")
    return on


def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    except Exception:
        pass


def show_result(key: str) -> None:
    r = ss.get(f"result_{key}")
    if not r or not Path(r["path"]).exists():
        return
    path = Path(r["path"])
    mb = path.stat().st_size / 1e6
    st.success(f"Guardado en: {path}")
    st.caption(f"{r['info']} · {mb:.1f} MB")
    if st.button("Abrir carpeta de salida", key=f"open_{key}"):
        open_folder(path.parent)
    if mb <= MAX_PREVIEW_MB:
        if path.suffix == ".mp4":
            st.video(str(path))
        else:
            st.info("Los .mov (ProRes/DNxHR) no se previsualizan en el navegador: ábrelo en Premiere.")
        st.download_button(f"Descargar {path.suffix[1:].upper()}", data=path.read_bytes(), file_name=path.name,
                           mime="video/mp4" if path.suffix == ".mp4" else "video/quicktime", key=f"dl_{key}")


# --------------------------------------------------------------------------- #
# 1 · Guion y voz
# --------------------------------------------------------------------------- #
st.title("Subtítulos estilo cartel")
head_l, head_r = st.columns([5, 1])
head_l.caption("Video sobre pantalla azul oscura (#00008B) para aplicar Ultra Key en Premiere Pro.")
head_r.button("Restablecer valores", on_click=reset_settings, use_container_width=True)

txt_file = st.file_uploader("Cargar guion (.txt)", type=["txt"], key="txt_file")
if txt_file is not None and ss.get("_txt_id") != txt_file.file_id:
    ss["_txt_id"] = txt_file.file_id
    ss["script"] = txt_file.getvalue().decode("utf-8", errors="ignore")
st.text_area("Texto de los subtítulos:", key="script", height=380, placeholder="Escribe o pega aquí el guion...")

v1, v2, v3 = st.columns([2, 1, 1])
v1.selectbox("Voz", OPTIONS["voice"], key="voice")
v2.slider("Velocidad (%)", -50, 50, step=5, key="speed")
v3.slider("Tono (Hz)", -50, 50, step=5, key="pitch")

tab_sub, tab_ls = st.tabs(["Subtítulos", "Lip-sync (PNGtuber)"])

# --------------------------------------------------------------------------- #
# 2 · Subtítulos
# --------------------------------------------------------------------------- #
with tab_sub:
    st.radio("Fuente del audio", OPTIONS["src_mode"], key="src_mode", horizontal=True)
    sub_audio = None
    if ss["src_mode"] == SRC_UPLOAD:
        sub_audio = st.file_uploader("Audio", type=["mp3", "wav", "m4a"], key="sub_audio")
        with st.expander("Detección de pausas (para estimar tiempos)"):
            p1, p2 = st.columns(2)
            p1.slider("Umbral de ruido (dB)", -50, -10, key="noise")
            p2.slider("Pausa mínima (s)", 0.10, 0.80, step=0.05, key="minsil")

    st.markdown("**Estilo y previsualización en tiempo real**")

    @st.fragment
    def style_editor() -> None:
        """Solo este bloque se vuelve a ejecutar al tocar un control: la página no se recalcula completa."""
        ctrl, prev = st.columns([3, 2], gap="large")
        with prev:
            st.markdown('<span class="prev-anchor"></span>', unsafe_allow_html=True)
            slot = st.container()

        with ctrl:
            fonts = font_options()
            if ss["font"] not in fonts:
                ss["font"] = fonts[0]
            st.selectbox("Fuente", fonts, key="font")
            if ss["font"] == OTHER_FONT:
                st.text_input("Nombre exacto de la fuente instalada", key="font_custom",
                              help="Debe estar instalada en el equipo o copiada en la carpeta /fonts.")
            t1, t2, t3, t4, t5 = st.columns([2, 1.3, 1.3, 1.5, 1.7])
            t1.number_input("Tamaño", 10.0, 400.0, key="size")
            with t2:
                st.checkbox("Negrita", key="bold")
            with t3:
                st.checkbox("Cursiva", key="italic")
            with t4:
                st.checkbox("Subrayado", key="underline")
            with t5:
                st.checkbox("MAYÚSCULAS", key="upper")
            if ss["poster"]:
                st.caption("Con «Estilo cartel» activo se ignoran: negrita, cursiva, mayúsculas, relleno, trazo, "
                           "fondo, sombra, escalas, tracking, interlineado y resaltado (los fija el cartel).")

            st.markdown("##### Rellenar · Trazo · Fondo · Sombra")
            toggle("Rellenar", "fill")
            stroke_on = toggle("Trazo (contorno exterior)", "stroke")
            st.number_input("Grosor del trazo (px)", 0.0, 30.0, step=0.5, key="stroke", disabled=not stroke_on)
            box_on = toggle("Fondo (marco sólido de color)", "box")
            b1, b2, b3 = st.columns([2, 1, 1])
            b1.slider("Opacidad del fondo (%)", 0, 100, key="box_op", disabled=not box_on)
            b2.number_input("Margen (px)", 0.0, 100.0, step=1.0, key="box_pad", disabled=not box_on)
            b3.number_input("Esquinas (px)", 0.0, 200.0, step=2.0, key="box_radius", disabled=not box_on)
            sh_on = toggle("Sombra", "shadow")
            s1, s2 = st.columns(2)
            s1.number_input("Distancia de la sombra (px)", 0.0, 60.0, step=1.0, key="shadow_dist", disabled=not sh_on)
            s2.slider("Opacidad de la sombra (%)", 0, 100, key="shadow_op", disabled=not sh_on)

            st.markdown("##### Bloques y animación")
            e1, e2 = st.columns(2)
            e1.slider("Palabras por bloque", 1, 4, key="words")
            e2.slider("Máx. caracteres por línea", 6, 24, key="chars")
            a1, a2 = st.columns([3, 1])
            a1.selectbox("Animación de entrada", OPTIONS["entry"], key="entry")
            a2.number_input("Duración (ms)", 60, 600, step=10, key="in_ms")
            x1, x2 = st.columns([3, 1])
            x1.selectbox("Animación de salida", OPTIONS["exit"], key="exit")
            x2.number_input("Duración (ms) ", 60, 600, step=10, key="out_ms")
            st.selectbox("Movimiento continuo", OPTIONS["motion"], key="motion")
            an1, an2 = st.columns(2)
            an1.checkbox("Animar letras alternas (1 s)", key="letters")
            an2.checkbox("Vaivén del recuadro cada 0,5 s (±2 px)", key="wobble")

            st.markdown("##### Mosaico (solo en el subtítulo, no en toda la pantalla)")
            mz1, mz2, mz3 = st.columns([2, 1, 1])
            mz1.selectbox("Efecto mosaico", OPTIONS["mosaic"], key="mosaic")
            off = ss["mosaic"] == "Sin mosaico"
            mz2.slider("Tamaño del mosaico (px)", 4, 120, key="mosaic_px", disabled=off)
            mz3.slider("Duración (ms)", 80, 1000, step=20, key="mosaic_ms", disabled=off or ss["mosaic"] == "Siempre")

            st.markdown("##### Estilo cartel (SADE · SUPER STAR · INTI-NET)")
            ps1, ps2, ps3 = st.columns(3)
            ps1.checkbox("Estilo cartel serif", key="poster")
            ps2.checkbox("Invertir colores", key="invert", disabled=not ss["poster"])
            ps3.checkbox("Granulado", key="grain")
            h1, h2 = st.columns([3, 1])
            h1.checkbox("Resaltar palabra activa", key="hl")
            h2.color_picker("Color del resaltado", key="hl_color", disabled=not ss["hl"], label_visibility="collapsed")
            with st.expander("Transformar (valores de Premiere Pro)"):
                q1, q2 = st.columns(2)
                q1.number_input("Escala horizontal (%)", 10.0, 600.0, key="sx")
                q2.number_input("Escala vertical (%)", 10.0, 600.0, key="sy")
                q1.number_input("Tracking", -500.0, 500.0, key="track")
                q2.number_input("Interlineado", -300.0, 300.0, key="lead")
                q1.number_input("Posición X (sobre 1080)", 0.0, 4000.0, key="px")
                q2.number_input("Posición Y (sobre 1920)", 0.0, 4000.0, key="py")

        style = style_from_state()
        style_json = json.dumps(asdict(style))
        words_text = " ".join(tokenize(ss["script"])[:40])
        with slot:
            st.markdown("##### Previsualización")
            n_blocks = preview_block_count(words_text, style)
            ss["prev_block"] = max(1, min(ss.get("prev_block", 1), n_blocks))
            block = (st.slider("Bloque de texto", 1, n_blocks, key="prev_block") if n_blocks > 1 else 1) - 1
            dur = block_duration_ms(words_text, style, block)
            ss["prev_t"] = max(0, min(ss.get("prev_t", 400), dur))
            t_ms = st.slider("Instante de la animación (ms)", 0, dur, step=20, key="prev_t",
                             help="Arrastra para ver cómo entra, se mueve y sale el subtítulo cuadro a cuadro.")
            pw, ph = target_dims(ss["res"], ss["aspect"])
            sc = 540 / max(pw, ph)
            pw, ph = int(pw * sc) // 2 * 2, int(ph * sc) // 2 * 2
            png, log = cached_preview(style_json, words_text, pw, ph, block, t_ms)
            if png:
                st.image(png, caption="Fondo azul oscuro #00008B")
            else:
                st.error("No se pudo generar la previsualización.")
                st.code(log[-1500:])
            for req, used in font_substitutions(log):
                st.warning(f"La fuente «{req}» no está instalada: se usa «{used}». Cópiala en la carpeta /fonts.")
            clip_key = (style_json, words_text, pw, ph)
            if st.button("▶ Reproducir animaciones", use_container_width=True, key="play_clip"):
                ss["clip_key"] = clip_key
            if ss.get("clip_key") == clip_key:
                clip, clog = cached_clip(style_json, words_text, pw * 3 // 4 // 2 * 2, ph * 3 // 4 // 2 * 2)
                if clip:
                    st.video(clip, format="video/mp4", loop=True, autoplay=True, muted=True)
                else:
                    st.error("No se pudo generar el clip.")
                    st.code(clog)
        save_settings()

    style_editor()

    st.markdown("**Formato, calidad y códec**")
    f1, f2, f3, f4 = st.columns(4)
    f1.selectbox("Resolución", OPTIONS["res"], key="res")
    f2.selectbox("Relación de aspecto", OPTIONS["aspect"], key="aspect")
    f3.selectbox("FPS", OPTIONS["fps"], key="fps")
    f4.selectbox(
        "Códec", OPTIONS["codec"], key="codec",
        help="H.264 pesa poco. ProRes/DNxHR conservan mejor los bordes para el chroma key pero generan archivos muy grandes.",
    )
    g1, g2 = st.columns(2)
    g1.selectbox("Calidad (solo H.264)", OPTIONS["crf"], key="crf", disabled=not CODECS[ss["codec"]]["uses_crf"])
    g2.checkbox("Incluir el audio en el video", key="inc_audio")

    if st.button("Generar video con subtítulos", use_container_width=True, key="gen_sub"):
        ss.pop("result_sub", None)
        text = ss["script"].strip()
        if not text:
            st.error("Escribe el guion arriba.")
        elif ss["src_mode"] == SRC_UPLOAD and sub_audio is None:
            st.error("Sube un audio o cambia la fuente del audio.")
        else:
            work = TEMP_DIR / f"job_{uuid.uuid4().hex[:8]}"
            work.mkdir(parents=True)
            bar = st.progress(0.0, text="Preparando…")
            try:
                audio_path = work / "audio.mp3"
                if ss["src_mode"] == SRC_TTS:
                    bar.progress(0.02, text="Generando voz y tiempos por palabra…")
                    rate, pitch = f"{ss['speed']:+d}%", f"{ss['pitch']:+d}Hz"
                    audio_bytes, wt, tmode = cached_tts(text, VOICES[ss["voice"]], rate, pitch)
                    audio_path.write_bytes(audio_bytes)
                else:
                    bar.progress(0.02, text="Analizando audio…")
                    audio_path = work / f"audio{Path(sub_audio.name).suffix}"
                    audio_path.write_bytes(sub_audio.getvalue())
                    wt, _ = cached_estimate(sub_audio.getvalue(), audio_path.suffix, text, ss["noise"], ss["minsil"])
                    tmode = "estimados"
                words = [Word(*w) for w in wt]
                if not words:
                    raise RuntimeError("No se obtuvieron palabras con tiempos. Revisa el texto y el audio.")
                total = get_duration(audio_path) + 0.4
                width, height = target_dims(ss["res"], ss["aspect"])
                codec = CODECS[ss["codec"]]
                final = unique_path(OUTPUT_DIR, name_from_text(text), codec["ext"])
                t0 = time.time()
                ok, err = render_captions(
                    words, style_from_state(), width, height, ss["fps"], ss["codec"], CRF_MAP[ss["crf"]],
                    FONTS_DIR, work, work / f"out.{codec['ext']}", audio_path if ss["inc_audio"] else None, total,
                    progress=lambda f, m: bar.progress(f, text=f"{m}… {int(f * 100)} %"),
                )
                if ok:
                    shutil.move(work / f"out.{codec['ext']}", final)
                    ss["result_sub"] = {
                        "path": str(final),
                        "info": f"{len(words)} palabras · tiempos {tmode} · {width}x{height} @ {ss['fps']} fps · "
                                f"renderizado en {time.time() - t0:.0f} s",
                    }
                else:
                    st.error("Falló el renderizado.")
                    st.code(err)
            except Exception as exc:
                st.error(f"Error: {exc}")
            finally:
                bar.empty()
                shutil.rmtree(work, ignore_errors=True)
    show_result("sub")

# --------------------------------------------------------------------------- #
# 3 · Lip-sync (PNGtuber)
# --------------------------------------------------------------------------- #
with tab_ls:
    st.caption("Alterna imágenes «hablando» y «silencio» según el audio. Sin audio subido, usa la voz del guion.")
    ls_audio = st.file_uploader("Audio (opcional)", type=["mp3", "wav", "m4a"], key="ls_audio")
    lc1, lc2 = st.columns(2)
    talking = lc1.file_uploader("Imágenes — Hablando", type=["png", "jpg", "jpeg"], accept_multiple_files=True, key="ls_talk")
    silent = lc2.file_uploader("Imágenes — Silencio", type=["png", "jpg", "jpeg"], accept_multiple_files=True, key="ls_sil")
    n1, n2 = st.columns(2)
    n1.slider("Umbral de ruido (dB)", -50, -10, key="ls_noise",
              help="Más alto: evita falsos positivos por ruido. Más bajo: detecta susurros.")
    n2.slider("Sensibilidad de detección (s)", 0.05, 0.50, step=0.01, key="ls_minsil",
              help="Bajo: cambio de imagen muy reactivo. Alto: evita parpadeos entre sílabas.")
    r1, r2, r3, r4 = st.columns(4)
    r1.selectbox("Resolución", OPTIONS["ls_res"], key="ls_res")
    r2.selectbox("Relación de aspecto", OPTIONS["ls_aspect"], key="ls_aspect")
    r3.selectbox("FPS", OPTIONS["ls_fps"], key="ls_fps")
    r4.selectbox("Calidad", OPTIONS["ls_crf"], key="ls_crf")

    if st.button("Generar video lip-sync", use_container_width=True, key="gen_ls"):
        ss.pop("result_ls", None)
        text = ss["script"].strip()
        if not (talking and silent) or not (ls_audio or text):
            st.error("Necesitas imágenes de «hablando» y «silencio», y un audio o un guion.")
        else:
            work = TEMP_DIR / f"job_{uuid.uuid4().hex[:8]}"
            work.mkdir(parents=True)
            bar = st.progress(0.0, text="Preparando…")
            try:
                if ls_audio:
                    audio_path = work / f"audio{Path(ls_audio.name).suffix}"
                    audio_path.write_bytes(ls_audio.getvalue())
                else:
                    audio_path = work / "audio.mp3"
                    bar.progress(0.02, text="Generando voz…")
                    audio_path.write_bytes(cached_tts(text, VOICES[ss["voice"]], f"{ss['speed']:+d}%", f"{ss['pitch']:+d}Hz")[0])

                def save_imgs(files, tag):
                    paths = []
                    for i, f in enumerate(files):
                        p = work / f"{tag}_src{i}{Path(f.name).suffix}"
                        p.write_bytes(f.getvalue())
                        paths.append(p)
                    return paths

                w, h = target_dims(ss["ls_res"], ss["ls_aspect"])
                final = unique_path(OUTPUT_DIR, name_from_text(text, fallback="lipsync") + "_lipsync", "mp4")
                ok, err, n_seg = make_lipsync(
                    audio_path, save_imgs(talking, "t"), save_imgs(silent, "s"), work / "out.mp4", work, w, h,
                    ss["ls_fps"], CRF_MAP[ss["ls_crf"]], ss["ls_noise"], ss["ls_minsil"],
                    progress=lambda f, m: bar.progress(f, text=f"{m}…"),
                )
                if ok:
                    shutil.move(work / "out.mp4", final)
                    ss["result_ls"] = {"path": str(final), "info": f"{n_seg} segmentos · {w}x{h} @ {ss['ls_fps']} fps"}
                else:
                    st.error("Falló el renderizado.")
                    st.code(err)
            except Exception as exc:
                st.error(f"Error: {exc}")
            finally:
                bar.empty()
                shutil.rmtree(work, ignore_errors=True)
    show_result("ls")

save_settings()
