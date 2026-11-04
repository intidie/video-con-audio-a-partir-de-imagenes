"""Subtítulos estilo Hormozi sobre pantalla azul oscura (chroma key) con FFmpeg + libass.

Flujo:
    texto (+ audio) -> tiempos por palabra -> bloques de 1-3 palabras -> archivo .ass
    -> FFmpeg dibuja el .ass sobre un fondo azul oscuro -> MP4 (H.264) o MOV (ProRes / DNxHR)

El estilo por defecto replica los valores del panel "Texto de origen" de Premiere Pro:
Georgia, itálica, tamaño 100, relleno blanco, trazo negro 4 px exterior, tracking -55,
interlineado -54, escala horizontal 135 %, escala vertical 308 %, posición (535,1 ; 800,4)
sobre un fotograma de referencia de 1080x1920.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import edge_tts

TICKS_PER_SECOND = 10_000_000  # edge-tts entrega offsets en unidades de 100 ns
REF_W, REF_H = 1080, 1920      # fotograma de referencia de las capturas de Premiere
AUTO_LEADING = 1.2             # interlineado automático de Premiere (x tamaño); el valor de "leading" se suma a esto
CHROMA_BG = "0x00008B"  # azul oscuro (pantalla azul para chroma key)
GRAIN = "noise=alls=22:allf=t"  # granulado opcional (efecto cartel desgastado)

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
    # Fuente
    font_family: str = "Georgia"
    font_size: float = 100.0
    italic: bool = True
    bold: bool = False
    underline: bool = False
    uppercase: bool = False
    # Rellenar
    fill_on: bool = True
    fill: str = "#FFFFFF"
    # Trazo (contorno exterior)
    stroke_on: bool = True
    stroke: str = "#000000"
    stroke_px: float = 4.0
    # Fondo / marco sólido de color
    box_on: bool = False
    box_color: str = "#000000"
    box_opacity: float = 100.0   # %
    box_pad: float = 10.0        # px alrededor del texto
    box_radius: float = 0.0      # px, esquinas redondeadas
    # Sombra
    shadow_on: bool = False
    shadow_color: str = "#000000"
    shadow_dist: float = 6.0     # px
    shadow_opacity: float = 60.0  # %
    # Transformar
    scale_x: float = 135.0
    scale_y: float = 308.0
    tracking: float = -55.0      # milésimas de em (como Premiere)
    leading: float = -54.0       # px sobre el tamaño de fuente (como Premiere)
    pos_x: float = 535.1         # sobre 1080x1920
    pos_y: float = 800.4
    # Bloques, resaltado y animación
    words_per_block: int = 2
    max_chars_line: int = 10
    highlight: bool = True
    highlight_color: str = "#FFE600"
    exit_style: str = "pop"
    in_ms: int = 110
    out_ms: int = 160
    letter_anim: bool = True   # letras alternas de cada palabra cambian de tamaño durante 1 s
    wobble: bool = True        # recuadro completo: rotación y tamaño sutiles cada 0,5 s
    poster: bool = False       # estilo cartel (SADE / SUPER STAR / INTI-NET) en serif
    invert: bool = False       # invierte colores del estilo cartel (blanco sobre negro)
    grain: bool = False        # granulado


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


def _ass_col(hex_color: str, alpha: int = 0) -> str:
    """Color para la línea Style: &HAABBGGRR (alpha 255 = transparente)."""
    h = hex_color.lstrip("#")
    return f"&H{int(alpha):02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _alpha_tag(a: int) -> str:
    return f"&H{int(max(0, min(255, a))):02X}&"


def _opacity_to_alpha(opacity_pct: float) -> int:
    return int(round(255 * (1 - max(0.0, min(100.0, opacity_pct)) / 100.0)))


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _rounded_rect(w: float, h: float, r: float) -> str:
    """Rectángulo (con esquinas opcionalmente redondeadas) como dibujo vectorial ASS."""
    w, h = int(round(w)), int(round(h))
    r = int(round(max(0.0, min(r, w / 2.0, h / 2.0))))
    if r <= 0:
        return f"m 0 0 l {w} 0 {w} {h} 0 {h}"
    c = int(round(r * 0.5523))
    return (
        f"m {r} 0 l {w - r} 0 b {w - r + c} 0 {w} {r - c} {w} {r} l {w} {h - r} "
        f"b {w} {h - r + c} {w - r + c} {h} {w - r} {h} l {r} {h} "
        f"b {r - c} {h} 0 {h - r + c} 0 {h - r} l 0 {r} b 0 {r - c} {r - c} 0 {r} 0"
    )


def apply_poster(style: CaptionStyle) -> CaptionStyle:
    """Estilo cartel: serif negrita condensada y alta, recuadro sólido; invert = blanco sobre negro."""
    if not style.poster:
        return style
    ink, paper = ("#FFFFFF", "#000000") if style.invert else ("#000000", "#FFFFFF")
    return replace(
        style, italic=False, bold=True, uppercase=True, fill_on=True, fill=ink,
        stroke_on=True, stroke=ink, stroke_px=2.0, box_on=True, box_color=paper, box_opacity=100.0,
        box_pad=14.0, box_radius=0.0, shadow_on=False, scale_x=70.0, scale_y=170.0,
        tracking=-20.0, leading=-45.0, highlight=False,
    )


def build_ass(
    words: Sequence[Word], style: CaptionStyle, width: int, height: int, total_duration: float,
    fonts_dir: Optional[Path] = None,
) -> str:
    style = apply_poster(style)
    k = min(width, height) / 1080.0                  # factor de tamaño respecto al diseño base
    sx, sy = style.scale_x, style.scale_y
    x0 = style.pos_x / REF_W * width
    y0 = style.pos_y / REF_H * height
    size_px = style.font_size * k                    # tamaño en em, como Premiere
    fm = get_font_metrics(style.font_family, style.bold, style.italic, fonts_dir)
    spacing = style.tracking / 1000.0 * size_px      # px por letra; libass ya lo multiplica por la escala horizontal
    # Trazo, marco y sombra escalan con la transformación, como en Premiere.
    xb = style.stroke_px * k * sx / 100.0 if style.stroke_on else 0.0
    yb = style.stroke_px * k * sy / 100.0 if style.stroke_on else 0.0
    pad_x = style.box_pad * k * sx / 100.0
    pad_y = style.box_pad * k * sy / 100.0
    sh_x = style.shadow_dist * k * sx / 100.0
    sh_y = style.shadow_dist * k * sy / 100.0
    line_pitch = (AUTO_LEADING * size_px + style.leading * k) * sy / 100.0
    rise = 0.12 * height                             # recorrido de la salida "sube"

    a_fill = 0 if style.fill_on else 255
    a_shadow = _opacity_to_alpha(style.shadow_opacity) if style.shadow_on else 255
    a_box = _opacity_to_alpha(style.box_opacity)
    hide_t = "\\1a&HFF&\\3a&HFF&\\4a&HFF&"
    show_t = f"\\1a{_alpha_tag(a_fill)}\\3a&H00&\\4a{_alpha_tag(a_shadow)}"
    hide_b = "\\1a&HFF&"
    show_b = f"\\1a{_alpha_tag(a_box)}"

    # libass interpreta Fontsize como la altura de celda (winAscent+winDescent) de la fuente.
    flags = f"{-1 if style.bold else 0},{-1 if style.italic else 0},{-1 if style.underline else 0},0"
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Hormozi,{style.font_family},{_f(size_px * fm.cell)},{_ass_col(style.fill, a_fill)},&H000000FF,"
        f"{_ass_col(style.stroke, 0)},{_ass_col(style.shadow_color, a_shadow)},{flags},{_f(sx)},{_f(sy)},"
        f"{_f(spacing)},0,1,{_f(max(xb, yb))},0,5,0,0,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def anim(first: bool, last: bool, dur_ms: int, hide: str, show: str, bx: float, by: float) -> str:
        out = ""
        if first and style.in_ms > 0:
            a1 = min(style.in_ms, dur_ms)
            a2 = min(int(style.in_ms * 1.6), dur_ms)
            out += (
                f"\\fscx{_f(bx * 0.6)}\\fscy{_f(by * 0.6)}{hide}"
                f"\\t(0,{a1},\\fscx{_f(bx * 1.1)}\\fscy{_f(by * 1.1)}{show})"
                f"\\t({a1},{a2},\\fscx{_f(bx)}\\fscy{_f(by)})"
            )
        if last and style.exit_style != "none" and style.out_ms > 0:
            t0 = max(0, dur_ms - style.out_ms)
            if style.exit_style == "pop":
                out += f"\\t({t0},{dur_ms},\\fscx{_f(bx * 1.25)}\\fscy{_f(by * 1.25)}{hide})"
            elif style.exit_style == "shrink":
                out += f"\\t({t0},{dur_ms},\\fscx{_f(bx * 0.3)}\\fscy{_f(by * 0.3)}{hide})"
            else:  # up
                out += f"\\t({t0},{dur_ms},{hide})"
        return out

    def position(cx: float, cy: float, dur_ms: int, last: bool) -> str:
        if last and style.exit_style == "up" and style.out_ms > 0:
            t0 = max(0, dur_ms - style.out_ms)
            return f"\\move({_f(cx)},{_f(cy)},{_f(cx)},{_f(cy - rise)},{t0},{dur_ms})"
        return f"\\pos({_f(cx)},{_f(cy)})"

    fs_base = size_px * fm.cell
    fs_big = fs_base * 1.12                          # cambio de tamaño de letra (+12 %)
    step, ang = 0.5, 0.6                             # vaivén del recuadro: cada 0,5 s, +-0,6 grados y +2 px

    def pieces(a: float, b: float) -> List[Tuple[float, float]]:
        """Parte [a, b] en cortes de 0,5 s (rejilla global) para el vaivén del recuadro."""
        if not style.wobble:
            return [(a, b)]
        cuts, n = [a], int(a / step) + 1
        while n * step < b - 0.03:
            if n * step > a + 0.03:
                cuts.append(n * step)
            n += 1
        cuts.append(b)
        return list(zip(cuts[:-1], cuts[1:]))

    def letters(txt: str, t1: int) -> str:
        """Una letra de por medio de cada palabra crece y vuelve a su tamaño durante 1 s (inicia en t1 ms)."""
        if not style.letter_anim:
            return txt
        out, n = [], 0
        for ch in txt:
            if ch.isalpha():
                if n % 2 == 0:
                    out.append(
                        f"{{\\t({t1},{t1 + 500},\\fs{_f(fs_big)})\\t({t1 + 500},{t1 + 1000},\\fs{_f(fs_base)})}}"
                        f"{ch}{{\\fs{_f(fs_base)}}}"
                    )
                else:
                    out.append(ch)
                n += 1
            else:
                out.append(ch)
        return "".join(out)

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
        n_lines = len(lines)
        line_y = [y0 + (li - (n_lines - 1) / 2.0) * line_pitch for li in range(n_lines)]
        texts = [" ".join(blk[i].text for i in idxs) for idxs in lines]
        widths = [(text_width_em(fm, t) * size_px + len(t) * spacing) * sx / 100.0 for t in texts]
        ref_w = max(max(widths) + (2 * pad_x if style.box_on else 0.0), 20.0)
        ocy = sum(line_y) / n_lines                  # centro de giro común (recuadro + texto)
        if style.box_on:
            s_y = size_px * sy / 100.0
            base_first = line_y[0] + (fm.ascent - fm.cell / 2.0) * s_y
            base_last = line_y[-1] + (fm.ascent - fm.cell / 2.0) * s_y
            top = base_first - fm.cap_top * s_y - pad_y
            bottom = base_last + fm.desc_bottom * s_y + pad_y
            ocy = (top + bottom) / 2.0

        def wob(t: float, bx: float, by: float):
            if not style.wobble:
                return "", bx, by
            on = int(t / step + 1e-6) % 2
            f = (ref_w + 2.0) / ref_w if on else 1.0   # +2 px de ancho en el estado "on"
            bx, by = bx * f, by * f
            return f"\\org({_f(x0)},{_f(ocy)})\\frz{_f(ang if on else -ang)}\\fscx{_f(bx)}\\fscy{_f(by)}", bx, by

        # --- Fondo / marco sólido: un solo rectángulo ajustado a todo el bloque (capa 0)
        if style.box_on:
            bw = max(widths) + 2 * pad_x
            bh = bottom - top
            for pa, pb in pieces(b_start, b_end):
                bdur = max(1, int(round((pb - pa) * 1000)))
                b_first, b_last = pa <= b_start + 1e-6, pb >= b_end - 1e-6
                wt, wbx, wby = wob(pa, 100.0, 100.0)
                btags = (
                    f"{{\\an5{position(x0, (top + bottom) / 2.0, bdur, b_last)}\\bord0\\shad0\\p1"
                    f"\\1c{_bgr(style.box_color)}\\1a{_alpha_tag(a_box)}{wt}"
                    f"{anim(b_first, b_last, bdur, hide_b, show_b, wbx, wby)}}}"
                )
                events.append(
                    f"Dialogue: 0,{_ts(pa)},{_ts(pb)},Hormozi,,0,0,0,,{btags}"
                    f"{_rounded_rect(bw, bh, style.box_radius * k)}{{\\p0}}"
                )

        # --- Texto (capa 1), un evento por palabra activa (resaltado) y por tramo de 0,5 s (vaivén)
        n_states = len(states)
        for si, state in enumerate(states):
            ev_start, ev_end = bounds[si], bounds[si + 1]
            first, last = si == 0, si == n_states - 1
            shadow_tags = f"\\xshad{_f(sh_x)}\\yshad{_f(sh_y)}" if style.shadow_on else "\\shad0"
            for pa, pb in pieces(ev_start, ev_end):
                dur_ms = max(1, int(round((pb - pa) * 1000)))
                p_first = first and pa <= ev_start + 1e-6
                p_last = last and pb >= ev_end - 1e-6
                wt, wbx, wby = wob(pa, sx, sy)
                anim_t = anim(p_first, p_last, dur_ms, hide_t, show_t, wbx, wby)
                for li, idxs in enumerate(lines):
                    parts = []
                    for i in idxs:
                        txt = letters(blk[i].text, int(round((blk[i].start - pa) * 1000)))
                        if state is not None and i == state:
                            parts.append(f"{{\\1c{_bgr(style.highlight_color)}}}{txt}{{\\1c{_bgr(style.fill)}}}")
                        else:
                            parts.append(txt)
                    ttags = (
                        f"{{\\an5{position(x0, line_y[li], dur_ms, p_last)}\\fsp{_f(spacing)}"
                        f"\\xbord{_f(xb)}\\ybord{_f(yb)}{shadow_tags}{wt}{anim_t}}}"
                    )
                    events.append(f"Dialogue: 1,{_ts(pa)},{_ts(pb)},Hormozi,,0,0,0,,{ttags}{' '.join(parts)}")

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
    grain: bool = False,
):
    """Dibuja el .ass sobre fondo azul oscuro. Archivos relativos a work_dir (evita escapar rutas)."""
    c = CODECS[codec_label]
    vf = (
        # La fuente de color nace en RGB: la pasamos a YUV con matriz BT.709 rango TV
        # para que el fondo sea consistente con las etiquetas de color del archivo.
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv444p,"
        "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv,"
        f"ass={ass_name}:fontsdir='{fonts_dir.resolve().as_posix()}',"
        f"{GRAIN + ',' if grain else ''}format={c['pix']}"
    )
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={CHROMA_BG}:s={width}x{height}:r={fps}:d={duration:.3f}"]
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


# --------------------------------------------------------------------------- #
# Fuentes disponibles
# --------------------------------------------------------------------------- #
FONT_EXTS = (".ttf", ".otf", ".ttc")


def font_family_of(path: Path) -> str:
    """Nombre de familia (name ID 1) de un archivo de fuente; cae al nombre del archivo."""
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(str(path), fontNumber=0, lazy=True)
        name = f["name"].getDebugName(1)
        f.close()
        if name:
            return name.strip()
    except Exception:
        pass
    return re.split(r"[-_ ]", path.stem)[0]


def list_font_families(fonts_dir: Path) -> List[str]:
    if not fonts_dir.exists():
        return []
    fams = {font_family_of(p) for p in fonts_dir.iterdir() if p.suffix.lower() in FONT_EXTS}
    return sorted(fams, key=str.lower)


def system_font_families() -> List[str]:
    """Familias instaladas en el sistema (vía fontconfig). Vacío si no hay fc-list (p. ej. Windows)."""
    if not shutil.which("fc-list"):
        return []
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    fams = {line.split(",")[0].replace("\\", "").strip() for line in out.splitlines() if line.strip()}
    return sorted(fams, key=str.lower)


def prepare_fonts_dir(repo_fonts: Path, uploads: Sequence[Tuple[str, bytes]], dest_root: Path) -> Path:
    """Carpeta con las fuentes del repo + las que subió la persona (para pasarla a libass como fontsdir)."""
    if not uploads:
        return repo_fonts
    h = hashlib.sha1()
    for name, data in uploads:
        h.update(name.encode("utf-8"))
        h.update(hashlib.sha1(data).digest())
    dest = dest_root / f"fonts_{h.hexdigest()[:12]}"
    if not dest.exists():
        tmp = dest_root / f".{dest.name}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        if repo_fonts.exists():
            for p in repo_fonts.iterdir():
                if p.suffix.lower() in FONT_EXTS:
                    shutil.copy(p, tmp / p.name)
        for name, data in uploads:
            if Path(name).suffix.lower() in FONT_EXTS:
                (tmp / Path(name).name).write_bytes(data)
        tmp.rename(dest)
    return dest


# --------------------------------------------------------------------------- #
# Previsualización (mismo motor que el video final: libass vía FFmpeg)
# --------------------------------------------------------------------------- #
_FONTSELECT = re.compile(r"fontselect: \((.+?), (\d+), (\d+)\) -> (.+?), (-?\d+), (.+)")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def font_substitutions(ffmpeg_log: str) -> List[Tuple[str, str]]:
    """(pedida, usada) cuando libass no encontró la fuente y puso otra."""
    seen, out = set(), []
    for m in _FONTSELECT.finditer(ffmpeg_log):
        req, got = m.group(1), f"{m.group(4)} {m.group(6)}"
        if _norm(req) not in _norm(got) and (req, got) not in seen:
            seen.add((req, got))
            out.append((req, m.group(6).strip() or m.group(4).strip()))
    return out


def preview_block_count(text: str, style: CaptionStyle) -> int:
    toks = tokenize(text)[:40] or ["Texto", "de", "prueba"]
    words = [Word(t, i * 0.5, i * 0.5 + 0.5) for i, t in enumerate(toks)]
    return len(make_blocks(words, max(1, style.words_per_block), style.uppercase))


def render_preview_png(
    style: CaptionStyle, text: str, fonts_dir: Path, width: int, height: int, block_index: int = 0
) -> Tuple[Optional[bytes], str, int]:
    """Un fotograma PNG de un bloque de subtítulos sobre el fondo azul oscuro. Devuelve (png, log, n_bloques)."""
    toks = tokenize(text)[:40] or ["Texto", "de", "prueba"]
    words = [Word(t, i * 0.5, i * 0.5 + 0.5) for i, t in enumerate(toks)]
    total = len(words) * 0.5 + 1.0
    blocks = make_blocks(words, max(1, style.words_per_block), style.uppercase)
    bi = max(0, min(block_index, len(blocks) - 1))
    t = blocks[bi][0].start + 0.30  # entrada terminada, primera palabra resaltada
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "p.ass").write_text(build_ass(words, style, width, height, total, fonts_dir), encoding="utf-8")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "verbose", "-y",
            "-f", "lavfi", "-i", f"color=c={CHROMA_BG}:s={width}x{height}:r=20:d={t + 0.3:.2f}",
            "-vf", f"ass=p.ass:fontsdir='{fonts_dir.resolve().as_posix()}'" + (f",{GRAIN}" if style.grain else ""),
            "-ss", f"{t:.2f}", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
        ]
        r = subprocess.run(cmd, cwd=tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = r.stderr.decode("utf-8", "replace")
    return (r.stdout if r.returncode == 0 and r.stdout else None), log, len(blocks)


# --------------------------------------------------------------------------- #
# Métricas de fuente (para tamaño real en "em" y para dibujar el marco ajustado al texto)
# --------------------------------------------------------------------------- #
@dataclass
class FontMetrics:
    path: Optional[Path]
    ascent: float = 0.93      # em
    descent: float = 0.20     # em
    cap_top: float = 0.69     # em sobre la línea base (tope de la "H")
    desc_bottom: float = 0.21  # em bajo la línea base (fondo de la "g")

    @property
    def cell(self) -> float:
        return self.ascent + self.descent


DEFAULT_METRICS = FontMetrics(None, ascent=0.917, descent=0.219, cap_top=0.692, desc_bottom=0.217)  # ~Georgia
_font_cache: Dict[Tuple[str, bool, bool, str], FontMetrics] = {}


def _style_flags(path: Path) -> Tuple[str, bool, bool]:
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(str(path), fontNumber=0, lazy=True)
        fam = (f["name"].getDebugName(1) or "").strip()
        sub = (f["name"].getDebugName(2) or "").lower()
        f.close()
        return fam, "bold" in sub, ("italic" in sub or "oblique" in sub)
    except Exception:
        return "", False, False


def find_font_file(family: str, bold: bool, italic: bool, fonts_dir: Optional[Path]) -> Optional[Path]:
    best, best_score = None, -1
    if fonts_dir and fonts_dir.exists():
        for p in fonts_dir.iterdir():
            if p.suffix.lower() not in FONT_EXTS:
                continue
            fam, is_b, is_i = _style_flags(p)
            if fam.lower() != family.lower():
                continue
            score = 1 + (2 if is_b == bold else 0) + (1 if is_i == italic else 0)
            if score > best_score:
                best, best_score = p, score
    if best:
        return best
    if shutil.which("fc-match"):
        pat = family + (":bold" if bold else "") + (":italic" if italic else "")
        out = subprocess.run(["fc-match", "-f", "%{file}", pat], capture_output=True, text=True).stdout.strip()
        if out and Path(out).exists():
            return Path(out)
    return None


def get_font_metrics(family: str, bold: bool, italic: bool, fonts_dir: Optional[Path]) -> FontMetrics:
    key = (family, bold, italic, str(fonts_dir))
    if key in _font_cache:
        return _font_cache[key]
    m = DEFAULT_METRICS
    path = find_font_file(family, bold, italic, fonts_dir)
    if path:
        try:
            from PIL import ImageFont

            f = ImageFont.truetype(str(path), 1000)
            asc, desc = (v / 1000.0 for v in f.getmetrics())
            try:  # libass usa las métricas "win" de OS/2 para la celda y la línea base
                from fontTools.ttLib import TTFont

                tt = TTFont(str(path), fontNumber=0, lazy=True)
                upm = tt["head"].unitsPerEm
                os2 = tt["OS/2"]
                if os2.usWinAscent + os2.usWinDescent > 0:
                    asc, desc = os2.usWinAscent / upm, os2.usWinDescent / upm
                tt.close()
            except Exception:
                pass
            m = FontMetrics(
                path, asc, desc,
                -f.getbbox("Hbdfhklt", anchor="ls")[1] / 1000.0, f.getbbox("gjpqy", anchor="ls")[3] / 1000.0,
            )
        except Exception:
            m = DEFAULT_METRICS
    _font_cache[key] = m
    return m


def text_width_em(metrics: FontMetrics, text: str) -> float:
    """Ancho del texto en em (sin escala ni tracking)."""
    if metrics.path:
        try:
            from PIL import ImageFont

            return ImageFont.truetype(str(metrics.path), 1000).getlength(text) / 1000.0
        except Exception:
            pass
    return 0.55 * len(text)
