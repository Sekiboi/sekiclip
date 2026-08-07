"""Regenerate assets/sekiclip.png and assets/sekiclip.ico.

Play wedge on rounded square — same plate blue as Sekikit brand.
Play mark is a compact equilateral-style triangle with generous padding.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
# Match Sekikit icon plate
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

    # Compact equilateral play — ~20% of canvas so the plate reads clearly.
    # Optical center: slight left shift so the triangle mass feels centered.
    ink = WHITE if with_plate else PLATE
    cx = s * 0.5
    cy = s * 0.5
    side = s * 0.22  # vertical base length
    height = side * 0.86602540378  # equilateral tip distance
    ox = cx - height * 0.10
    x0 = ox - height * 0.28
    x1 = ox + height * 0.72
    y0 = cy - side * 0.5
    y1 = cy + side * 0.5
    tri = [(x0, y0), (x0, y1), (x1, cy)]
    d.polygon(tri, fill=ink)
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    draw_icon(512, True).save(ASSETS / "sekiclip.png")
    draw_icon(512, False).save(ASSETS / "sekiclip_mark.png")

    # Proper multi-resolution ICO (Windows taskbar / installer)
    ico_frames = [draw_icon(s, True) for s in (16, 24, 32, 48, 64, 128, 256)]
    ico_frames[0].save(
        ASSETS / "sekiclip.ico",
        format="ICO",
        sizes=[(im.width, im.height) for im in ico_frames],
        append_images=ico_frames[1:],
    )
    print(f"Wrote {ASSETS / 'sekiclip.png'}")
    print(f"Wrote {ASSETS / 'sekiclip_mark.png'}")
    print(f"Wrote {ASSETS / 'sekiclip.ico'}")


if __name__ == "__main__":
    main()
