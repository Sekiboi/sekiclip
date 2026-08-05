"""Generate simple Clipwork icon (play wedge on rounded square)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BLUE = (37, 99, 235, 255)
WHITE = (255, 255, 255, 255)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = 16
    d.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=48,
        fill=BLUE,
    )
    # Play triangle
    cx, cy = size // 2, size // 2
    tri = [(cx - 28, cy - 40), (cx - 28, cy + 40), (cx + 48, cy)]
    d.polygon(tri, fill=WHITE)
    png = ASSETS / "clipwork.png"
    img.save(png)
    # ICO sizes
    ico_path = ASSETS / "clipwork.ico"
    icons = []
    for s in (16, 32, 48, 64, 128, 256):
        icons.append(img.resize((s, s), Image.Resampling.LANCZOS))
    icons[-1].save(ico_path, format="ICO", sizes=[(i.width, i.height) for i in icons])
    mark = img.resize((64, 64), Image.Resampling.LANCZOS)
    mark.save(ASSETS / "clipwork_mark.png")
    print(f"Wrote {png}")
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()
