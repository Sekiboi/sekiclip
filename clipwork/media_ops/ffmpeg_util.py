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
    """Run ffmpeg; supports cancel via request_cancel() and optional progress.

    Important: stderr is drained on a side thread so the process cannot deadlock
    when the stderr pipe fills (common hang: progress stuck mid-export).
    """
    global _CURRENT_PROC
    clear_cancel()
    ffmpeg = str(require_ffmpeg())
    # progress on stdout (pipe:1); keep stderr separate and always drain it
    if on_progress is not None:
        cmd = [ffmpeg, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats", *args]
    else:
        cmd = [ffmpeg, "-hide_banner", "-y", *args]

    creation = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if on_progress else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation,
    )
    with _PROC_LOCK:
        _CURRENT_PROC = proc

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        try:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                stderr_chunks.append(line)
                if len(stderr_chunks) > 400:
                    # keep tail only
                    del stderr_chunks[:-200]
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    try:
        if on_progress and proc.stdout is not None:
            for line in proc.stdout:
                if _CANCEL_REQUESTED:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        out_time_ms = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    t = out_time_ms / 1_000_000.0
                    if duration_hint and duration_hint > 0:
                        frac = min(0.99, max(0.0, t / duration_hint))
                        pct = int(frac * 100)
                        on_progress(frac, f"{pct}% · {t:.1f}s / {duration_hint:.1f}s")
                    else:
                        on_progress(min(0.99, t / max(t, 1.0)), f"{t:.1f}s")
                elif line.startswith("progress=") and line.endswith("end"):
                    on_progress(1.0, "100% · done")
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(f"ffmpeg timed out: {' '.join(args[:8])}…") from exc
        else:
            # Drain stdout too (may be empty) while stderr thread runs
            try:
                out, _ = proc.communicate(timeout=timeout)
                # stderr already in thread; join below
                _ = out
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                raise RuntimeError(f"ffmpeg timed out: {' '.join(args[:8])}…") from exc
    finally:
        try:
            err_thread.join(timeout=2.0)
        except Exception:
            pass
        with _PROC_LOCK:
            if _CURRENT_PROC is proc:
                _CURRENT_PROC = None

    if _CANCEL_REQUESTED:
        raise CancelledError("Export cancelled")

    stderr = "".join(stderr_chunks)

    if check and proc.returncode not in (0, None) and proc.returncode != 0:
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
    """Never overwrite: add _1, _2, … (auto-generated names only)."""
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


def output_path(
    dest: Path | str | None,
    src: Path | str,
    *,
    suffix: str,
    tag: str = "",
) -> Path:
    """Resolve write path.

    Explicit ``dest`` is used as-is so the user can replace an existing file.
    When ``dest`` is omitted, invent a unique name next to the source.
    """
    if dest is not None and str(dest).strip():
        return Path(dest)
    return default_output(Path(src), suffix, tag)


def paths_same(a: Path | str | None, b: Path | str | None) -> bool:
    """True if both paths exist as the same file (best-effort on Windows)."""
    if a is None or b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        try:
            return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
                os.path.abspath(str(b))
            )
        except OSError:
            return Path(a) == Path(b)


def staging_path(final: Path | str) -> Path:
    """Sibling temp file for safe encode-then-replace (same folder as final)."""
    final = Path(final)
    # Hidden-ish name so it sorts near the original; same suffix for ffmpeg muxers
    return final.with_name(f".{final.stem}.clipwork_tmp{final.suffix or '.mp4'}")


def commit_staged(staged: Path | str, final: Path | str) -> Path:
    """Atomically replace ``final`` with the staged encode result."""
    import os as _os

    staged = Path(staged)
    final = Path(final)
    if not staged.is_file():
        raise RuntimeError(f"Encode produced no output: {staged.name}")
    final.parent.mkdir(parents=True, exist_ok=True)
    # os.replace is atomic on the same volume (Windows + POSIX)
    _os.replace(str(staged), str(final))
    return final
