"""Subtítulos estilo Hormozi sobre pantalla verde (chroma key) con FFmpeg + libass.

Flujo:
    texto (+ audio) -> tiempos por palabra -> bloques de 1-3 palabras -> archivo .ass
    -> FFmpeg dibuja el .ass sobre un fondo verde -> MP4 (H.264) o MOV (ProRes / DNxHR)

El estilo por defecto replica los valores del panel "Texto de origen" de Premiere Pro:
Georgia, itálica, tamaño 100, relleno blanco, trazo negro 4 px exterior, tracking -55,
interlineado -54, escala horizontal 135 %, escala vertical 308 %, posición (535,1 ; 800,4)
sobre un fotograma de referencia de 1080x1920.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import edge_tts

TICKS_PER_SECOND = 10_000_000  # edge-tts entrega offsets en unidades de 100 ns
REF_W, REF_H = 1080, 1920      # fotograma de referencia de las capturas de Premiere
CELL_RATIO = 1.136             # Georgia: (ascent+descent)/em. libass interpreta Fontsize como altura de celda
AUTO_LEADING = 1.2             # interlineado automático de Premiere (x tamaño); el valor de "leading" se suma a esto
CHROMA_GREEN = "0x00FF00"

EXIT_STYLES = {
    "Pop: crece y se desvanece": "pop",
    "Sube y se desvanece": "up",
    "Se encoge y se desvanece": "shrink",
    "Sin animación de salida": "none",
}

# Presets de códec. "pix" es el formato de píxel final.
CODECS: Dict[str, dict] = {
    "H.264 / MP4 (ligero, compatible con Premiere)": {
        "ext": "mp4",
        "vargs": ["-c:v", "libx264", "-profile:v", "high", "-preset", "medium"],
        "pix": "yuv420p",
        "audio": ["-c:a", "aac", "-b:a", "192k"],
        "extra": ["-movflags", "+faststart"],
        "uses_crf": True,
    },
    "ProRes 422 HQ / MOV (mejor para Ultra Key, pesado)": {
        "ext": "mov",
        "vargs": ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"],
        "pix": "yuv422p10le",
        "audio": ["-c:a", "pcm_s16le"],
        "extra": [],
        "uses_crf": False,
    },
    "DNxHR HQ / MOV (alternativa a ProRes)": {
        "ext": "mov",
        "vargs": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq"],
        "pix": "yuv422p",
        "audio": ["-c:a", "pcm_s16le"],
        "extra": [],
        "uses_crf": False,
    },
}


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class CaptionStyle:
    font_family: str = "Georgia"
    font_size: float = 100.0
    italic: bool = True
    bold: bool = False
    fill: str = "#FFFFFF"
    stroke: str = "#000000"
    stroke_px: float = 4.0
    scale_x: float = 135.0
    scale_y: float = 308.0
    tracking: float = -55.0      # milésimas de em (como Premiere)
    leading: float = -54.0       # px sobre el tamaño de fuente (como Premiere)
    pos_x: float = 535.1         # sobre 1080x1920
    pos_y: float = 800.4
    words_per_block: int = 2
    max_chars_line: int = 10
    uppercase: bool = False
    highlight: bool = True
    highlight_color: str = "#FFE600"
    exit_style: str = "pop"
    in_ms: int = 110
    out_ms: int = 160


# --------------------------------------------------------------------------- #
# Fuente
# --------------------------------------------------------------------------- #
def resolve_font(fonts_dir: Path) -> Tuple[str, Optional[str]]:
    """Devuelve (familia, aviso). Prioridad: Georgia > Gelasio (compatible) > serif del sistema."""
    if fonts_dir.exists():
        names = [p.name.lower() for p in fonts_dir.iterdir() if p.suffix.lower() in (".ttf", ".otf")]
        if any(n.startswith("georgia") for n in names):
            return "Georgia", None
        if any(n.startswith("gelasio") for n in names):
            return "Gelasio", None  # OFL, métricas compatibles con Georgia; viene incluida en /fonts
    if shutil.which("fc-list"):
        out = subprocess.run(["fc-list", ":family=Georgia"], capture_output=True, text=True).stdout
        if out.strip():
            return "Georgia", None
    return "DejaVu Serif", "Georgia no está instalada: se usa DejaVu Serif. Copia georgia.ttf en la carpeta /fonts (ver fonts/README.md)."


# --------------------------------------------------------------------------- #
# Tiempos por palabra
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> List[str]:
    return [t for t in re.sub(r"[{}\\]", "", text).split() if t]


def _weight(tok: str) -> float:
    w = len(re.sub(r"\W", "", tok)) + 1.0
    if re.search(r"[.!?…]$", tok):
        w += 3.0
    elif re.search(r"[,;:]$", tok):
        w += 1.5
    return w


def estimate_word_timings(tokens: Sequence[str], speech_segments: Sequence[Tuple[float, float]]) -> List[Word]:
    """Reparte las palabras sobre los tramos con voz, proporcional a su longitud.

    Se usa cuando el audio es subido (o si el servicio TTS no entrega tiempos por palabra).
    """
    segs = [(a, b) for a, b in speech_segments if b - a > 1e-3]
    if not tokens or not segs:
        return []
    total = sum(b - a for a, b in segs)
    weights = [_weight(t) for t in tokens]
    wsum = sum(weights)

    def at(t: float, is_end: bool) -> float:
        acc = 0.0
        for a, b in segs:
            d = b - a
            if (t <= acc + d + 1e-9) if is_end else (t < acc + d - 1e-9):
                return a + max(0.0, t - acc)
            acc += d
        return segs[-1][1]

    words, cum = [], 0.0
    for tok, w in zip(tokens, weights):
        s, e = cum / wsum * total, (cum + w) / wsum * total
        words.append(Word(tok, at(s, False), at(e, True)))
        cum += w
    return words


def speech_segments_from_intervals(intervals: Sequence[Tuple[float, float, str]]) -> List[Tuple[float, float]]:
    """Convierte la salida de build_intervals() de app.py en tramos con voz."""
    return [(a, b) for a, b, label in intervals if label == "talking"]


async def _tts_stream(text, voice, rate, pitch, boundary, out_path):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary=boundary)
    events = []
    with open(out_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("WordBoundary", "SentenceBoundary"):
                events.append(ch)
    return events


def synthesize_with_timings(text: str, voice: str, rate: str, pitch: str, out_path: Path) -> Tuple[List[Word], str]:
    """Genera el MP3 con edge-tts y devuelve (palabras con tiempos, modo).

    modo: "exacto" (WordBoundary) o "por frases" (SentenceBoundary + reparto dentro de cada frase).
    """
    tokens = tokenize(text)
    events = asyncio.run(_tts_stream(text, voice, rate, pitch, "WordBoundary", str(out_path)))
    words = [
        Word(e["text"], e["offset"] / TICKS_PER_SECOND, (e["offset"] + e["duration"]) / TICKS_PER_SECOND)
        for e in events if e["type"] == "WordBoundary"
    ]
    if words:
        if len(words) == len(tokens):  # conserva la puntuación original del texto
            for w, t in zip(words, tokens):
                w.text = t
        return words, "exacto"

    # Respaldo: el servicio no envió palabras; pedimos frases y repartimos dentro de cada una.
    events = asyncio.run(_tts_stream(text, voice, rate, pitch, "SentenceBoundary", str(out_path)))
    words = []
    for e in events:
        if e["type"] != "SentenceBoundary":
            continue
        s = e["offset"] / TICKS_PER_SECOND
        words += estimate_word_timings(tokenize(e["text"]), [(s, s + e["duration"] / TICKS_PER_SECOND)])
    return words, "por frases"


# --------------------------------------------------------------------------- #
# Bloques y ASS
# --------------------------------------------------------------------------- #
def make_blocks(words: Sequence[Word], per_block: int, uppercase: bool) -> List[List[Word]]:
    blocks, cur = [], []
    for w in words:
        txt = w.text.upper() if uppercase else w.text
        cur.append(Word(txt, w.start, w.end))
        if len(cur) >= per_block or re.search(r"[.!?…,;:]$", txt):
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def wrap_lines(block: Sequence[Word], max_chars: int) -> List[List[int]]:
    """Agrupa índices de palabras en líneas (el \\N se aplica por línea)."""
    lines: List[List[int]] = []
    cur: List[int] = []
    cur_len = 0
    for i, w in enumerate(block):
        add = len(w.text) + (1 if cur else 0)
        if cur and cur_len + add > max_chars:
            lines.append(cur)
            cur, cur_len = [], 0
            add = len(w.text)
        cur.append(i)
        cur_len += add
    if cur:
        lines.append(cur)
    return lines


def _bgr(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}&".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def build_ass(words: Sequence[Word], style: CaptionStyle, width: int, height: int, total_duration: float) -> str:
    k = min(width, height) / 1080.0                  # factor de tamaño respecto al diseño base
    sx, sy = style.scale_x, style.scale_y
    x0 = style.pos_x / REF_W * width
    y0 = style.pos_y / REF_H * height
    size_px = style.font_size * k
    spacing = style.tracking / 1000.0 * size_px      # px por letra; libass ya lo multiplica por la escala horizontal
    xb = style.stroke_px * k * sx / 100.0            # el trazo escala con la transformación, como en Premiere
    yb = style.stroke_px * k * sy / 100.0
    line_pitch = (AUTO_LEADING * size_px + style.leading * k) * sy / 100.0
    rise = 0.12 * height                             # recorrido de la salida "sube"

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Hormozi,{style.font_family},{_f(size_px * CELL_RATIO)},{_bgr(style.fill).replace('&H', '&H00', 1)},"
        f"&H000000FF,{_bgr(style.stroke).replace('&H', '&H00', 1)},&H00000000,"
        f"{-1 if style.bold else 0},{-1 if style.italic else 0},0,0,{_f(sx)},{_f(sy)},{_f(spacing)},0,1,"
        f"{_f(max(xb, yb))},0,5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    blocks = make_blocks(words, max(1, style.words_per_block), style.uppercase)
    events: List[str] = []
    for bi, blk in enumerate(blocks):
        b_start = blk[0].start
        nxt = blocks[bi + 1][0].start if bi + 1 < len(blocks) else None
        b_end = blk[-1].end + 0.20
        if nxt is not None:
            b_end = min(b_end, nxt)
        b_end = min(max(b_end, b_start + 0.25), total_duration)

        if style.highlight and len(blk) > 1:
            bounds = [w.start for w in blk]
            bounds[0] = b_start
            for i in range(1, len(bounds)):
                bounds[i] = max(bounds[i], bounds[i - 1] + 0.04)
            b_end = max(b_end, bounds[-1] + 0.08)
            bounds.append(b_end)
            states = list(range(len(blk)))
        elif style.highlight:
            bounds, states = [b_start, b_end], [0]
        else:
            bounds, states = [b_start, b_end], [None]

        lines = wrap_lines(blk, style.max_chars_line)
        n_states = len(states)
        for si, state in enumerate(states):
            ev_start, ev_end = bounds[si], bounds[si + 1]
            dur_ms = max(1, int(round((ev_end - ev_start) * 1000)))
            first, last = si == 0, si == n_states - 1

            anim = ""
            if first and style.in_ms > 0:
                a1 = min(style.in_ms, dur_ms)
                a2 = min(int(style.in_ms * 1.6), dur_ms)
                anim += (
                    f"\\fscx{_f(sx * 0.6)}\\fscy{_f(sy * 0.6)}\\alpha&HFF&"
                    f"\\t(0,{a1},\\fscx{_f(sx * 1.1)}\\fscy{_f(sy * 1.1)}\\alpha&H00&)"
                    f"\\t({a1},{a2},\\fscx{_f(sx)}\\fscy{_f(sy)})"
                )
            move_exit = last and style.exit_style == "up"
            if last and style.exit_style != "none" and style.out_ms > 0:
                t0 = max(0, dur_ms - style.out_ms)
                if style.exit_style == "pop":
                    anim += f"\\t({t0},{dur_ms},\\fscx{_f(sx * 1.25)}\\fscy{_f(sy * 1.25)}\\alpha&HFF&)"
                elif style.exit_style == "shrink":
                    anim += f"\\t({t0},{dur_ms},\\fscx{_f(sx * 0.3)}\\fscy{_f(sy * 0.3)}\\alpha&HFF&)"
                else:  # up
                    anim += f"\\t({t0},{dur_ms},\\alpha&HFF&)"
            else:
                move_exit = False

            for li, idxs in enumerate(lines):
                y = y0 + (li - (len(lines) - 1) / 2.0) * line_pitch
                if move_exit:
                    t0 = max(0, dur_ms - style.out_ms)
                    pos = f"\\move({_f(x0)},{_f(y)},{_f(x0)},{_f(y - rise)},{t0},{dur_ms})"
                else:
                    pos = f"\\pos({_f(x0)},{_f(y)})"
                parts = []
                for i in idxs:
                    txt = blk[i].text
                    if state is not None and i == state:
                        parts.append(f"{{\\1c{_bgr(style.highlight_color)}}}{txt}{{\\1c{_bgr(style.fill)}}}")
                    else:
                        parts.append(txt)
                body = " ".join(parts)
                tags = f"{{\\an5{pos}\\fsp{_f(spacing)}\\xbord{_f(xb)}\\ybord{_f(yb)}{anim}}}"
                events.append(f"Dialogue: 0,{_ts(ev_start)},{_ts(ev_end)},Hormozi,,0,0,0,,{tags}{body}")

    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def render_green_video(
    ass_name: str,
    audio_name: Optional[str],
    out_name: str,
    width: int,
    height: int,
    fps: int,
    duration: float,
    codec_label: str,
    crf: int,
    fonts_dir: Path,
    work_dir: Path,
):
    """Dibuja el .ass sobre fondo verde. Archivos relativos a work_dir (evita escapar rutas)."""
    c = CODECS[codec_label]
    vf = (
        # La fuente de color nace en RGB: la pasamos a YUV con matriz BT.709 rango TV
        # para que el verde sea consistente con las etiquetas de color del archivo.
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv444p,"
        "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,"
        f"ass={ass_name}:fontsdir='{fonts_dir.resolve().as_posix()}',"
        f"format={c['pix']}"
    )
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={CHROMA_GREEN}:s={width}x{height}:r={fps}:d={duration:.3f}"]
    if audio_name:
        cmd += ["-i", audio_name]
    cmd += ["-vf", vf, "-r", str(fps), "-fps_mode", "cfr"]
    cmd += c["vargs"]
    if c["uses_crf"]:
        cmd += ["-crf", str(crf)]
    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    if audio_name:
        cmd += ["-map", "0:v", "-map", "1:a"] + c["audio"]
    cmd += c["extra"] + ["-t", f"{duration:.3f}", out_name]
    return subprocess.run(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
