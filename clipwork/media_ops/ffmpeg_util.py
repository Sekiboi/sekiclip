"""Locate and run ffmpeg / ffprobe. Offline only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

_WARNINGS: list[str] = []
_PROC_LOCK = threading.Lock()
_CURRENT_PROC: subprocess.Popen[str] | None = None
_CANCEL_REQUESTED = False


def take_warnings() -> list[str]:
    global _WARNINGS
    out = list(_WARNINGS)
    _WARNINGS = []
    return out


def warn(msg: str) -> None:
    _WARNINGS.append(msg)


def _bundled_bin_dir() -> Path | None:
    """vendor/ next to package root, or _internal/vendor when frozen."""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: exe next to _internal
        base = Path(sys.executable).resolve().parent
        for candidate in (base / "vendor", base / "_internal" / "vendor"):
            if candidate.is_dir():
                return candidate
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p = Path(meipass) / "vendor"
            if p.is_dir():
                return p
        return None
    root = Path(__file__).resolve().parent.parent.parent
    p = root / "vendor"
    return p if p.is_dir() else None


def find_ffmpeg() -> Path | None:
    env = os.environ.get("CLIPWORK_FFMPEG") or os.environ.get("FFMPEG")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    bundled = _bundled_bin_dir()
    if bundled:
        for name in ("ffmpeg.exe", "ffmpeg"):
            cand = bundled / name
            if cand.is_file():
                return cand
    which = shutil.which("ffmpeg")
    return Path(which) if which else None


def find_ffprobe() -> Path | None:
    env = os.environ.get("CLIPWORK_FFPROBE") or os.environ.get("FFPROBE")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    bundled = _bundled_bin_dir()
    if bundled:
        for name in ("ffprobe.exe", "ffprobe"):
            cand = bundled / name
            if cand.is_file():
                return cand
    which = shutil.which("ffprobe")
    return Path(which) if which else None


def require_ffmpeg() -> Path:
    p = find_ffmpeg()
    if not p:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg and ensure it is on PATH, "
            "or place ffmpeg.exe under vendor/, or set CLIPWORK_FFMPEG."
        )
    return p


def require_ffprobe() -> Path:
    p = find_ffprobe()
    if not p:
        # Fall back: some builds only ship ffmpeg; probe via ffmpeg -i is worse
        raise RuntimeError(
            "ffprobe not found. Install ffmpeg (includes ffprobe) or set CLIPWORK_FFPROBE."
        )
    return p


def request_cancel() -> None:
    """Ask any in-flight ffmpeg process to stop."""
    global _CANCEL_REQUESTED, _CURRENT_PROC
    _CANCEL_REQUESTED = True
    with _PROC_LOCK:
        proc = _CURRENT_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass


def clear_cancel() -> None:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = False


def cancel_requested() -> bool:
    return _CANCEL_REQUESTED


class CancelledError(RuntimeError):
    """Raised when the user cancels an ffmpeg job."""


def run_ffmpeg(
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    on_progress: Callable[[float, str], None] | None = None,
    duration_hint: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg; supports cancel via request_cancel() and optional progress."""
    global _CURRENT_PROC
    clear_cancel()
    ffmpeg = str(require_ffmpeg())
    # Always enable progress pipe when callback provided
    if on_progress is not None:
        cmd = [ffmpeg, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats", *args]
    else:
        cmd = [ffmpeg, "-hide_banner", "-y", *args]

    creation = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if on_progress else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation,
    )
    with _PROC_LOCK:
        _CURRENT_PROC = proc

    stdout_data = ""
    try:
        if on_progress and proc.stdout is not None:
            out_time_ms = 0
            for line in proc.stdout:
                if _CANCEL_REQUESTED:
                    proc.kill()
                    break
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        out_time_ms = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                    if duration_hint and duration_hint > 0:
                        t = out_time_ms / 1_000_000.0
                        on_progress(min(0.99, max(0.0, t / duration_hint)), f"{t:.1f}s")
                elif line == "progress=end":
                    on_progress(1.0, "done")
            proc.wait(timeout=timeout)
        else:
            try:
                _, err = proc.communicate(timeout=timeout)
                stdout_data = err or ""
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(f"ffmpeg timed out: {' '.join(args[:8])}…") from exc
    finally:
        with _PROC_LOCK:
            if _CURRENT_PROC is proc:
                _CURRENT_PROC = None

    if _CANCEL_REQUESTED:
        raise CancelledError("Export cancelled")

    stderr = ""
    if proc.stderr and not stdout_data:
        try:
            stderr = proc.stderr.read() or ""
        except Exception:
            stderr = ""
    elif stdout_data:
        stderr = stdout_data

    if check and proc.returncode not in (0, None) and proc.returncode != 0:
        # cancelled processes may be non-zero
        if _CANCEL_REQUESTED:
            raise CancelledError("Export cancelled")
        tail = (stderr or "").strip()
        tail = tail[-2000:] if len(tail) > 2000 else tail
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")

    return subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout="", stderr=stderr)


def probe(path: Path | str) -> dict[str, Any]:
    """Return ffprobe JSON for a media file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    ffprobe = str(require_ffprobe())
    cmd = [
        ffprobe,
        "-hide_banner",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(proc.stderr or '')[-500:]}")
    return json.loads(proc.stdout or "{}")


def media_summary(path: Path | str) -> str:
    """Human one-liner for UI/CLI."""
    path = Path(path)
    data = probe(path)
    fmt = data.get("format") or {}
    duration = fmt.get("duration")
    size = fmt.get("size")
    format_name = fmt.get("format_long_name") or fmt.get("format_name") or "?"
    v = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    parts = [path.name, format_name]
    if duration:
        try:
            parts.append(f"{float(duration):.1f}s")
        except (TypeError, ValueError):
            parts.append(f"{duration}s")
    if size:
        try:
            parts.append(f"{int(size) // 1024} KB")
        except (TypeError, ValueError):
            pass
    if v:
        w, h = v.get("width"), v.get("height")
        if w and h:
            parts.append(f"{w}x{h}")
        if v.get("codec_name"):
            parts.append(f"v:{v['codec_name']}")
    if a and a.get("codec_name"):
        parts.append(f"a:{a['codec_name']}")
    return " · ".join(parts)


def unique_path(path: Path) -> Path:
    """Never overwrite: add _1, _2, …"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        cand = parent / f"{stem}_{n}{suffix}"
        if not cand.exists():
            return cand
        n += 1


def default_output(src: Path, suffix: str, tag: str = "") -> Path:
    src = Path(src)
    mid = f"_{tag}" if tag else ""
    return unique_path(src.with_name(f"{src.stem}{mid}{suffix}"))
