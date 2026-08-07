"""Async preview frame extract + LRU cache (offline only).

Preview-only path. Export never imports this for encodes.
- ffmpeg still with optional ``-hwaccel auto``
- Background worker; only the latest seek request is served
- LRU cache keyed by rounded time
"""

from __future__ import annotations

import collections
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from sekiclip.media_ops.ffmpeg_util import find_ffmpeg

PREVIEW_DECODE_MAX_EDGE = 1280
DEFAULT_CACHE_SIZE = 48
SEEK_QUANTUM = 0.05  # seconds — cache bucket size
EXTRACT_TIMEOUT = 6.0


def _creation_flags() -> int:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0


def quantize_time(t: float, quantum: float = SEEK_QUANTUM) -> float:
    q = max(0.01, float(quantum))
    return round(max(0.0, float(t)) / q) * q


class AsyncFrameCache:
    """Thread-safe async frame provider for scrub/preview."""

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_CACHE_SIZE,
        max_edge: int = PREVIEW_DECODE_MAX_EDGE,
        use_hwaccel: bool = True,
    ) -> None:
        self.max_size = max(8, int(max_size))
        self.max_edge = int(max_edge)
        self.use_hwaccel = bool(use_hwaccel)
        self.path: Path | None = None
        self._cache: collections.OrderedDict[float, Image.Image] = collections.OrderedDict()
        self._lock = threading.Lock()
        self._req_t: float | None = None
        self._req_gen = 0
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._stop = False
        self.on_frame: Callable[[Image.Image, float], None] | None = None
        self.metrics: dict[str, int | float | str] = {
            "hits": 0,
            "misses": 0,
            "extracts": 0,
            "last_source": "none",
            "hwaccel": "auto" if use_hwaccel else "off",
        }
        self._worker = threading.Thread(target=self._loop, daemon=True, name="preview-frames")
        self._worker.start()

    def open(self, path: Path | None) -> None:
        with self._lock:
            self.path = Path(path) if path else None
            self._cache.clear()
            self._req_t = None
            self.metrics["hits"] = 0
            self.metrics["misses"] = 0
            self.metrics["extracts"] = 0
            self.metrics["last_source"] = "none"

    def close(self) -> None:
        self._stop = True
        self._wake.set()
        with self._lock:
            self._cache.clear()
            self.path = None

    def get_cached(self, t: float) -> Image.Image | None:
        key = quantize_time(t)
        with self._lock:
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
                self.metrics["hits"] = int(self.metrics["hits"]) + 1
                self.metrics["last_source"] = "cache"
                return img.copy()
            self.metrics["misses"] = int(self.metrics["misses"]) + 1
            return None

    def request(self, t: float, *, force: bool = False) -> Image.Image | None:
        """Return cache hit immediately; otherwise schedule async extract.

        ``on_frame`` is called on the worker when a new frame is ready.
        """
        key = quantize_time(t)
        hit = self.get_cached(key)
        if hit is not None and not force:
            return hit
        with self._lock:
            self._req_t = key
            self._req_gen += 1
        self._wake.set()
        return None

    def extract_sync(self, t: float) -> Image.Image | None:
        """Blocking extract (e.g. mouse-up finalize). Uses cache when possible."""
        key = quantize_time(t)
        hit = self.get_cached(key)
        if hit is not None:
            return hit
        img = self._extract(key)
        if img is not None:
            self._put(key, img)
        return img.copy() if img is not None else None

    def prefetch_around(self, t: float, *, radius: float = 0.5, step: float = 0.2) -> None:
        """Best-effort: queue neighbor times for cache (does not block)."""
        times = [t]
        r = max(0.1, float(radius))
        s = max(0.05, float(step))
        x = s
        while x <= r:
            times.append(t + x)
            times.append(max(0.0, t - x))
            x += s
        for tt in times:
            key = quantize_time(tt)
            with self._lock:
                if key in self._cache:
                    continue
            # Only schedule if idle-ish; latest request wins in worker
            self.request(key)

    def _put(self, key: float, img: Image.Image) -> None:
        with self._lock:
            self._cache[key] = img
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)

    def _loop(self) -> None:
        while not self._stop:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if self._stop:
                break
            with self._lock:
                t = self._req_t
                gen = self._req_gen
                path = self.path
            if t is None or path is None:
                continue
            # coalesce: if a newer request arrives mid-extract, drop result
            img = self._extract(t)
            with self._lock:
                if gen != self._req_gen:
                    continue  # superseded
            if img is None:
                continue
            self._put(t, img)
            cb = self.on_frame
            if cb is not None:
                try:
                    cb(img.copy(), t)
                except Exception:
                    pass

    def _extract(self, t: float) -> Image.Image | None:
        if not self.path or not self.path.is_file():
            return None
        ff = find_ffmpeg()
        if not ff:
            return None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                out = Path(tmp.name)
            scale = f"scale='min({self.max_edge}\\,iw)':-2"
            cmd: list[str] = [str(ff), "-hide_banner", "-loglevel", "error"]
            if self.use_hwaccel:
                cmd.extend(["-hwaccel", "auto"])
            cmd.extend(
                [
                    "-ss",
                    f"{max(0.0, t):.3f}",
                    "-i",
                    str(self.path),
                    "-frames:v",
                    "1",
                    "-vf",
                    scale,
                    "-q:v",
                    "5",
                    "-y",
                    str(out),
                ]
            )
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=EXTRACT_TIMEOUT,
                creationflags=_creation_flags(),
            )
            # If hwaccel failed, retry once without it
            if (proc.returncode != 0 or not out.is_file() or out.stat().st_size < 32) and self.use_hwaccel:
                out.unlink(missing_ok=True)
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    out = Path(tmp.name)
                cmd2 = [
                    str(ff),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{max(0.0, t):.3f}",
                    "-i",
                    str(self.path),
                    "-frames:v",
                    "1",
                    "-vf",
                    scale,
                    "-q:v",
                    "5",
                    "-y",
                    str(out),
                ]
                proc = subprocess.run(
                    cmd2,
                    capture_output=True,
                    timeout=EXTRACT_TIMEOUT,
                    creationflags=_creation_flags(),
                )
                self.metrics["hwaccel"] = "fallback-cpu"
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 32:
                out.unlink(missing_ok=True)
                return None
            img = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            self.metrics["extracts"] = int(self.metrics["extracts"]) + 1
            self.metrics["last_source"] = "ffmpeg"
            return img
        except Exception:
            return None
