"""Regenerate Sekiclip icons + Inno Setup wizard images.

Plate blue matches Sekikit: (47, 111, 168).
Play mark: first-fixed balanced proportions (not full-bleed, not tiny).

Outputs:
  assets/sekiclip.png / .ico / _mark.png
  assets/wizard_image.bmp   (164:314 aspect for WizardImageFile)
  assets/wizard_small.bmp   (for WizardSmallImageFile)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

PLATE = (47, 111, 168, 255)
PLATE_RGB = (47, 111, 168)
WHITE = (255, 255, 255, 255)

# Inno docs: keep 164:314 aspect; ≥202×386 recommended for HiDPI
WIZARD_LARGE = (240, 459)  # 164:314 * ~1.46
WIZARD_SMALL = (58, 58)


def _play_triangle(s: float, cx: float, cy: float) -> list[tuple[float, float]]:
    """Balanced play wedge (first fixed size), centered optically at (cx, cy)."""
    ox = cx - s * 0.03
    half_h = s * 0.155
    depth = s * 0.26
    return [
        (ox - depth * 0.35, cy - half_h),
        (ox - depth * 0.35, cy + half_h),
        (ox + depth * 0.65, cy),
    ]


def draw_icon(size: int, with_plate: bool = True) -> Image.Image:
    """Square app icon. Draw natively at ``size`` (do not upscale from 16px)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = float(size)

    if with_plate:
        pad = max(1, int(round(s * 0.06)))
        d.rounded_rectangle(
            [pad, pad, size - pad - 1, size - pad - 1],
            radius=max(2, int(round(s * 0.22))),
            fill=PLATE,
        )

    ink = WHITE if with_plate else PLATE
    d.polygon(_play_triangle(s, s * 0.5, s * 0.5), fill=ink)
    return img


def draw_wizard_large() -> Image.Image:
    """Tall left-panel art: solid plate + large play (same proportions as app icon).

    Drawn flat (no nested rounded tile) so Inno scaling stays clean.
    """
    w, h = WIZARD_LARGE
    img = Image.new("RGB", (w, h), PLATE_RGB)
    d = ImageDraw.Draw(img)
    # Scale play relative to panel width so it reads large but not edge-to-edge
    s = float(min(w, h)) * 0.72
    cx = w * 0.50
    cy = h * 0.42
    d.polygon(_play_triangle(s, cx, cy), fill=(255, 255, 255))
    return img


def draw_wizard_small() -> Image.Image:
    """Square small wizard icon (full app mark)."""
    icon = draw_icon(256, True).resize(WIZARD_SMALL, Image.Resampling.LANCZOS)
    bg = Image.new("RGB", WIZARD_SMALL, PLATE_RGB)
    bg.paste(icon, mask=icon.split()[3])
    return bg


def save_multi_ico(path: Path, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> None:
    """Write a real multi-resolution .ico (each size drawn natively)."""
    frames = [draw_icon(s, True).convert("RGBA") for s in sizes]
    # Largest first helps some tools; Pillow embeds all via append_images
    frames_sorted = sorted(frames, key=lambda im: im.size[0], reverse=True)
    # Do NOT pass sizes= with wrong semantics — that produced a 16×16-only file before.
    frames_sorted[0].save(
        path,
        format="ICO",
        append_images=frames_sorted[1:],
        bitmap_format="bmp",
    )


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    draw_icon(512, True).save(ASSETS / "sekiclip.png")
    draw_icon(512, False).save(ASSETS / "sekiclip_mark.png")
    save_multi_ico(ASSETS / "sekiclip.ico")

    draw_wizard_large().save(ASSETS / "wizard_image.bmp", format="BMP")
    draw_wizard_small().save(ASSETS / "wizard_small.bmp", format="BMP")

    # Verify ICO directory lists multiple native sizes (Pillow n_frames is unreliable)
    import struct

    raw = (ASSETS / "sekiclip.ico").read_bytes()
    _res, _typ, count = struct.unpack_from("<HHH", raw, 0)
    ico_sizes: list[tuple[int, int]] = []
    for i in range(count):
        w, h = struct.unpack_from("<BB", raw, 6 + i * 16)[:2]
        ico_sizes.append((w or 256, h or 256))

    print(f"Wrote {ASSETS / 'sekiclip.png'}")
    print(f"Wrote {ASSETS / 'sekiclip_mark.png'}")
    print(f"Wrote {ASSETS / 'sekiclip.ico'} count={count} sizes={ico_sizes}")
    print(f"Wrote {ASSETS / 'wizard_image.bmp'} {WIZARD_LARGE}")
    print(f"Wrote {ASSETS / 'wizard_small.bmp'} {WIZARD_SMALL}")
    if count < 5 or max(s[0] for s in ico_sizes) < 128:
        raise SystemExit(f"ICO multi-size failed: {ico_sizes}")


if __name__ == "__main__":
    main()
