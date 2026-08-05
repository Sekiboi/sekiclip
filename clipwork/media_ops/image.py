"""Image operations via Pillow (and optional PDF via Pillow)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from clipwork.media_ops.ffmpeg_util import default_output, unique_path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def _open(path: Path) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


def convert_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fmt: str = "png",
) -> Path:
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    if fmt == "jpg":
        fmt = "jpeg"
    ext = ".jpg" if fmt == "jpeg" else f".{fmt}"
    out = Path(dest) if dest else default_output(src, ext, "convert")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ext, "convert")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    if fmt == "jpeg" and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    save_kw: dict = {}
    if fmt == "jpeg":
        save_kw["quality"] = 90
        save_kw["optimize"] = True
    elif fmt == "png":
        save_kw["optimize"] = True
    elif fmt == "webp":
        save_kw["quality"] = 90
    img.save(out, format=fmt.upper() if fmt != "jpeg" else "JPEG", **save_kw)
    return out


def resize_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    max_edge: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, src.suffix, "resize")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, src.suffix or ".png", "resize")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    if max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    elif width or height:
        w, h = img.size
        if width and height:
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        elif width:
            nh = max(1, int(h * (width / w)))
            img = img.resize((width, nh), Image.Resampling.LANCZOS)
        elif height:
            nw = max(1, int(w * (height / h)))
            img = img.resize((nw, height), Image.Resampling.LANCZOS)
    else:
        raise ValueError("Provide max_edge or width/height")
    if out.suffix.lower() in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out)
    return out


def compress_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    quality: int = 75,
    max_edge: int | None = 1920,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, ".jpg", "compress")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ".jpg", "compress")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    if max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out, format="JPEG", quality=int(quality), optimize=True)
    return out


def rotate_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    degrees: int = 90,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, src.suffix, f"rot{degrees}")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, src.suffix or ".png", f"rot{degrees}")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    # Pillow: expand so corners aren't clipped
    img = img.rotate(-int(degrees), expand=True)
    if out.suffix.lower() in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out)
    return out


def flip_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    horizontal: bool = True,
) -> Path:
    src = Path(src)
    tag = "flip_h" if horizontal else "flip_v"
    out = Path(dest) if dest else default_output(src, src.suffix, tag)
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, src.suffix or ".png", tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    img = ImageOps.mirror(img) if horizontal else ImageOps.flip(img)
    if out.suffix.lower() in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out)
    return out


def crop_image(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, src.suffix, "crop")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, src.suffix or ".png", "crop")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    img = img.crop((left, top, right, bottom))
    if out.suffix.lower() in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.save(out)
    return out


def strip_exif(src: Path | str, dest: Path | str | None = None) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, src.suffix, "noexif")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, src.suffix or ".jpg", "noexif")
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open(src)
    clean = img.copy()
    clean.info = {}
    if out.suffix.lower() in (".jpg", ".jpeg") and clean.mode in ("RGBA", "P"):
        clean = clean.convert("RGB")
    if out.suffix.lower() in (".jpg", ".jpeg"):
        clean.save(out, format="JPEG", quality=90, optimize=True, exif=b"")
    else:
        clean.save(out)
    return out


def images_to_pdf(
    sources: list[Path | str],
    dest: Path | str,
) -> Path:
    paths = [Path(p) for p in sources]
    if not paths:
        raise ValueError("No images")
    out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    images: list[Image.Image] = []
    for p in paths:
        im = _open(p)
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        else:
            im = im.convert("RGB")
        images.append(im)
    first, rest = images[0], images[1:]
    first.save(out, "PDF", save_all=True, append_images=rest)
    return out
