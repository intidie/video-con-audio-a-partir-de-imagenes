"""Utilidades de FFmpeg compartidas y generador de video lip-sync (PNGtuber)."""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

RES_MAP = {
    "720p HD (1280x720)": (1280, 720),
    "1080p Full HD (1920x1080)": (1920, 1080),
    "2K (2560x1440)": (2560, 1440),
    "4K Ultra HD (3840x2160)": (3840, 2160),
}
ASPECT_MAP = {"16:9 (Horizontal)": (16, 9), "9:16 (Vertical)": (9, 16), "1:1 (Cuadrado)": (1, 1)}
CRF_MAP = {"Máxima (CRF 18)": 18, "Alta (CRF 23)": 23, "Estándar (CRF 28)": 28}


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def run(cmd: Sequence[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# Limpieza de metadatos del video final
# --------------------------------------------------------------------------- #
# Flags para el paso de CODIFICACIÓN: evitan que FFmpeg escriba su "firma" (etiqueta encoder=Lavf/Lavc y la versión
# del codificador AAC, que va dentro del primer frame de audio y no se puede quitar después sin recodificar).
BITEXACT_FLAGS = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]


def exiftool_path() -> Optional[str]:
    """exiftool del PATH o el que el instalador deja en la carpeta tools/ de la app (motor de ExifCleaner)."""
    here = Path(__file__).resolve().parent / "tools"
    for c in (here / "exiftool.exe", here / "exiftool"):
        if c.exists():
            return str(c)
    return shutil.which("exiftool")


def _video_codec(path: Path) -> str:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(path)])
    return r.stdout.strip()


def strip_metadata(path: Path) -> bool:
    """Elimina metadatos del video ya renderizado, SIN recodificar (copia de streams, no pierde calidad).

    Quita: etiquetas globales y por pista (encoder, creation_time, comment...), capítulos, nombres de handler
    (se dejan genéricos) y el SEI de x264 (la cadena "x264 - core ... options" que se incrusta en el bitstream).
    Si `exiftool` está en el PATH, pasa además `exiftool -all=` para borrar lo que quede (equivale a ExifCleaner,
    que es una interfaz gráfica sobre exiftool). Devuelve True si el archivo quedó limpio y es válido; si algo
    falla, deja el original intacto y devuelve False.
    """
    path = Path(path)
    if not path.exists():
        return False
    tmp = path.with_name(f"{path.stem}.limpio{path.suffix}")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:v", "-map", "0:a?", "-c", "copy",
        "-map_metadata", "-1", "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1", "-map_chapters", "-1",
        *BITEXACT_FLAGS,
        "-metadata:s:v:0", "handler_name=Video", "-metadata:s:a:0", "handler_name=Audio",
    ]
    if _video_codec(path) == "h264":
        cmd += ["-bsf:v", "filter_units=remove_types=6"]          # NAL tipo 6 = SEI (aquí vive la firma de x264)
    if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(tmp))
    try:
        r = run(cmd)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            return False
        if run(["ffmpeg", "-v", "error", "-i", str(tmp), "-f", "null", "-"]).stderr.strip():
            return False                                          # el resultado no decodifica limpio: no lo uses
        exif = exiftool_path()
        if exif:
            # Igual que ExifCleaner: borra todo lo que exiftool puede escribir (XMP, QuickTime, UserData, Keys...).
            run([exif, "-q", "-q", "-m", "-overwrite_original", "-api", "LargeFileSupport=1",
                 "-all=", "-XMP:all=", "-QuickTime:all=", str(tmp)])
            if run(["ffmpeg", "-v", "error", "-i", str(tmp), "-f", "null", "-"]).stderr.strip():
                return False
        os.replace(tmp, path)
        return True
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def get_duration(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def detect_silence(path: Path, noise_db: float, min_dur: float) -> List[Tuple[float, Optional[float]]]:
    log = run(["ffmpeg", "-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def build_intervals(duration: float, silences) -> List[Tuple[float, float, str]]:
    segments, cursor = [], 0.0
    for s, e in silences:
        e = e if e is not None else duration
        if s > cursor:
            segments.append((cursor, s, "talking"))
        segments.append((s, e, "silent"))
        cursor = e
    if cursor < duration:
        segments.append((cursor, duration, "talking"))
    segments = [(a, b, lbl) for a, b, lbl in segments if b - a > 0.01]
    return segments or [(0.0, duration, "talking")]


def speech_segments(intervals) -> List[Tuple[float, float]]:
    return [(a, b) for a, b, label in intervals if label == "talking"]


def target_dims(res_label: str, aspect_label: str) -> Tuple[int, int]:
    base_w, base_h = RES_MAP[res_label]
    ar_w, ar_h = ASPECT_MAP[aspect_label]
    ratio = ar_w / ar_h
    if abs(base_w / base_h - ratio) < 0.001:
        return base_w, base_h
    if ratio >= 1:
        width = max(base_w, base_h)
        height = round(width / ratio)
    else:
        height = max(base_w, base_h)
        width = round(height * ratio)
    return width + width % 2, height + height % 2


# --------------------------------------------------------------------------- #
# Lip-sync (PNGtuber): alterna imágenes "hablando" / "silencio" según el audio
# --------------------------------------------------------------------------- #
def make_lipsync(
    audio: Path, talking: Sequence[Path], silent: Sequence[Path], out_path: Path, work_dir: Path,
    width: int, height: int, fps: int, crf: int, noise_db: float, min_silence: float,
    progress: Optional[Callable[[float, str], None]] = None, switch_s: float = 0.18, clean: bool = True,
) -> Tuple[bool, str, int]:
    """Devuelve (ok, log, n_segmentos). Cada imagen se escala una sola vez; el render usa x264 veryfast."""
    say = progress or (lambda f, m: None)
    say(0.05, "Analizando audio")
    duration = get_duration(audio)
    segments = build_intervals(duration, detect_silence(audio, noise_db, min_silence))

    say(0.15, "Preparando imágenes")
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"

    def prep(paths: Sequence[Path], tag: str) -> List[Path]:
        outs = []
        for i, p in enumerate(paths):
            dst = work_dir / f"{tag}_{i}.png"
            run(["ffmpeg", "-y", "-i", str(p), "-vf", vf, "-frames:v", "1", str(dst)])
            outs.append(dst)
        return outs

    t_imgs, s_imgs = prep(talking, "talk"), prep(silent, "sil")
    cycles = {"talking": itertools.cycle(t_imgs), "silent": itertools.cycle(s_imgs)}
    lines = ["ffconcat version 1.0"]
    for start, end, label in segments:
        cur, dur = 0.0, end - start
        while cur < dur - 1e-6:
            step = min(switch_s, dur - cur)
            lines += [f"file '{next(cycles[label]).name}'", f"duration {round(step, 3)}"]
            cur += step
    lines.append(f"file '{next(cycles[segments[-1][2]]).name}'")
    (work_dir / "filelist.txt").write_text("\n".join(lines), encoding="utf-8")

    say(0.3, "Renderizando")
    r = run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "filelist.txt", "-i", str(audio),
        "-r", str(fps), "-fps_mode", "cfr", "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
        "-tune", "stillimage", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-crf", str(crf), "-g", str(fps), "-keyint_min", str(fps),
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-tag:v", "avc1", "-movflags", "+faststart",
        *BITEXACT_FLAGS, "-map_metadata", "-1", "-shortest", str(out_path),
    ], cwd=work_dir)
    ok = out_path.exists() and out_path.stat().st_size > 0
    if ok and clean:
        say(0.97, "Limpiando metadatos")
        strip_metadata(out_path)
    say(1.0, "Listo")
    return ok, r.stderr[-3000:], len(segments)
