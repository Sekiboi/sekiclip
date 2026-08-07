"""Regenerate Sekiclip icons + Inno Setup wizard images.

Play wedge on rounded square — Sekikit plate blue (47, 111, 168).
Play size: first fixed proportions (compact, not stretched / not tiny).
Also writes wizard_image.bmp + wizard_small.bmp for the Setup wizard.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
# Match Sekikit icon plate
PLATE = (47, 111, 168, 255)
WHITE = (255, 255, 255, 255)
# Inno Setup classic wizard bitmap sizes
WIZARD_LARGE = (164, 314)
WIZARD_SMALL = (55, 55)


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

    # First fixed play triangle: balanced, not full-bleed, not elongated.
    ink = WHITE if with_plate else PLATE
    cx = s * 0.5
    cy = s * 0.5
    ox = cx - s * 0.03
    half_h = s * 0.155
    depth = s * 0.26
    tri = [
        (ox - depth * 0.35, cy - half_h),
        (ox - depth * 0.35, cy + half_h),
        (ox + depth * 0.65, cy),
    ]
    d.polygon(tri, fill=ink)
    return img


def _to_bmp_rgb(img: Image.Image) -> Image.Image:
    """Inno wizard images are most reliable as 24-bit BMP (no alpha)."""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, PLATE[:3])
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB")


def draw_wizard_large() -> Image.Image:
    """Tall welcome-page panel: plate fill + centered app icon."""
    w, h = WIZARD_LARGE
    canvas = Image.new("RGB", (w, h), PLATE[:3])
    icon_px = min(w - 24, 120)
    icon = draw_icon(icon_px * 2, True).resize((icon_px, icon_px), Image.Resampling.LANCZOS)
    icon_rgb = _to_bmp_rgb(icon)
    x = (w - icon_px) // 2
    y = (h - icon_px) // 2 - h // 12  # slightly high for visual balance
    canvas.paste(icon_rgb, (x, y))
    return canvas


def draw_wizard_small() -> Image.Image:
    """Top-right corner icon on non-welcome wizard pages."""
    w, h = WIZARD_SMALL
    icon = draw_icon(128, True).resize((w, h), Image.Resampling.LANCZOS)
    return _to_bmp_rgb(icon)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    draw_icon(512, True).save(ASSETS / "sekiclip.png")
    draw_icon(512, False).save(ASSETS / "sekiclip_mark.png")

    # Multi-resolution ICO (exe / shortcuts / SetupIconFile)
    ico_frames = [draw_icon(s, True) for s in (16, 24, 32, 48, 64, 128, 256)]
    ico_frames[0].save(
        ASSETS / "sekiclip.ico",
        format="ICO",
        sizes=[(im.width, im.height) for im in ico_frames],
        append_images=ico_frames[1:],
    )

    # Inno Setup wizard graphics (BMP)
    draw_wizard_large().save(ASSETS / "wizard_image.bmp", format="BMP")
    draw_wizard_small().save(ASSETS / "wizard_small.bmp", format="BMP")

    print(f"Wrote {ASSETS / 'sekiclip.png'}")
    print(f"Wrote {ASSETS / 'sekiclip_mark.png'}")
    print(f"Wrote {ASSETS / 'sekiclip.ico'}")
    print(f"Wrote {ASSETS / 'wizard_image.bmp'}")
    print(f"Wrote {ASSETS / 'wizard_small.bmp'}")


if __name__ == "__main__":
    main()
