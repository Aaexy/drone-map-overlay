"""Generate src/dronemap/assets/icon.png (1024x1024). Run: python packaging/make_icon.py"""

import math
import os

from PIL import Image, ImageDraw

N, AA = 1024, 2
S = N * AA
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# rounded-square background with a vertical gradient
top, bot = (34, 40, 52), (16, 19, 25)
grad = Image.new("RGBA", (1, S))
grad.putdata([tuple(int(top[c] + (bot[c] - top[c]) * y / S) for c in range(3)) + (255,) for y in range(S)])
bg = grad.resize((S, S))
mask = Image.new("L", (S, S), 0)
pad = int(S * 0.06)
ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], int(S * 0.2), fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)
# map: streets
street = (70, 80, 98, 255)
for i in range(-2, 6):
    x = pad + i * S * 0.2
    d.line([(x, pad), (x + S * 0.55, S - pad)], fill=street, width=int(S * 0.018))
for i in range(-1, 6):
    y = pad + i * S * 0.18
    d.line([(pad, y + S * 0.2), (S - pad, y - S * 0.12)], fill=street, width=int(S * 0.012))
img.putalpha(Image.composite(img.getchannel("A"), Image.new("L", (S, S), 0), mask))
d = ImageDraw.Draw(img)
# flight path
accent = (255, 92, 54, 255)
pts = [(S * 0.22, S * 0.78), (S * 0.36, S * 0.62), (S * 0.5, S * 0.6), (S * 0.62, S * 0.46)]
d.line(pts, fill=(255, 255, 255, 255), width=int(S * 0.06), joint="curve")
d.line(pts, fill=accent, width=int(S * 0.036), joint="curve")
r = S * 0.035
d.ellipse([pts[0][0] - r, pts[0][1] - r, pts[0][0] + r, pts[0][1] + r], fill=(255, 255, 255, 255))
# arrow marker
cx, cy, L = S * 0.66, S * 0.42, S * 0.15
a = math.radians(45)
R = lambda x, y: (cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a))
halo = S * 0.2
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([cx - halo, cy - halo, cx + halo, cy + halo], fill=(255, 92, 54, 60))
img.alpha_composite(glow)
d = ImageDraw.Draw(img)
d.polygon([R(0, -L), R(0.75 * L, 0.8 * L), R(0, 0.38 * L), R(-0.75 * L, 0.8 * L)], fill=(255, 255, 255, 255))
d.polygon([R(0, -L * 0.6), R(0.43 * L, 0.52 * L), R(0, 0.22 * L), R(-0.43 * L, 0.52 * L)], fill=accent)
out = img.resize((N, N), Image.LANCZOS)
here = os.path.dirname(os.path.abspath(__file__))
out.save(os.path.join(here, "..", "src", "dronemap", "assets", "icon.png"))
print("ok")
