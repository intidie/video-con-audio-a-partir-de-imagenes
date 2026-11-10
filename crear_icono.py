"""Genera icono.ico (cartel con la letra S sobre fondo azul oscuro) para el acceso directo."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).parent
img = Image.new("RGBA", (256, 256), (0, 0, 139, 255))
d = ImageDraw.Draw(img)
d.rectangle((28, 70, 228, 186), fill="white", outline="black", width=8)
fonts = sorted((BASE / "fonts").glob("BebasNeue*.ttf"))
font = ImageFont.truetype(str(fonts[0]), 150) if fonts else ImageFont.load_default()
d.text((128, 128), "SUB", fill="black", font=font, anchor="mm")
img.save(BASE / "icono.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
