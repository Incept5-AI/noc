"""Regenerate NOC/Assets.xcassets/AppIcon.appiconset/icon-1024.png (needs Pillow)."""
import math
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGB", (S, S), (8, 11, 16))
d = ImageDraw.Draw(img)
for i in range(1, 5):                                   # minute gridlines
    x = int(S * i / 5)
    d.line([(x, 260), (x, 780)], fill=(27, 38, 52), width=6)
pts = []
for i in range(61):                                     # a sparkline that climbs then settles
    x = 90 + i * (S - 180) / 60
    v = 0.25 + 0.5 * math.exp(-((i - 38) / 9) ** 2) + 0.08 * math.sin(i / 2.3)
    pts.append((x, 780 - v * 520))
stops = [(0, (59, 130, 246)), (0.4, (63, 185, 80)), (0.6, (227, 179, 65)), (0.8, (240, 136, 62)), (1, (240, 96, 62))]
def col(t):
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = (t - a) / (b - a)
            return tuple(int(ca[k] + f * (cb[k] - ca[k])) for k in range(3))
    return stops[-1][1]
for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
    t = (780 - (y1 + y2) / 2) / 520
    d.line([(x1, y1), (x2, y2)], fill=col(max(0, min(1, t))), width=30)
d.ellipse([pts[-1][0] - 26, pts[-1][1] - 26, pts[-1][0] + 26, pts[-1][1] + 26], fill=(63, 185, 80))
img.save("NOC/Assets.xcassets/AppIcon.appiconset/icon-1024.png")
