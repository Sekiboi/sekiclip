"""Visual media session: video frames, audio waveform, scrub position.

Uses OpenCV for video, ffmpeg for waveforms / audio duration fallback, Pillow for images.
Offline only — no network.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from clipwork.media_ops.ffmpeg_util import find_ffmpeg, find_ffprobe, probe, require_ffmpeg

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg", ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    UNKNOWN = "unknown"


def classify(path: Path) -> MediaKind:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return MediaKind.VIDEO
    if ext in AUDIO_EXTS:
        return MediaKind.AUDIO
    if ext in IMAGE_EXTS:
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def format_time(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        seconds = 0.0
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    ms = int((seconds - s) * 100)
    if h:
        return f"{h}:{m:02d}:{sec:02d}.{ms:02d}"
    return f"{m:02d}:{sec:02d}.{ms:02d}"


@dataclass
class MediaInfo:
    path: Path
    kind: MediaKind
    duration: float
    width: int
    height: int
    has_audio: bool
    has_video: bool
    summary: str


def load_info(path: Path) -> MediaInfo:
    kind = classify(path)
    duration = 0.0
    width = height = 0
    has_audio = has_video = False
    summary = path.name
    if kind == MediaKind.IMAGE:
        with Image.open(path) as im:
            width, height = im.size
        summary = f"{path.name} · {width}×{height}"
        return MediaInfo(path, kind, 0.0, width, height, False, False, summary)
    try:
        data = probe(path)
    except Exception as exc:  # noqa: BLE001
        return MediaInfo(path, kind, 0.0, 0, 0, False, False, f"{path.name} · {exc}")
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not has_video:
            has_video = True
            width = int(s.get("width") or 0)
            height = int(s.get("height") or 0)
        if s.get("codec_type") == "audio":
            has_audio = True
    if has_video and kind == MediaKind.UNKNOWN:
        kind = MediaKind.VIDEO
    elif has_audio and not has_video and kind == MediaKind.UNKNOWN:
        kind = MediaKind.AUDIO
    parts = [path.name, format_time(duration)]
    if width and height:
        parts.append(f"{width}×{height}")
    summary = " · ".join(parts)
    return MediaInfo(path, kind, duration, width, height, has_audio, has_video, summary)


class MediaSession:
    """Open one file for visual scrubbing / playback / in-out selection."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.info: MediaInfo | None = None
        self.position: float = 0.0
        self.in_point: float = 0.0
        self.out_point: float | None = None  # None = end
        self._cap = None  # cv2.VideoCapture
        self._image: Image.Image | None = None
        self._waveform: Image.Image | None = None
        self._playing = False
        self._play_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.on_frame: Callable[[Image.Image | None, float], None] | None = None
        self.on_position: Callable[[float], None] | None = None

    @property
    def duration(self) -> float:
        return float(self.info.duration) if self.info else 0.0

    @property
    def out_or_end(self) -> float:
        if self.out_point is not None:
            return min(self.out_point, self.duration or self.out_point)
        return self.duration

    def close(self) -> None:
        self.stop()
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._image = None
            self._waveform = None
            self.path = None
            self.info = None

    def open(self, path: Path) -> MediaInfo:
        self.close()
        path = Path(path)
        info = load_info(path)
        self.path = path
        self.info = info
        self.position = 0.0
        self.in_point = 0.0
        self.out_point = info.duration if info.duration > 0 else None

        if info.kind == MediaKind.IMAGE:
            self._image = Image.open(path).convert("RGB")
            self._emit_frame(self._image.copy(), 0.0)
        elif info.kind == MediaKind.VIDEO or info.has_video:
            self._open_video(path)
            self.seek(0.0)
            if info.has_audio or info.kind == MediaKind.VIDEO:
                self._waveform = self._build_waveform(path, info.duration)
        elif info.kind == MediaKind.AUDIO or info.has_audio:
            self._waveform = self._build_waveform(path, info.duration)
            self._emit_frame(self._waveform_frame(), 0.0)
        else:
            self._emit_frame(self._placeholder("Unsupported file"), 0.0)
        return info

    def _open_video(self, path: Path) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "opencv-python is required for video preview. pip install opencv-python-headless"
            ) from exc
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video for preview: {path.name}")
        self._cap = cap
        # Prefer ffprobe duration; fallback to capture
        if self.info and self.info.duration <= 0:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            if fps > 0 and frames > 0:
                self.info.duration = float(frames / fps)
                self.out_point = self.info.duration

    def _placeholder(self, text: str, size: tuple[int, int] = (640, 360)) -> Image.Image:
        img = Image.new("RGB", size, (28, 28, 32))
        d = ImageDraw.Draw(img)
        d.text((24, size[1] // 2 - 10), text, fill=(200, 200, 210))
        return img

    def _waveform_frame(self) -> Image.Image:
        if self._waveform is not None:
            return self._waveform.copy()
        return self._placeholder("Audio — generating waveform…")

    def _build_waveform(self, path: Path, duration: float) -> Image.Image | None:
        ff = find_ffmpeg()
        if not ff:
            return self._placeholder("ffmpeg needed for waveform")
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                out = Path(tmp.name)
            # showwavespic: single image overview
            cmd = [
                str(ff),
                "-hide_banner",
                "-y",
                "-i",
                str(path),
                "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=800x160:colors=3B82F6",
                "-frames:v",
                "1",
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode != 0 or not out.is_file():
                out.unlink(missing_ok=True)
                return self._placeholder("Waveform unavailable")
            img = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            # Draw in/out friendly canvas
            canvas = Image.new("RGB", (800, 200), (24, 24, 28))
            canvas.paste(img, (0, 20))
            return canvas
        except Exception:
            return self._placeholder("Waveform failed")

    def _emit_frame(self, img: Image.Image | None, t: float) -> None:
        if self.on_frame:
            try:
                self.on_frame(img, t)
            except Exception:
                pass
        if self.on_position:
            try:
                self.on_position(t)
            except Exception:
                pass

    def seek(self, seconds: float) -> None:
        with self._lock:
            if not self.info:
                return
            dur = self.duration
            if dur > 0:
                seconds = max(0.0, min(float(seconds), dur))
            else:
                seconds = max(0.0, float(seconds))
            self.position = seconds
            if self.info.kind == MediaKind.IMAGE and self._image is not None:
                self._emit_frame(self._image.copy(), 0.0)
                return
            if self._cap is not None:
                import cv2

                self._cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
                ok, frame = self._cap.read()
                if ok and frame is not None:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(rgb)
                    self._emit_frame(img, seconds)
                    return
            if self.info.kind == MediaKind.AUDIO or (
                self.info.has_audio and not self.info.has_video
            ):
                img = self._waveform_frame()
                # playhead line
                if img and dur > 0:
                    img = img.copy()
                    d = ImageDraw.Draw(img)
                    x = int((seconds / dur) * (img.width - 1))
                    d.line([(x, 0), (x, img.height)], fill=(255, 220, 80), width=2)
                self._emit_frame(img, seconds)

    def set_in(self, t: float | None = None) -> float:
        t = self.position if t is None else float(t)
        self.in_point = max(0.0, t)
        if self.out_point is not None and self.out_point <= self.in_point:
            self.out_point = min(self.duration, self.in_point + 0.1) if self.duration else self.in_point + 0.1
        return self.in_point

    def set_out(self, t: float | None = None) -> float:
        t = self.position if t is None else float(t)
        self.out_point = max(self.in_point + 0.05, t)
        if self.duration > 0:
            self.out_point = min(self.out_point, self.duration)
        return self.out_point

    def clear_in_out(self) -> None:
        self.in_point = 0.0
        self.out_point = self.duration if self.duration > 0 else None

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        if self._playing or not self.info:
            return
        if self.info.kind == MediaKind.IMAGE:
            return
        self._playing = True
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._play_thread.start()

    def stop(self) -> None:
        self._playing = False
        t = self._play_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._play_thread = None

    def toggle_play(self) -> None:
        if self._playing:
            self.stop()
        else:
            self.play()

    def _play_loop(self) -> None:
        end = self.out_or_end if self.out_or_end > 0 else 1e9
        last = time.perf_counter()
        while self._playing:
            now = time.perf_counter()
            dt = now - last
            last = now
            nxt = self.position + dt
            if nxt >= end:
                self.position = end if end < 1e9 else self.position
                self.seek(self.position)
                self._playing = False
                break
            self.seek(nxt)
            time.sleep(1 / 30)

    def current_preview_image(self) -> Image.Image | None:
        """Best-effort current frame for UI."""
        if self.info and self.info.kind == MediaKind.IMAGE and self._image:
            return self._image.copy()
        if self._waveform and self.info and (
            self.info.kind == MediaKind.AUDIO or (self.info.has_audio and not self.info.has_video)
        ):
            return self._waveform_frame()
        return None


def run_ffmpeg_with_progress(
    args: list[str],
    *,
    duration_hint: float | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    timeout: float | None = None,
) -> None:
    """Run ffmpeg with -progress pipe:1 and report 0..1 progress."""
    ffmpeg = str(require_ffmpeg())
    cmd = [ffmpeg, "-hide_banner", "-y", "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    out_time_ms = 0
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    out_time_ms = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                if on_progress and duration_hint and duration_hint > 0:
                    t = out_time_ms / 1_000_000.0
                    on_progress(min(0.99, max(0.0, t / duration_hint)), format_time(t))
            elif line == "progress=end":
                if on_progress:
                    on_progress(1.0, "done")
        rc = proc.wait(timeout=timeout)
    except Exception:
        proc.kill()
        raise
    if rc != 0:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(f"ffmpeg failed ({rc}): {err[-1500:]}")
