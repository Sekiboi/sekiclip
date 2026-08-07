"""Optional low-res scrub proxy (preview only). Offline."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sekiclip.media_ops.ffmpeg_util import find_ffmpeg, run_ffmpeg


def proxy_cache_dir() -> Path:
    try:
        from sekiclip.core.prefs import user_data_dir

        d = user_data_dir() / "proxy_cache"
    except Exception:
        d = Path.home() / ".sekiclip_proxy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def proxy_path_for(src: Path) -> Path:
    src = Path(src).resolve()
    h = hashlib.sha1(str(src).encode("utf-8", errors="replace")).hexdigest()[:16]
    try:
        st = src.stat()
        tag = f"{st.st_size}_{int(st.st_mtime)}"
    except OSError:
        tag = "0"
    return proxy_cache_dir() / f"{h}_{tag}_480p.mp4"


def ensure_scrub_proxy(
    src: Path | str,
    *,
    max_w: int = 854,
    force: bool = False,
) -> Path | None:
    """Build or return a 480p-ish H.264 proxy for smoother scrub.

    Returns proxy path, or None if ffmpeg missing / failed.
    Best-effort only — source file is unchanged.
    """
    src = Path(src)
    if not src.is_file() or not find_ffmpeg():
        return None
    dest = proxy_path_for(src)
    if dest.is_file() and dest.stat().st_size > 1024 and not force:
        return dest
    try:
        if dest.is_file():
            dest.unlink()
    except OSError:
        pass
    # Fast encode, low res — preview only
    try:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-vf",
                f"scale='min({int(max_w)}\\,iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-an",
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
        if dest.is_file() and dest.stat().st_size > 1024:
            return dest
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    return None
