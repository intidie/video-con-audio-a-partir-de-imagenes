"""Motor de subtítulos estilo cartel sobre pantalla azul oscura (chroma key) con FFmpeg + libass.

Flujo:  texto (+ audio) -> tiempos por palabra -> bloques -> archivo .ass -> FFmpeg dibuja el .ass
        sobre un fondo azul oscuro -> MP4 (H.264) o MOV (ProRes / DNxHR).

Rendimiento: el video se parte en trozos que se renderizan en paralelo (un proceso FFmpeg por trozo,
con prioridad baja) y se unen sin recodificar. El mosaico se aplica solo a la franja del subtítulo.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import edge_tts

TICKS_PER_SECOND = 10_000_000  # edge-tts entrega offsets en unidades de 100 ns
REF_W, REF_H = 1080, 1920      # fotograma de referencia de las capturas de Premiere
AUTO_LEADING = 1.2             # interlineado automático de Premiere (x tamaño)
CHROMA_BG = "0x00008B"         # azul oscuro (pantalla azul para chroma key)
GRAIN = "noise@gr=alls=22:allf=t:enable=0"   # se enciende con sendcmd mientras hay texto
GRID = 0.5                     # rejilla (s) del vaivén / movimiento continuo

EXIT_STYLES = {
    "Sin animación de salida": "none",
    "Pop: crece y se desvanece": "pop",
    "Sube y se desvanece": "up",
    "Cae y se desvanece": "down",
    "Se encoge y se desvanece": "shrink",
    "Gira y se desvanece": "spin",
    "Solo se desvanece": "fade",
}
ENTRY_STYLES = {
    "Pop: crece": "pop",
    "Rebote elástico": "bounce",
    "Zoom desde grande": "zoom",
    "Gira al entrar": "spin",
    "Desliza desde abajo": "up",
    "Desliza desde la izquierda": "left",
    "Cae desde arriba": "drop",
    "Parpadeo": "flicker",
    "Letras en cascada": "cascade",
    "Sin animación de entrada": "none",
}
MOTION_STYLES = {"Sin movimiento continuo": "none", "Latido (pulso)": "pulse", "Flotar (sube y baja)": "float"}
MOSAIC_MODES = {
    "Sin mosaico": "off", "Al entrar": "in", "Al salir": "out", "Al entrar y salir": "inout", "Siempre": "always",
}

# "pix" = formato de píxel final; "chunk" = contenedor intermedio de cada trozo.
CODECS: Dict[str, dict] = {
    "H.264 / MP4 (ligero, compatible con Premiere)": {
        "ext": "mp4", "chunk": "mkv",
        "vargs": ["-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast"],
        "pix": "yuv420p", "audio": ["-c:a", "aac", "-b:a", "192k"],
        "extra": ["-movflags", "+faststart"], "uses_crf": True,
    },
    "ProRes 422 HQ / MOV (mejor para Ultra Key, pesado)": {
        "ext": "mov", "chunk": "mov",
        "vargs": ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"],
        "pix": "yuv422p10le", "audio": ["-c:a", "pcm_s16le"], "extra": [], "uses_crf": False,
    },
    "DNxHR HQ / MOV (alternativa a ProRes)": {
        "ext": "mov", "chunk": "mov",
        "vargs": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq"],
        "pix": "yuv422p", "audio": ["-c:a", "pcm_s16le"], "extra": [], "uses_crf": False,
    },
}


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class CaptionStyle:
    # Fuente (valores por defecto = capturas de la app)
    font_family: str = "Bebas Neue"
    font_size: float = 150.0
    italic: bool = True
    bold: bool = False
    underline: bool = False
    uppercase: bool = False
    fill_on: bool = True
    fill: str = "#FFFFFF"
    stroke_on: bool = True
    stroke: str = "#000000"
    stroke_px: float = 4.0
    box_on: bool = False
    box_color: str = "#000000"
    box_opacity: float = 100.0
    box_pad: float = 10.0
    box_radius: float = 0.0
    shadow_on: bool = False
    shadow_color: str = "#000000"
    shadow_dist: float = 6.0
    shadow_opacity: float = 60.0
    # Transformar (valores de Premiere Pro)
    scale_x: float = 135.0
    scale_y: float = 308.0
    tracking: float = -55.0
    leading: float = -54.0
    pos_x: float = 535.1
    pos_y: float = 500.0
    # Bloques y resaltado
    words_per_block: int = 1
    max_chars_line: int = 10
    highlight: bool = True
    highlight_color: str = "#FFE600"
    # Animación
    entry_style: str = "pop"
    exit_style: str = "none"
    motion: str = "none"
    in_ms: int = 110
    out_ms: int = 160
    letter_anim: bool = True
    wobble: bool = True
    # Mosaico (solo sobre la franja del subtítulo)
    mosaic: str = "off"
    mosaic_px: float = 36.0
    mosaic_ms: int = 300
    # Estilo cartel
    poster: bool = True
    invert: bool = False
    grain: bool = False


@dataclass
class Captions:
    ass: str
    spans: List[Tuple[float, float]]   # (inicio, fin) de cada bloque
    band: Tuple[int, int]              # (y, alto) de la franja que contiene el subtítulo
    cmds: str                          # comandos sendcmd del mosaico ("" si no hay)


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
    """Reparte las palabras sobre los tramos con voz, proporcional a su longitud."""
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
    """Genera el MP3 con edge-tts y devuelve (palabras con tiempos, modo "exacto" | "por frases")."""
    tokens = tokenize(text)
    events = asyncio.run(_tts_stream(text, voice, rate, pitch, "WordBoundary", str(out_path)))
    words = [
        Word(e["text"], e["offset"] / TICKS_PER_SECOND, (e["offset"] + e["duration"]) / TICKS_PER_SECOND)
        for e in events if e["type"] == "WordBoundary"
    ]
    if words:
        if len(words) == len(tokens):  # conserva la puntuación original
            for w, t in zip(words, tokens):
                w.text = t
        return words, "exacto"
    events = asyncio.run(_tts_stream(text, voice, rate, pitch, "SentenceBoundary", str(out_path)))
    words = []
    for e in events:
        if e["type"] == "SentenceBoundary":
            s = e["offset"] / TICKS_PER_SECOND
            words += estimate_word_timings(tokenize(e["text"]), [(s, s + e["duration"] / TICKS_PER_SECOND)])
    return words, "por frases"


# --------------------------------------------------------------------------- #
# Bloques y ASS
# --------------------------------------------------------------------------- #
def make_blocks(words: Sequence[Word], per_block: int, uppercase: bool) -> List[List[Word]]:
    blocks, cur = [], []
    for w in words:
        cur.append(Word(w.text.upper() if uppercase else w.text, w.start, w.end))
        if len(cur) >= per_block or re.search(r"[.!?…,;:]$", cur[-1].text):
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def wrap_lines(block: Sequence[Word], max_chars: int) -> List[List[int]]:
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
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ass_col(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    return f"&H{int(alpha):02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _alpha_tag(a: int) -> str:
    return f"&H{int(max(0, min(255, a))):02X}&"


def _opacity_to_alpha(opacity_pct: float) -> int:
    return int(round(255 * (1 - max(0.0, min(100.0, opacity_pct)) / 100.0)))


def _ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _rounded_rect(w: float, h: float, r: float) -> str:
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
    """Estilo cartel: negrita condensada y alta, recuadro sólido; invert = blanco sobre negro."""
    if not style.poster:
        return style
    ink, paper = ("#FFFFFF", "#000000") if style.invert else ("#000000", "#FFFFFF")
    return replace(
        style, italic=False, bold=True, uppercase=True, fill_on=True, fill=ink,
        stroke_on=True, stroke=ink, stroke_px=2.0, box_on=True, box_color=paper, box_opacity=100.0,
        box_pad=14.0, box_radius=0.0, shadow_on=False, scale_x=70.0, scale_y=170.0,
        tracking=-20.0, leading=-45.0, highlight=False,
    )


def effect_commands(spans: Sequence[Tuple[float, float]], style: CaptionStyle, k: float) -> str:
    """Archivo sendcmd para la franja del subtítulo: pasos del mosaico (pixelize) y encendido del granulado."""
    lines: List[Tuple[float, str]] = []
    if style.mosaic in ("in", "out", "inout"):
        px, dur, steps = max(2.0, style.mosaic_px * k), style.mosaic_ms / 1000.0, (1.0, 0.6, 0.35, 0.18, 0.07)
        pts: Dict[float, float] = {}

        def put(t: float, size: float) -> None:
            t = round(t, 3)
            pts[t] = max(pts.get(t, 1.0), size)  # si coinciden salida y entrada, gana el bloque grande

        for a, b in spans:
            d = min(dur, (b - a) * (0.5 if style.mosaic == "inout" else 0.9))
            n = len(steps)
            if style.mosaic in ("in", "inout"):
                for i, f in enumerate(steps):
                    put(a + d * i / n, px * f)
                put(a + d, 1.0)
            if style.mosaic in ("out", "inout"):
                for i, f in enumerate(reversed(steps)):
                    put(b - d + d * i / n, px * f)
                put(b, 1.0)
        for t, v in pts.items():
            s = int(min(1024, max(1, round(v))))
            lines += [(t, f"pixelize@mz w {s}"), (t, f"pixelize@mz h {s}")]
    if style.grain:  # el ruido solo se aplica mientras hay texto en pantalla (y solo en la franja)
        runs: List[List[float]] = []
        for a, b in spans:
            if runs and a - runs[-1][1] < 0.05:
                runs[-1][1] = b
            else:
                runs.append([a, b])
        for a, b in runs:
            lines += [(round(a, 3), "noise@gr enable 1"), (round(b, 3), "noise@gr enable 0")]
    lines.sort(key=lambda x: x[0])
    return "".join(f"{t:.3f} {c};\n" for t, c in lines)


def build_ass(
    words: Sequence[Word], style: CaptionStyle, width: int, height: int, total_duration: float,
    fonts_dir: Optional[Path] = None,
) -> Captions:
    style = apply_poster(style)
    k = min(width, height) / 1080.0
    sx, sy = style.scale_x, style.scale_y
    x0 = style.pos_x / REF_W * width
    y0 = style.pos_y / REF_H * height
    size_px = style.font_size * k
    fm = get_font_metrics(style.font_family, style.bold, style.italic, fonts_dir)
    spacing = style.tracking / 1000.0 * size_px
    xb = style.stroke_px * k * sx / 100.0 if style.stroke_on else 0.0
    yb = style.stroke_px * k * sy / 100.0 if style.stroke_on else 0.0
    pad_x, pad_y = style.box_pad * k * sx / 100.0, style.box_pad * k * sy / 100.0
    sh_x, sh_y = style.shadow_dist * k * sx / 100.0, style.shadow_dist * k * sy / 100.0
    line_pitch = (AUTO_LEADING * size_px + style.leading * k) * sy / 100.0
    rise = 0.12 * height

    a_fill = 0 if style.fill_on else 255
    a_shadow = _opacity_to_alpha(style.shadow_opacity) if style.shadow_on else 255
    a_box = _opacity_to_alpha(style.box_opacity)
    hide_t = "\\1a&HFF&\\3a&HFF&\\4a&HFF&"
    show_t = f"\\1a{_alpha_tag(a_fill)}\\3a&H00&\\4a{_alpha_tag(a_shadow)}"
    hide_b, show_b = "\\1a&HFF&", f"\\1a{_alpha_tag(a_box)}"

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

    es, xs, mo = style.entry_style, style.exit_style, style.motion
    D = max(40, int(style.in_ms))
    W_IN = int(D * 1.6)                       # ventana total de la entrada (ms)
    OUT = max(40, int(style.out_ms))
    grid_on = style.wobble or mo != "none"
    need_org = style.wobble or "spin" in (es, xs)
    float_amp = 0.006 * height

    def fx(inp: bool, outp: bool, cell: int, dur: int, bx: float, by: float, rot: float,
           cx: float, cy: float, hide: str, show: str) -> Tuple[str, str]:
        """(etiqueta de posición, etiquetas de animación) de un tramo de evento."""
        S = lambda f: f"\\fscx{_f(bx * f)}\\fscy{_f(by * f)}"
        tg, mv = "", None
        if inp and es != "none":
            w = max(2, min(W_IN, dur))
            a = min(D, w)
            if es == "pop":
                tg += S(.6) + hide + f"\\t(0,{a},{S(1.1)}{show})\\t({a},{w},{S(1)})"
            elif es == "bounce":
                q1, q2 = int(w * .4), int(w * .7)
                tg += S(.3) + hide + f"\\t(0,{q1},{S(1.25)}{show})\\t({q1},{q2},{S(.9)})\\t({q2},{w},{S(1)})"
            elif es == "zoom":
                tg += S(2.2) + hide + f"\\t(0,{w},{S(1)}{show})"
            elif es == "spin":
                tg += f"\\frz{_f(rot - 40)}" + S(.5) + hide + f"\\t(0,{w},\\frz{_f(rot)}{S(1)}{show})"
            elif es == "flicker":
                q = [int(w * i / 5) for i in range(6)]
                tg += hide + "".join(f"\\t({q[i]},{q[i + 1]},{show if i % 2 == 0 else hide})" for i in range(5))
            elif es in ("up", "left", "drop"):
                ox, oy = {"up": (0, 0.10 * height), "left": (-0.30 * width, 0), "drop": (0, -0.12 * height)}[es]
                tg += hide + f"\\t(0,{int(w * .6)},{show})"
                if not outp:
                    mv = (cx + ox, cy + oy, cx, cy, 0, w)
        elif not inp and not outp:
            if mo == "pulse":
                tg += f"\\t(0,{dur // 2},{S(1.05)})\\t({dur // 2},{dur},{S(1)})"
            elif mo == "float":
                a1, a2 = (cy + float_amp, cy - float_amp) if cell % 2 == 0 else (cy - float_amp, cy + float_amp)
                mv = (cx, a1, cx, a2, 0, dur)
        if outp and xs != "none":
            t0 = max(0, dur - OUT)
            if xs == "pop":
                tg += f"\\t({t0},{dur},{S(1.25)}{hide})"
            elif xs == "shrink":
                tg += f"\\t({t0},{dur},{S(.3)}{hide})"
            elif xs == "spin":
                tg += f"\\t({t0},{dur},\\frz{_f(rot + 45)}{S(.6)}{hide})"
            else:  # fade / up / down
                tg += f"\\t({t0},{dur},{hide})"
                if xs in ("up", "down") and mv is None:
                    mv = (cx, cy, cx, cy - rise if xs == "up" else cy + rise, t0, dur)
        pos = f"\\move({','.join(_f(v) if i < 4 else str(int(v)) for i, v in enumerate(mv))})" if mv \
            else f"\\pos({_f(cx)},{_f(cy)})"
        return pos, tg

    fs_base = size_px * fm.cell
    fs_big = fs_base * 1.12
    ang = 0.6

    def pieces(a: float, b: float, first: bool, last: bool) -> List[Tuple[float, float]]:
        """Parte [a, b] en tramos: entrada, salida y rejilla de 0,5 s (vaivén / movimiento continuo)."""
        e_in = a + W_IN / 1000.0 if first and es != "none" else a
        e_out = b - OUT / 1000.0 if last and xs != "none" else b
        cuts = {a, b}
        if grid_on:
            n = int(a / GRID) + 1
            while n * GRID < b - 0.03:
                t = n * GRID
                if t > a + 0.03 and not (a < t < e_in) and not (e_out < t < b):
                    cuts.add(round(t, 4))
                n += 1
        if e_in > a and e_in < b - 0.03:
            cuts.add(e_in)
        if e_out < b and e_out > a + 0.03:
            cuts.add(e_out)
        cs = sorted(cuts)
        clean = [cs[0]]
        for t in cs[1:-1]:
            if t - clean[-1] >= 0.03 and cs[-1] - t >= 0.03:
                clean.append(t)
        clean.append(cs[-1])
        return list(zip(clean[:-1], clean[1:]))

    def letters(txt: str, t1: int, casc: int) -> str:
        """Letras alternas que crecen (1 s) y/o aparición en cascada durante la entrada."""
        if not style.letter_anim and not casc:
            return txt
        out, n, idx = [], 0, 0
        n_vis = max(1, sum(1 for c in txt if not c.isspace()))
        for ch in txt:
            tg = ""
            if casc and not ch.isspace():
                ti = int(casc * 0.5 * idx / n_vis)
                tg += f"{hide_t}\\t({ti},{ti + 60},{show_t})"
                idx += 1
            grow = style.letter_anim and ch.isalpha() and n % 2 == 0
            if ch.isalpha():
                n += 1
            if grow:
                tg += f"\\t({t1},{t1 + 500},\\fs{_f(fs_big)})\\t({t1 + 500},{t1 + 1000},\\fs{_f(fs_base)})"
            out.append(f"{{{tg}}}{ch}" + (f"{{\\fs{_f(fs_base)}}}" if grow else "") if tg else ch)
        return "".join(out)

    blocks = make_blocks(words, max(1, style.words_per_block), style.uppercase)
    events: List[str] = []
    spans: List[Tuple[float, float]] = []
    y_min, y_max = float("inf"), float("-inf")
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
        spans.append((b_start, b_end))

        lines = wrap_lines(blk, style.max_chars_line)
        n_lines = len(lines)
        line_y = [y0 + (li - (n_lines - 1) / 2.0) * line_pitch for li in range(n_lines)]
        texts = [" ".join(blk[i].text for i in idxs) for idxs in lines]
        widths = [(text_width_em(fm, t) * size_px + len(t) * spacing) * sx / 100.0 for t in texts]
        ref_w = max(max(widths) + (2 * pad_x if style.box_on else 0.0), 20.0)
        half = size_px * fm.cell * sy / 200.0 + yb
        top, bottom = line_y[0] - half, line_y[-1] + half
        ocy = sum(line_y) / n_lines
        if style.box_on:
            s_y = size_px * sy / 100.0
            top = line_y[0] + (fm.ascent - fm.cell / 2.0) * s_y - fm.cap_top * s_y - pad_y
            bottom = line_y[-1] + (fm.ascent - fm.cell / 2.0) * s_y + fm.desc_bottom * s_y + pad_y
            ocy = (top + bottom) / 2.0
        # Margen de la franja: rebote/zoom (x1,3), giro (ancho/2 * sin) y recorrido vertical solo si el texto se desplaza
        moves_v = es in ("up", "drop") or xs in ("up", "down")
        marg = 0.35 * (bottom - top) + (0.45 * ref_w if "spin" in (es, xs) else 0.0) \
            + (0.14 if moves_v else 0.02) * height
        y_min, y_max = min(y_min, top - marg), max(y_max, bottom + marg)

        def wob(t: float, bx: float, by: float):
            org = f"\\org({_f(x0)},{_f(ocy)})" if need_org else ""
            if not style.wobble:
                return org, bx, by, 0.0
            on = int(t / GRID + 1e-6) % 2
            f = (ref_w + 2.0) / ref_w if on else 1.0
            bx, by, r = bx * f, by * f, (ang if on else -ang)
            return f"{org}\\frz{_f(r)}\\fscx{_f(bx)}\\fscy{_f(by)}", bx, by, r

        # --- Fondo / marco sólido (capa 0)
        if style.box_on:
            bw, bh = max(widths) + 2 * pad_x, bottom - top
            for pa, pb in pieces(b_start, b_end, True, True):
                dur = max(1, int(round((pb - pa) * 1000)))
                wt, wbx, wby, r = wob(pa, 100.0, 100.0)
                inp = pa < b_start + W_IN / 1000.0 - 1e-6 and es != "none"
                outp = pb > b_end - OUT / 1000.0 + 1e-6 and xs != "none"
                pos, atg = fx(inp, outp, int(pa / GRID), dur, wbx, wby, r, x0, (top + bottom) / 2.0, hide_b, show_b)
                events.append(
                    f"Dialogue: 0,{_ts(pa)},{_ts(pb)},Hormozi,,0,0,0,,{{\\an5{pos}\\bord0\\shad0\\p1"
                    f"\\1c{_bgr(style.box_color)}\\1a{_alpha_tag(a_box)}{wt}{atg}}}"
                    f"{_rounded_rect(bw, bh, style.box_radius * k)}{{\\p0}}"
                )

        # --- Texto (capa 1): un evento por palabra activa y por tramo
        n_states = len(states)
        shadow_tags = f"\\xshad{_f(sh_x)}\\yshad{_f(sh_y)}" if style.shadow_on else "\\shad0"
        for si, state in enumerate(states):
            ev_start, ev_end = bounds[si], bounds[si + 1]
            first, last = si == 0, si == n_states - 1
            for pa, pb in pieces(ev_start, ev_end, first, last):
                dur = max(1, int(round((pb - pa) * 1000)))
                inp = first and es != "none" and pa < ev_start + W_IN / 1000.0 - 1e-6
                outp = last and xs != "none" and pb > ev_end - OUT / 1000.0 + 1e-6
                wt, wbx, wby, r = wob(pa, sx, sy)
                casc = min(W_IN, dur) if (inp and es == "cascade") else 0
                for li, idxs in enumerate(lines):
                    pos, atg = fx(inp, outp, int(pa / GRID), dur, wbx, wby, r, x0, line_y[li], hide_t, show_t)
                    parts = []
                    for i in idxs:
                        txt = letters(blk[i].text, int(round((blk[i].start - pa) * 1000)), casc)
                        if state is not None and i == state:
                            parts.append(f"{{\\1c{_bgr(style.highlight_color)}}}{txt}{{\\1c{_bgr(style.fill)}}}")
                        else:
                            parts.append(txt)
                    ttags = (
                        f"{{\\an5{pos}\\fsp{_f(spacing)}\\xbord{_f(xb)}\\ybord{_f(yb)}{shadow_tags}{wt}{atg}}}"
                    )
                    events.append(f"Dialogue: 1,{_ts(pa)},{_ts(pb)},Hormozi,,0,0,0,,{ttags}{' '.join(parts)}")

    if y_min > y_max:
        y_min, y_max = 0.0, float(height)
    by0 = max(0, int(y_min)) // 2 * 2
    by1 = min(height, int(math.ceil(y_max)))
    bh = max(2, (by1 - by0) // 2 * 2)
    return Captions(header + "\n".join(events) + "\n", spans, (by0, bh), effect_commands(spans, style, k))


# --------------------------------------------------------------------------- #
# Filtros y render (paralelo por trozos)
# --------------------------------------------------------------------------- #
def make_vf(width: int, height: int, pix: str, fonts_dir: Path, cap: Captions, style: CaptionStyle, t0: float = 0.0) -> str:
    """Cadena de filtros: .ass -> (mosaico en la franja del subtítulo) -> (granulado) -> formato final."""
    work = "yuv420p" if pix == "yuv420p" else "yuv444p"
    vf = ",".join(
        ([f"setpts=PTS+{t0:.6f}/TB"] if t0 else [])
        + [
            "scale=out_color_matrix=bt709:out_range=tv", f"format={work}",
            "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv",
            f"ass=c.ass:fontsdir='{fonts_dir.resolve().as_posix()}'",
        ]
    )
    # Mosaico y granulado se aplican solo a la franja que contiene el subtítulo (el resto de la pantalla no se toca).
    by, bh = cap.band
    chain = [f"crop={width}:{bh}:0:{by}"] + (["sendcmd=f=m.cmd"] if cap.cmds else [])
    if style.mosaic == "always":
        s = int(min(1024, max(2, round(style.mosaic_px * min(width, height) / 1080.0))))
        chain.append(f"pixelize=w={s}:h={s}:mode=avg")
    elif style.mosaic != "off":
        chain.append("pixelize@mz=w=1:h=1:mode=avg")
    if style.mosaic != "off" or style.grain:
        band = f"[b]{','.join(chain)}"
        if style.grain:
            # El ruido solo toca los píxeles del texto (máscara = todo lo que no es el azul de fondo):
            # el fondo queda limpio para el Ultra Key y el archivo no se dispara de tamaño.
            band += (f"[bb];[bb]split=3[c1][c2][c3];[c2]{GRAIN}[n];"
                     f"[c3]colorkey=color={CHROMA_BG}:similarity=0.03:blend=0,format=yuva{work[3:]},alphaextract[k];"
                     "[n][k]alphamerge[g];[c1][g]overlay")
        vf += f",split[a][b];{band}[m];[a][m]overlay=0:{by}:format=auto"
    return vf + f",format={pix}" + (",setpts=PTS-STARTPTS" if t0 else "")


def write_assets(words, style, width, height, total, fonts_dir, work_dir: Path) -> Captions:
    cap = build_ass(words, style, width, height, total, fonts_dir)
    (work_dir / "c.ass").write_text(cap.ass, encoding="utf-8")
    if cap.cmds:
        (work_dir / "m.cmd").write_text(cap.cmds, encoding="utf-8")
    return cap


def _popen(cmd: List[str], cwd: Path, err_path: Path) -> subprocess.Popen:
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00004000 | 0x08000000      # prioridad baja + sin ventana
    else:
        kw["preexec_fn"] = lambda: os.nice(5)
    return subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=open(err_path, "w"), **kw)


def _frames_done(p: Path) -> int:
    try:
        m = re.findall(r"frame=(\d+)", p.read_text())
        return int(m[-1]) if m else 0
    except OSError:
        return 0


def default_workers() -> int:
    return max(1, min(os.cpu_count() or 2, 6))


def render_captions(
    words: Sequence[Word], style: CaptionStyle, width: int, height: int, fps: int, codec_label: str, crf: int,
    fonts_dir: Path, work_dir: Path, out_path: Path, audio: Optional[Path], total: float,
    workers: Optional[int] = None, progress: Optional[Callable[[float, str], None]] = None,
) -> Tuple[bool, str]:
    """Renderiza el video por trozos en paralelo y los une sin recodificar. Devuelve (ok, log de error)."""
    c = CODECS[codec_label]
    cap = write_assets(words, style, width, height, total, fonts_dir, work_dir)
    frames = math.ceil(total * fps)
    n = max(1, min(workers or default_workers(), int(total // 3)))   # trozos de al menos ~3 s
    bounds = [round(frames * i / n) for i in range(n + 1)]
    threads = max(2, (os.cpu_count() or 2) // n)
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    procs = []
    for i in range(n):
        s, e = bounds[i], bounds[i + 1]
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", f"p{i}.txt",
            "-f", "lavfi", "-i", f"color=c={CHROMA_BG}:s={width}x{height}:r={fps}",
            "-vf", make_vf(width, height, c["pix"], fonts_dir, cap, style, s / fps),
            "-frames:v", str(e - s), *c["vargs"], *(["-crf", str(crf)] if c["uses_crf"] else []),
            "-threads", str(threads), *tags, "-an", f"c{i}.{c['chunk']}",
        ]
        procs.append(_popen(cmd, work_dir, work_dir / f"e{i}.log"))
    while any(p.poll() is None for p in procs):
        if progress:
            progress(min(0.97, sum(_frames_done(work_dir / f"p{i}.txt") for i in range(n)) / frames), "Renderizando")
        time.sleep(0.4)
    for i, p in enumerate(procs):
        if p.returncode != 0:
            return False, (work_dir / f"e{i}.log").read_text(errors="replace")[-3000:]

    (work_dir / "l.txt").write_text("".join(f"file 'c{i}.{c['chunk']}'\n" for i in range(n)))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "l.txt"]
    if audio:
        cmd += ["-i", str(audio)]
    cmd += ["-map", "0:v"] + (["-map", "1:a", *c["audio"]] if audio else []) + ["-c:v", "copy", *c["extra"]]
    cmd += ["-t", f"{total:.3f}", str(out_path)]
    r = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if progress:
        progress(1.0, "Listo")
    ok = r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0
    return ok, r.stderr[-3000:]


# --------------------------------------------------------------------------- #
# Previsualización (mismo motor que el video final)
# --------------------------------------------------------------------------- #
PREVIEW_WORD = 0.8   # segundos por palabra en la vista previa (deja ver entradas, salidas y mosaico)
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


def preview_words(text: str) -> List[Word]:
    toks = tokenize(text)[:40] or ["Texto", "de", "prueba"]
    return [Word(t, i * PREVIEW_WORD, (i + 1) * PREVIEW_WORD) for i, t in enumerate(toks)]


def preview_block_count(text: str, style: CaptionStyle) -> int:
    return len(make_blocks(preview_words(text), max(1, style.words_per_block), style.uppercase))


def block_duration_ms(text: str, style: CaptionStyle, block_index: int) -> int:
    blocks = make_blocks(preview_words(text), max(1, style.words_per_block), style.uppercase)
    blk = blocks[max(0, min(block_index, len(blocks) - 1))]
    return int((blk[-1].end - blk[0].start) * 1000)


def render_preview_png(
    style: CaptionStyle, text: str, fonts_dir: Path, width: int, height: int, block_index: int = 0, t_ms: int = 400,
) -> Tuple[Optional[bytes], str]:
    """Un fotograma PNG del bloque elegido en el instante t_ms de su animación. Devuelve (png, log)."""
    words = preview_words(text)
    total = len(words) * PREVIEW_WORD + 1.0
    blocks = make_blocks(words, max(1, style.words_per_block), style.uppercase)
    blk = blocks[max(0, min(block_index, len(blocks) - 1))]
    t = blk[0].start + max(0.0, min(t_ms / 1000.0, (blk[-1].end - blk[0].start) - 0.02))
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cap = write_assets(words, style, width, height, total, fonts_dir, d)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "verbose", "-y",
            "-f", "lavfi", "-i", f"color=c={CHROMA_BG}:s={width}x{height}:r=25:d={t + 0.1:.2f}",
            "-vf", make_vf(width, height, "yuv420p", fonts_dir, cap, style),
            "-ss", f"{t:.2f}", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
        ]
        r = subprocess.run(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log = r.stderr.decode("utf-8", "replace")
    return (r.stdout if r.returncode == 0 and r.stdout else None), log


def render_preview_clip(
    style: CaptionStyle, text: str, fonts_dir: Path, width: int, height: int, n_blocks: int = 4,
) -> Tuple[Optional[bytes], str]:
    """Clip corto (MP4) con las primeras palabras para ver las animaciones en movimiento."""
    blocks = make_blocks(preview_words(text), max(1, style.words_per_block), style.uppercase)[:n_blocks]
    words = [w for b in blocks for w in b]
    total = words[-1].end + 0.5
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cap = write_assets(words, style, width, height, total, fonts_dir, d)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={CHROMA_BG}:s={width}x{height}:r=30:d={total:.2f}",
            "-vf", make_vf(width, height, "yuv420p", fonts_dir, cap, style),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-movflags", "+faststart", "clip.mp4",
        ]
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True)
        out = (d / "clip.mp4")
        return (out.read_bytes() if r.returncode == 0 and out.exists() else None), r.stderr[-1500:]


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #
FONT_EXTS = (".ttf", ".otf", ".ttc")


@lru_cache(maxsize=256)
def _font_info(path: str) -> Tuple[str, bool, bool]:
    """(familia, negrita, cursiva) de un archivo de fuente."""
    try:
        from fontTools.ttLib import TTFont

        f = TTFont(path, fontNumber=0, lazy=True)
        fam = (f["name"].getDebugName(1) or "").strip()
        sub = (f["name"].getDebugName(2) or "").lower()
        f.close()
        return fam or re.split(r"[-_ ]", Path(path).stem)[0], "bold" in sub, ("italic" in sub or "oblique" in sub)
    except Exception:
        return re.split(r"[-_ ]", Path(path).stem)[0], False, False


@lru_cache(maxsize=8)
def list_font_families(fonts_dir: str) -> List[str]:
    p = Path(fonts_dir)
    if not p.exists():
        return []
    return sorted({_font_info(str(f))[0] for f in p.iterdir() if f.suffix.lower() in FONT_EXTS}, key=str.lower)


@lru_cache(maxsize=1)
def system_font_families() -> List[str]:
    """Familias instaladas (vía fontconfig). Vacío si no hay fc-list (p. ej. Windows)."""
    if not shutil.which("fc-list"):
        return []
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    return sorted({ln.split(",")[0].replace("\\", "").strip() for ln in out.splitlines() if ln.strip()}, key=str.lower)


@dataclass
class FontMetrics:
    path: Optional[Path]
    ascent: float = 0.93
    descent: float = 0.20
    cap_top: float = 0.69
    desc_bottom: float = 0.21

    @property
    def cell(self) -> float:
        return self.ascent + self.descent


DEFAULT_METRICS = FontMetrics(None, ascent=0.917, descent=0.219, cap_top=0.692, desc_bottom=0.217)


@lru_cache(maxsize=32)
def _truetype(path: str):
    from PIL import ImageFont

    return ImageFont.truetype(path, 1000)


def find_font_file(family: str, bold: bool, italic: bool, fonts_dir: Optional[Path]) -> Optional[Path]:
    best, best_score = None, -1
    if fonts_dir and fonts_dir.exists():
        for p in fonts_dir.iterdir():
            if p.suffix.lower() not in FONT_EXTS:
                continue
            fam, is_b, is_i = _font_info(str(p))
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


@lru_cache(maxsize=64)
def _metrics(family: str, bold: bool, italic: bool, fonts_dir: str) -> FontMetrics:
    path = find_font_file(family, bold, italic, Path(fonts_dir) if fonts_dir else None)
    if not path:
        return DEFAULT_METRICS
    try:
        f = _truetype(str(path))
        asc, desc = (v / 1000.0 for v in f.getmetrics())
        try:  # libass usa las métricas "win" de OS/2 para la celda y la línea base
            from fontTools.ttLib import TTFont

            tt = TTFont(str(path), fontNumber=0, lazy=True)
            upm, os2 = tt["head"].unitsPerEm, tt["OS/2"]
            if os2.usWinAscent + os2.usWinDescent > 0:
                asc, desc = os2.usWinAscent / upm, os2.usWinDescent / upm
            tt.close()
        except Exception:
            pass
        return FontMetrics(path, asc, desc, -f.getbbox("Hbdfhklt", anchor="ls")[1] / 1000.0,
                           f.getbbox("gjpqy", anchor="ls")[3] / 1000.0)
    except Exception:
        return DEFAULT_METRICS


def get_font_metrics(family: str, bold: bool, italic: bool, fonts_dir: Optional[Path]) -> FontMetrics:
    return _metrics(family, bold, italic, str(fonts_dir) if fonts_dir else "")


def text_width_em(metrics: FontMetrics, text: str) -> float:
    if metrics.path:
        try:
            return _truetype(str(metrics.path)).getlength(text) / 1000.0
        except Exception:
            pass
    return 0.55 * len(text)


def name_from_text(text: str, n: int = 3, fallback: str = "subtitulos") -> str:
    """Nombre de archivo con las n primeras palabras del guion (sin tildes ni símbolos)."""
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode(), re.I)
    return "_".join(w.lower() for w in words[:n])[:60] or fallback


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    p, i = folder / f"{stem}.{ext}", 2
    while p.exists():
        p, i = folder / f"{stem}_{i}.{ext}", i + 1
    return p
