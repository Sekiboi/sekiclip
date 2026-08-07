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

    # Compact play triangle (not full-bleed). Slight left optical bias so it
    # reads centered; height slightly > depth so it isn't stretched sideways.
    ink = WHITE if with_plate else PLATE
    cx = s * 0.5
    cy = s * 0.5
    # Shift geometry left so the mass of the triangle feels centered
    ox = cx - s * 0.03
    half_h = s * 0.155  # vertical half-edge
    depth = s * 0.26  # base → tip (shorter than height → not elongated)
    tri = [
        (ox - depth * 0.35, cy - half_h),
        (ox - depth * 0.35, cy + half_h),
        (ox + depth * 0.65, cy),
    ]
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
