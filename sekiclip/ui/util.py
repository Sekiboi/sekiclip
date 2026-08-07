"""Shared GUI helpers (no window class)."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

PREVIEW_W, PREVIEW_H = 960, 540
PREVIEW_MAX = (PREVIEW_W, PREVIEW_H)
PREVIEW_MIN = (320, 180)


def resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base.joinpath(*parts)
    return Path(__file__).resolve().parent.parent.parent.joinpath(*parts)


def parse_drop(data: str) -> list[Path]:
    """Parse tkinterdnd2 / Windows Explorer drop payload into existing files.

    Handles ``{C:\\path with spaces\\a.mp4}``, bare paths, newlines, nulls,
    and ``file:///`` URIs. File size is irrelevant — only that the path exists.
    """
    if not data:
        return []
    from urllib.parse import unquote, urlparse

    raw = str(data).replace("\r\n", "\n").replace("\r", "\n").replace("\0", "\n")
    tokens: list[str] = []
    token = ""
    in_brace = False
    for ch in raw:
        if ch == "{":
            if in_brace and token:
                token += ch
            else:
                in_brace = True
                token = ""
        elif ch == "}" and in_brace:
            in_brace = False
            if token.strip():
                tokens.append(token.strip())
            token = ""
        elif ch in (" ", "\n", "\t") and not in_brace:
            if token.strip():
                tokens.append(token.strip())
            token = ""
        else:
            token += ch
    if token.strip():
        tokens.append(token.strip())

    out: list[Path] = []
    seen: set[str] = set()
    for t in tokens:
        s = t.strip().strip('"').strip("'")
        if not s:
            continue
        if s.lower().startswith("file:"):
            parsed = urlparse(s)
            s = unquote(parsed.path or "")
            if sys.platform == "win32" and len(s) >= 3 and s[0] == "/" and s[2] == ":":
                s = s[1:]
        try:
            p = Path(s).expanduser()
            try:
                p = p.resolve(strict=False)
            except OSError:
                pass
            key = str(p).lower() if sys.platform == "win32" else str(p)
            if key in seen:
                continue
            if p.is_file():
                seen.add(key)
                out.append(p)
        except OSError:
            continue
    return out


def fit_image(img: Image.Image, max_size: tuple[int, int] = PREVIEW_MAX) -> Image.Image:
    """Letterbox into a fixed stage so framing stays stable while scrubbing/playing."""
    stage_w, stage_h = max_size
    src = img.convert("RGB")
    src.thumbnail((stage_w, stage_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (stage_w, stage_h), (20, 20, 24))
    x = (stage_w - src.width) // 2
    y = (stage_h - src.height) // 2
    canvas.paste(src, (x, y))
    return canvas
