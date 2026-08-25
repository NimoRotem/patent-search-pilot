"""Draw a sketch that looks hand-made, so the tracer can be exercised without a scanner.

Wobbly strokes, uneven pressure, a lighting gradient and paper grain: the things that defeat a
global threshold and a boundary tracer. Not part of the product; a fixture.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def wobble(points, rng, amount=2.4, step=6):
    """Resample a path with a slow random walk on it, the way a hand shakes."""
    out = []
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        length = math.hypot(x1 - x0, y1 - y0)
        n = max(2, int(length / step))
        for k in range(n):
            t = k / n
            drift = rng.uniform(-amount, amount)
            nx, ny = -(y1 - y0) / (length or 1), (x1 - x0) / (length or 1)
            out.append((x0 + (x1 - x0) * t + nx * drift, y0 + (y1 - y0) * t + ny * drift))
    out.append(points[-1])
    return out


def draw(path: Path, seed: int = 7) -> None:
    rng = random.Random(seed)
    W, H = 1100, 820
    image = Image.new("L", (W, H), 245)
    pen = ImageDraw.Draw(image)

    def stroke(points, width=5, closed=False):
        pts = list(points) + ([points[0]] if closed else [])
        pen.line(wobble(pts, rng), fill=rng.randint(35, 70), width=width, joint="curve")

    # A body with a lid, a spout and a handle: separate closed shapes, which is what a numeral
    # points at.
    stroke([(230, 300), (620, 300), (620, 620), (230, 620)], closed=True, width=6)
    stroke([(255, 240), (595, 240), (595, 295), (255, 295)], closed=True, width=5)
    pen.ellipse([700, 380, 860, 540], outline=rng.randint(35, 70), width=6)
    stroke([(640, 430), (700, 450)], width=5)
    stroke([(640, 490), (700, 470)], width=5)
    stroke([(300, 660), (560, 660), (560, 720), (300, 720)], closed=True, width=5)

    # Paper: a lighting gradient across the sheet, then grain.
    gradient = Image.linear_gradient("L").resize((W, H)).point(lambda v: 255 - v // 3)
    image = Image.blend(image, gradient, 0.28)
    noise = Image.effect_noise((W, H), 14)
    image = Image.blend(image, noise, 0.10).filter(ImageFilter.GaussianBlur(0.6))
    image.save(path)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sketch.png")
    draw(target)
    print(f"{target}: {target.stat().st_size:,} bytes")
