"""Voz robótica con meSpeak.js (eSpeak en JavaScript), ejecutada en Node sin navegador.

Cada palabra se sintetiza por separado y se unen los WAV: así los tiempos por palabra son exactos
(meSpeak no entrega eventos de palabra). Requiere Node.js (`winget install OpenJS.NodeJS.LTS`).
"""
from __future__ import annotations

import array
import json
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import List, Tuple

from subtitles import Word, tokenize

MESPEAK_DIR = Path(__file__).parent / "mespeak"
PREFIX = "mespeak:"
# Menú igual al de meSpeak.js: Voice (idioma) y Variant. Clave = texto del menú, valor = archivo/valor interno.
ROBOT_LANGS = {"ca - Catalan": "ca", "cs - Czech": "cs", "de - German": "de", "el - Greek": "el", "en - English": "en/en", "en-n - English, regional": "en/en-n", "en-rp - English, regional": "en/en-rp", "en-sc - English, Scottish": "en/en-sc", "en-us - English, US": "en/en-us", "en-wm - English, regional": "en/en-wm", "eo - Esperanto": "eo", "es - Spanish": "es", "es-la - Spanish, Latin America": "es-la", "fi - Finnish": "fi", "fr - French": "fr", "hu - Hungarian": "hu", "it - Italian": "it", "kn - Kannada": "kn", "la - Latin": "la", "lv - Latvian": "lv", "nl - Dutch": "nl", "pl - Polish": "pl", "pt - Portuguese, Brazil": "pt", "pt-pt - Portuguese, European": "pt-pt", "ro - Romanian": "ro", "sk - Slovak": "sk", "sv - Swedish": "sv", "tr - Turkish": "tr", "zh - Mandarin Chinese (Pinyin)": "zh", "zh-yue - Cantonese Chinese": "zh-yue"}
ROBOT_VARIANTS = {"None": "", "f1 (female 1)": "f1", "f2 (female 2)": "f2", "f3 (female 3)": "f3", "f4 (female 4)": "f4", "f5 (female 5)": "f5", "m1 (male 1)": "m1", "m2 (male 2)": "m2", "m3 (male 3)": "m3", "m4 (male 4)": "m4", "m5 (male 5)": "m5", "m6 (male 6)": "m6", "m7 (male 7)": "m7", "croak": "croak", "klatt": "klatt", "klatt2": "klatt2", "klatt3": "klatt3", "whisper": "whisper", "whisperf (female)": "whisperf"}
ROBOT_DEFAULTS = {"amp": 100, "pitch": 50, "speed": 175, "gap": 0}
ROBOT_ENTRY = "Robot (meSpeak.js)"


def make_robot_id(lang_label: str, variant_label: str, amp: int, pitch: int, speed: int, gap: int) -> str:
    """Empaqueta las opciones del menú en un único identificador de voz (así viaja por la caché y el TTS)."""
    return ":".join([PREFIX.rstrip(":"), ROBOT_LANGS[lang_label], ROBOT_VARIANTS[variant_label],
                     str(amp), str(pitch), str(speed), str(gap)])
GAP_S, COMMA_S, STOP_S = 0.04, 0.12, 0.28   # silencios entre palabras / tras coma / tras punto


def is_robot(voice_id: str) -> bool:
    return voice_id.startswith(PREFIX)


def _num(s: str) -> int:
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else 0


def _read_wav(p: Path) -> Tuple[bytes, int]:
    """PCM del WAV sin el silencio sobrante del principio y del final (eSpeak lo añade a cada frase)."""
    with wave.open(str(p), "rb") as w:
        raw, hz = w.readframes(w.getnframes()), w.getframerate()
    s = array.array("h", raw)
    loud = [i for i, v in enumerate(s) if abs(v) > 400]
    if not loud:
        return raw, hz
    a, b = max(0, loud[0] - int(0.01 * hz)), min(len(s), loud[-1] + int(0.03 * hz))
    return s[a:b].tobytes(), hz


def synthesize_robot(text: str, voice_id: str, rate: str, pitch: str, out_path: Path) -> Tuple[List[Word], str]:
    """Genera el MP3 con meSpeak y devuelve (palabras con tiempos, "exacto"). rate "+20%", pitch "-10Hz"."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Falta Node.js para la voz robótica. Instálalo con: winget install OpenJS.NodeJS.LTS")
    _, voice, variant, amp, pit, speed, gap = voice_id.split(":")
    tokens = tokenize(text)
    if not tokens:
        return [], "exacto"
    speed, pit, amp, gap_s = int(speed), int(pit), int(amp), GAP_S + int(gap) * 0.01   # "Word gap" = 10 ms por unidad
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        cfg = {"voice": voice, "variant": variant, "outdir": str(d / "w"), "items": tokens, "speed": speed,
               "pitch": pit, "amplitude": amp, "wordgap": 0}
        (d / "in.json").write_text(json.dumps(cfg), encoding="utf-8")
        r = subprocess.run([node, str(MESPEAK_DIR / "mespeak_node.js"), str(d / "in.json")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"meSpeak falló: {(r.stderr or r.stdout)[-400:]}")
        pcm, rate_hz, words, cursor = bytearray(), 22050, [], 0.0
        for i, tok in enumerate(tokens):
            data, rate_hz = _read_wav(d / "w" / f"w{i:05d}.wav")
            dur = len(data) / 2 / rate_hz
            words.append(Word(tok, cursor, cursor + dur))
            pcm += data
            gap = STOP_S if re.search(r"[.!?…]$", tok) else COMMA_S if re.search(r"[,;:]$", tok) else gap_s
            pcm += b"\x00\x00" * int(gap * rate_hz)
            cursor += dur + gap
        wav_path = d / "all.wav"
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate_hz)
            w.writeframes(bytes(pcm))
        c = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-c:a", "libmp3lame",
                            "-q:a", "2", str(out_path)], capture_output=True, text=True)
        if c.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"No se pudo crear el MP3: {c.stderr[-300:]}")
    return words, "exacto"
