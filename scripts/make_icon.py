"""Regenerate assets/sekiclip.png and assets/sekiclip.ico.

Play wedge on rounded square — same plate blue as Sekikit brand.
Sekikit PLATE: (47, 111, 168).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
# Match Sekikit icon plate (scripts/make_icon.py in justpages / Sekikit)
PLATE = (47, 111, 168, 255)
WHITE = (255, 255, 255, 255)


def draw_icon(size: int, with_plate: bool = True) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = float(size)

    if with_plate:
        pad = max(1, int(s * 0.06))
        d.rounded_rectangle(
            [pad, pad, size - pad - 1, size - pad - 1],
            radius=max(2, int(s * 0.22)),
            fill=PLATE,
        )

    # Play triangle (media), white on plate — bare mark uses plate fill on clear
    m = 0.28 if with_plate else 0.18
    ink = WHITE if with_plate else PLATE
    left = m * s
    right = (1 - m * 0.55) * s
    top = m * s
    bot = (1 - m) * s
    mid_y = (top + bot) / 2
    tri = [(left, top), (left, bot), (right, mid_y)]
    d.polygon(tri, fill=ink)
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    draw_icon(512, True).save(ASSETS / "sekiclip.png")
    draw_icon(512, False).save(ASSETS / "sekiclip_mark.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    draw_icon(256, True).save(ASSETS / "sekiclip.ico", format="ICO", sizes=sizes)
    print(f"Wrote {ASSETS / 'sekiclip.png'}")
    print(f"Wrote {ASSETS / 'sekiclip_mark.png'}")
    print(f"Wrote {ASSETS / 'sekiclip.ico'}")


if __name__ == "__main__":
    main()
