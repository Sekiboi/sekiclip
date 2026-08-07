"""Visual media session: smooth video frames, audio playback, waveform scrub.

Play clock:
  - **Master** = preview audio sample clock when PCM engine is active;
    otherwise wall clock. Video decoder *follows* the master.
  - Audio: ``PreviewAudioEngine`` (ffmpeg → PCM → sounddevice); ffplay fallback.
  - Scrub: video first; optional short audio blip.
  - Frames: OpenCV + async ffmpeg LRU cache (hwaccel on preview only).
  - Export never uses this module — ``render_cut`` is authoritative.

Images: Pillow stills. Offline only.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from sekiclip.media_ops.ffmpeg_util import find_ffmpeg, format_bytes, probe, require_ffmpeg
from sekiclip.preview.audio import PreviewAudioEngine
from sekiclip.preview.frames import AsyncFrameCache, PREVIEW_DECODE_MAX_EDGE as _FRAME_EDGE
from sekiclip.preview.match import CutTimeline, build_audio_filter

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg", ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# ── Preview pipeline — never used by export / render_cut ──────────
PREVIEW_DECODE_MAX_EDGE = _FRAME_EDGE
PREVIEW_IMAGE_MAX_EDGE = 2048
PREVIEW_FFMPEG_SEEK_TIMEOUT = 8.0
PREVIEW_SCRUB_AUDIO = True  # short blip while scrubbing when idle


def find_ffplay() -> Path | None:
    """Locate ffplay next to ffmpeg or on PATH."""
    import os

    env = os.environ.get("SEKICLIP_FFPLAY") or os.environ.get("FFPLAY")
    if env and Path(env).is_file():
        return Path(env)
    which = shutil.which("ffplay")
    if which:
        return Path(which)
    ff = find_ffmpeg()
    if ff:
        cand = ff.with_name("ffplay.exe" if ff.suffix.lower() == ".exe" else "ffplay")
        if cand.is_file():
            return cand
        # winget shims often sit beside ffmpeg.exe
        sibling = ff.parent / ("ffplay.exe" if os.name == "nt" else "ffplay")
        if sibling.is_file():
            return sibling
    return None


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
    fps: float = 30.0


def load_info(path: Path) -> MediaInfo:
    kind = classify(path)
    duration = 0.0
    width = height = 0
    has_audio = has_video = False
    fps = 30.0
    summary = path.name
    if kind == MediaKind.IMAGE:
        with Image.open(path) as im:
            width, height = im.size
        try:
            sz = format_bytes(path.stat().st_size)
            summary = f"{path.name} · {width}×{height} · {sz}"
        except OSError:
            summary = f"{path.name} · {width}×{height}"
        return MediaInfo(path, kind, 0.0, width, height, False, False, summary, 0.0)
    try:
        data = probe(path)
    except Exception as exc:  # noqa: BLE001
        return MediaInfo(path, kind, 0.0, 0, 0, False, False, f"{path.name} · {exc}", 30.0)
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
            # avg_frame_rate like "30000/1001"
            afr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1"
            try:
                if isinstance(afr, str) and "/" in afr:
                    num, den = afr.split("/", 1)
                    den_f = float(den) or 1.0
                    fps = max(1.0, float(num) / den_f)
                else:
                    fps = max(1.0, float(afr))
            except (TypeError, ValueError, ZeroDivisionError):
                fps = 30.0
        if s.get("codec_type") == "audio":
            has_audio = True
    if has_video and kind == MediaKind.UNKNOWN:
        kind = MediaKind.VIDEO
    elif has_audio and not has_video and kind == MediaKind.UNKNOWN:
        kind = MediaKind.AUDIO
    parts = [path.name, format_time(duration)]
    if width and height:
        parts.append(f"{width}×{height}")
    if has_video and fps:
        parts.append(f"{fps:.2f}fps")
    try:
        fsize = path.stat().st_size
        if fsize > 0:
            parts.append(format_bytes(fsize))
    except OSError:
        pass
    summary = " · ".join(parts)
    return MediaInfo(path, kind, duration, width, height, has_audio, has_video, summary, fps)


class MediaSession:
    """Open one file for scrubbing, playback with audio, and in-out selection."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.info: MediaInfo | None = None
        self.position: float = 0.0
        self.in_point: float = 0.0
        self.out_point: float | None = None
        self._cap = None
        self._fps: float = 30.0
        self._image: Image.Image | None = None
        self._waveform: Image.Image | None = None
        self._ffplay_proc: subprocess.Popen[str] | None = None  # legacy fallback only
        self._playing = False
        self._loop_selection = False
        self._play_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._play_gen = 0  # bump to cancel in-flight play
        self.on_frame: Callable[[Image.Image | None, float], None] | None = None
        self.on_position: Callable[[float], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        # Live preview A/V looks (must match export render_cut settings)
        self.preview_volume: float = 1.0
        self.preview_mute: bool = False
        self.preview_audio_fade_in: float = 0.0
        self.preview_audio_fade_out: float = 0.0
        self.preview_speed: float = 1.0
        self.scrub_audio_enabled: bool = PREVIEW_SCRUB_AUDIO
        self.use_scrub_proxy: bool = False  # optional low-res file for OpenCV scrub
        self._proxy_path: Path | None = None
        self._play_start_at: float = 0.0
        self._play_end_at: float = 0.0
        self._play_mode: str = "to_out"  # "to_out" | "selection"
        self._audio_restarts_this_seg: int = 0
        self._cap_reopen_attempts: int = 0
        self._ffmpeg_frame_cache: tuple[float, Image.Image] | None = None
        # Preview engines (PCM audio + async frames)
        self._audio = PreviewAudioEngine()
        self._audio.on_status = lambda m: self._status(m)
        self._frames = AsyncFrameCache(max_edge=PREVIEW_DECODE_MAX_EDGE, use_hwaccel=True)
        self._frames.on_frame = self._on_async_frame
        self._last_scrub_blip = 0.0
        self._preview_metrics: dict[str, object] = {}

    @property
    def duration(self) -> float:
        return float(self.info.duration) if self.info else 0.0

    @property
    def play_mode_label(self) -> str:
        if self._play_mode == "selection":
            return "Loop cut (In→Out)"
        return "Play → Out"

    def video_capture_ok(self) -> bool:
        if not self.info or not self.info.has_video:
            return True
        cap = self._cap
        if cap is None:
            return False
        try:
            return bool(cap.isOpened())
        except Exception:
            return False

    def ensure_legal_marks(self) -> list[str]:
        """Clamp In/Out/playhead to a legal state. Returns human fix notes."""
        notes: list[str] = []
        dur = self.duration
        if dur <= 0:
            if self.in_point < 0:
                self.in_point = 0.0
                notes.append("clamped In ≥ 0")
            if self.position < 0:
                self.position = 0.0
                notes.append("clamped playhead ≥ 0")
            return notes

        inn = float(self.in_point)
        if inn < 0.0:
            self.in_point = 0.0
            notes.append("clamped In to 0")
            inn = 0.0
        if inn >= dur:
            self.in_point = max(0.0, dur - 0.05)
            notes.append("clamped In before end")
            inn = self.in_point

        out = self.out_point if self.out_point is not None else dur
        out = float(out)
        if out > dur:
            self.out_point = dur
            notes.append("clamped Out to duration")
            out = dur
        if out <= inn:
            self.out_point = min(dur, inn + 0.1)
            notes.append("fixed Out ≤ In")
            out = float(self.out_point)

        pos = float(self.position)
        if pos < 0.0 or pos > dur:
            self.position = max(0.0, min(pos, dur))
            notes.append("clamped playhead to media")
        return notes

    def try_reopen_video(self) -> bool:
        """Re-open OpenCV capture if it died (same path)."""
        if not self.path or not self.info or not self.info.has_video:
            return False
        if self._cap_reopen_attempts >= 2:
            return False
        self._cap_reopen_attempts += 1
        try:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._open_video(self.path)
            # Seek to current playhead
            self.seek(self.position, emit=False, stop_playback=False)
            return self.video_capture_ok()
        except Exception:
            return False

    @property
    def out_or_end(self) -> float:
        if self.out_point is not None:
            return min(self.out_point, self.duration or self.out_point)
        return self.duration

    @property
    def has_preview_audio(self) -> bool:
        return self._audio.available or find_ffplay() is not None

    def preview_metrics(self) -> dict[str, object]:
        """Debug/ops metrics for preview pipeline (not export)."""
        m: dict[str, object] = {
            "audio": self._audio.metrics(),
            "frames": dict(self._frames.metrics),
            "play_mode": self._play_mode,
            "position": round(self.position, 3),
        }
        self._preview_metrics = m
        return m

    def _on_async_frame(self, img: Image.Image, t: float) -> None:
        """Worker callback: only paint if still near the requested scrub time."""
        if abs(float(t) - float(self.position)) > 0.12:
            return
        if self._playing:
            return
        self._emit_frame(img, float(self.position))

    def close(self) -> None:
        self.stop()
        self._stop_audio()
        try:
            self._audio.close()
        except Exception:
            pass
        try:
            self._frames.close()
        except Exception:
            pass
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
        # Recreate engines after close()
        self._audio = PreviewAudioEngine()
        self._audio.on_status = lambda m: self._status(m)
        self._frames = AsyncFrameCache(max_edge=PREVIEW_DECODE_MAX_EDGE, use_hwaccel=True)
        self._frames.on_frame = self._on_async_frame
        path = Path(path)
        info = load_info(path)
        self.path = path
        self.info = info
        self.position = 0.0
        self.in_point = 0.0
        self.out_point = info.duration if info.duration > 0 else None
        self._fps = info.fps if info.fps and info.fps > 1 else 30.0
        self._cap_reopen_attempts = 0
        self._audio_restarts_this_seg = 0
        self._proxy_path = None
        self._audio.open(path)
        self._frames.open(path)
        eng = "pcm" if self._audio.available else ("ffplay" if find_ffplay() else "none")
        self._status(f"Preview audio engine: {eng}")

        if info.kind == MediaKind.IMAGE:
            self._image = self._load_preview_image(path)
            self._emit_frame(self._image.copy(), 0.0)
        elif info.kind == MediaKind.VIDEO or info.has_video:
            open_path = path
            if self.use_scrub_proxy:
                try:
                    from sekiclip.media_ops.proxy import ensure_scrub_proxy

                    self._status("Building scrub proxy (optional, once)…")
                    px = ensure_scrub_proxy(path)
                    if px is not None:
                        self._proxy_path = px
                        open_path = px
                        self._status(f"Scrub proxy ready · {px.name}")
                except Exception:
                    self._proxy_path = None
            self._open_video(open_path)
            self.seek(0.0, emit=True)
            # Prefetch around start (best-effort)
            try:
                self._frames.prefetch_around(0.0, radius=1.0, step=0.25)
            except Exception:
                pass
            if info.has_audio and not self._audio.available and not find_ffplay():
                self._status("Preview audio unavailable (install sounddevice or ffplay)")
            threading.Thread(
                target=self._bg_waveform, args=(path, info.duration), daemon=True
            ).start()
        elif info.kind == MediaKind.AUDIO or info.has_audio:
            if not self._audio.available and not find_ffplay():
                self._status("Preview audio unavailable")
            self._waveform = self._placeholder("Building waveform…")
            self._emit_frame(self._waveform_frame_at(0.0), 0.0)
            threading.Thread(
                target=self._bg_waveform, args=(path, info.duration), daemon=True
            ).start()
        else:
            self._emit_frame(self._placeholder("Unsupported file"), 0.0)
        return info

    def _load_preview_image(self, path: Path) -> Image.Image:
        """Load still for preview; cap edge so multi‑MP images stay light in RAM."""
        with Image.open(path) as im:
            img = im.convert("RGB")
        w, h = img.size
        edge = max(w, h)
        if edge > PREVIEW_IMAGE_MAX_EDGE:
            img.thumbnail(
                (PREVIEW_IMAGE_MAX_EDGE, PREVIEW_IMAGE_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
        return img

    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _open_video(self, path: Path) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "opencv-python is required for video preview. pip install opencv-python-headless"
            ) from exc
        # Prefer FFmpeg backend when available (HW where OpenCV/ffmpeg support it)
        cap = None
        try:
            cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        except Exception:
            cap = None
        if cap is None or not cap.isOpened():
            cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video for preview: {path.name}")
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # Best-effort HW acceleration flag (ignored if unsupported)
        try:
            if hasattr(cv2, "CAP_PROP_HW_ACCELERATION"):
                cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        except Exception:
            pass
        self._cap = cap
        cv_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if cv_fps > 1:
            self._fps = cv_fps
        if self.info and self.info.duration <= 0:
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            if self._fps > 0 and frames > 0:
                self.info.duration = float(frames / self._fps)
                self.out_point = self.info.duration
        if self.info and self.info.width and self.info.height:
            w, h = self.info.width, self.info.height
            if max(w, h) > PREVIEW_DECODE_MAX_EDGE:
                self._status(
                    f"Preview scaled for speed ({w}×{h} source · export stays full quality)"
                )

    def _placeholder(self, text: str, size: tuple[int, int] = (960, 540)) -> Image.Image:
        img = Image.new("RGB", size, (28, 28, 32))
        d = ImageDraw.Draw(img)
        d.text((24, size[1] // 2 - 10), text, fill=(200, 200, 210))
        return img

    def _bg_waveform(self, path: Path, duration: float) -> None:
        try:
            wf = self._build_waveform(path, duration)
            self._waveform = wf
            # Refresh audio-only preview if still showing this file
            if (
                self.path == path
                and self.info
                and (self.info.kind == MediaKind.AUDIO or (self.info.has_audio and not self.info.has_video))
                and not self._playing
            ):
                self._emit_frame(self._waveform_frame_at(self.position), self.position)
        except Exception:
            self._waveform = self._placeholder("Waveform failed")

    def _waveform_frame_at(self, seconds: float) -> Image.Image:
        if self._waveform is None:
            return self._placeholder("Audio waveform…")
        img = self._waveform.copy()
        dur = self.duration
        if dur > 0:
            d = ImageDraw.Draw(img)
            x = int((seconds / dur) * (img.width - 1))
            d.line([(x, 0), (x, img.height)], fill=(255, 220, 80), width=3)
            # In/out markers
            if self.in_point > 0:
                xi = int((self.in_point / dur) * (img.width - 1))
                d.line([(xi, 0), (xi, img.height)], fill=(80, 220, 120), width=2)
            if self.out_point is not None:
                xo = int((self.out_point / dur) * (img.width - 1))
                d.line([(xo, 0), (xo, img.height)], fill=(220, 100, 100), width=2)
        return img

    def _build_waveform(self, path: Path, duration: float) -> Image.Image:
        """Overview waveform for scrubbing. Fast path for multi-hour files.

        Downsamples audio heavily — visual only; export audio quality is unchanged.
        """
        ff = find_ffmpeg()
        if not ff:
            return self._placeholder("ffmpeg needed for waveform")
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                out = Path(tmp.name)
            dur = max(0.0, float(duration or 0))
            # Timeout scales with length but stays bounded (huge files must not hang forever)
            timeout = int(min(600, max(45, dur * 0.15 + 30)))
            # Low sample rate + mono = order-of-magnitude faster scan on long clips
            # For multi-hour media, also skip silence detection work
            cmd = [
                str(ff),
                "-hide_banner",
                "-y",
                "-threads",
                "0",
                "-i",
                str(path),
                "-filter_complex",
                "aformat=channel_layouts=mono,aresample=4000,showwavespic=s=960x200:colors=3B82F6",
                "-frames:v",
                "1",
                str(out),
            ]
            creation = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout, creationflags=creation
            )
            if proc.returncode != 0 or not out.is_file():
                out.unlink(missing_ok=True)
                return self._placeholder("Waveform unavailable")
            img = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            canvas = Image.new("RGB", (960, 240), (24, 24, 28))
            canvas.paste(img, (0, 20))
            return canvas
        except subprocess.TimeoutExpired:
            return self._placeholder("Waveform timed out (file very long)")
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

    def _frame_to_image(self, frame) -> Image.Image | None:
        """Convert OpenCV frame → RGB PIL, downscaled for preview only.

        Full-resolution frames from 4K/8K sources are expensive to convert and
        hold in RAM every scrub/play tick. Export never uses this path.
        """
        try:
            import cv2
            import numpy as np

            if frame is None:
                return None
            h, w = frame.shape[:2]
            edge = max(w, h)
            if edge > PREVIEW_DECODE_MAX_EDGE:
                scale = PREVIEW_DECODE_MAX_EDGE / float(edge)
                nw = max(2, int(w * scale) & ~1)
                nh = max(2, int(h * scale) & ~1)
                frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
            if frame.ndim == 2:
                rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)
            elif frame.shape[2] == 4:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb = np.ascontiguousarray(rgb)
            return Image.fromarray(rgb, mode="RGB")
        except Exception:
            return None

    def _cap_time(self) -> float:
        if self._cap is None:
            return self.position
        try:
            import cv2

            ms = float(self._cap.get(cv2.CAP_PROP_POS_MSEC) or 0)
            if ms > 0:
                return ms / 1000.0
        except Exception:
            pass
        return self.position

    def seek(
        self,
        seconds: float,
        *,
        emit: bool = True,
        stop_playback: bool = True,
    ) -> None:
        """Seek playhead to an exact source time.

        Always keeps ``position`` and emit time as the requested ``seconds``
        (never OpenCV CAP_PROP_POS_MSEC — that clock drifts and caused early fades).
        """
        if stop_playback and self._playing:
            self.stop()
        need_ffmpeg = False
        img: Image.Image | None = None
        painted = False
        with self._lock:
            if not self.info:
                return
            dur = self.duration
            if dur > 0:
                seconds = max(0.0, min(float(seconds), dur))
            else:
                seconds = max(0.0, float(seconds))
            # Authoritative playhead — do not overwrite with decoder time
            self.position = seconds

            if self.info.kind == MediaKind.IMAGE and self._image is not None:
                if emit:
                    self._emit_frame(self._image.copy(), 0.0)
                return

            if self._cap is not None:
                try:
                    import cv2
                except ImportError:
                    if emit:
                        self._emit_frame(
                            self._placeholder("OpenCV missing"),
                            seconds,
                        )
                    return
                # Frame index is more stable than MSEC for scrubbing
                if self._fps > 1:
                    frame_idx = int(round(seconds * self._fps))
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                else:
                    self._cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds * 1000.0))
                ok, frame = self._cap.read()
                img = self._frame_to_image(frame) if ok else None
                if not ok or img is None:
                    # Retry once with MSEC (some files mishandle frame seeks)
                    try:
                        self._cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds * 1000.0))
                        ok, frame = self._cap.read()
                        img = self._frame_to_image(frame) if ok else None
                    except Exception:
                        img = None
                if img is None and self.path is not None:
                    need_ffmpeg = True
                elif emit and img is not None:
                    self._emit_frame(img, seconds)
                    painted = True
            else:
                # Audio-only
                if emit:
                    self._emit_frame(self._waveform_frame_at(seconds), seconds)
                    painted = True

        # Async cache + sync finalize when OpenCV missed
        if need_ffmpeg:
            cached = self._frames.get_cached(seconds)
            if cached is not None:
                if emit:
                    self._emit_frame(cached, seconds)
                    painted = True
            else:
                self._frames.request(seconds)  # warm cache for neighbors
                img = self._frames.extract_sync(seconds)
                if img is None:
                    img = self._ffmpeg_preview_frame(seconds)
                if emit:
                    self._emit_frame(
                        img or self._placeholder(f"No frame @ {seconds:.2f}s"),
                        seconds,
                    )
                    painted = True
        if painted:
            try:
                self._frames.prefetch_around(seconds, radius=0.5, step=0.2)
            except Exception:
                pass

    def scrub_audio_at(self, seconds: float) -> None:
        """Optional short audio tick (call on scrub mouse-up, not every drag tick)."""
        if (
            not self.scrub_audio_enabled
            or self._playing
            or not self.info
            or not self.info.has_audio
            or self.preview_mute
        ):
            return
        now = time.perf_counter()
        if now - self._last_scrub_blip < 0.08:
            return
        self._last_scrub_blip = now
        try:
            self._audio.scrub_blip(float(seconds), duration=0.08)
        except Exception:
            pass

    def _ffmpeg_preview_frame(self, seconds: float) -> Image.Image | None:
        """Preview-only: grab one downscaled frame via ffmpeg (fast -ss).

        Used when OpenCV seek fails. Never used by export encode paths.
        """
        if not self.path or not self.path.is_file():
            return None
        # Reuse last frame if seek is essentially the same (avoid thrash)
        if self._ffmpeg_frame_cache is not None:
            ct, cimg = self._ffmpeg_frame_cache
            if abs(ct - seconds) < 0.04:
                return cimg.copy()
        ff = find_ffmpeg()
        if not ff:
            return None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                out = Path(tmp.name)
            # scale for preview only; export never hits this
            scale = (
                f"scale='min({PREVIEW_DECODE_MAX_EDGE}\\,iw)':-2"
            )
            cmd = [
                str(ff),
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, seconds):.3f}",
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
            creation = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=PREVIEW_FFMPEG_SEEK_TIMEOUT,
                creationflags=creation,
            )
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 32:
                out.unlink(missing_ok=True)
                return None
            img = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            self._ffmpeg_frame_cache = (float(seconds), img.copy())
            return img
        except Exception:
            return None

    def set_in(self, t: float | None = None) -> float:
        t = self.position if t is None else float(t)
        self.in_point = max(0.0, t)
        if self.out_point is not None and self.out_point <= self.in_point:
            self.out_point = (
                min(self.duration, self.in_point + 0.1) if self.duration else self.in_point + 0.1
            )
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

    def frame_step(self, delta: int = 1) -> float:
        """Step playhead by N frames (always pauses). Returns new time."""
        was = self._playing
        self.stop()
        fps = max(self._fps, 1.0)
        t = self.position + (delta / fps)
        # Stay inside media; do not jump past duration
        if self.duration > 0:
            t = max(0.0, min(t, self.duration))
        self.seek(t, emit=True)
        if was and self.on_status:
            try:
                self.on_status("Paused")
            except Exception:
                pass
        return self.position

    def play(self, *, selection_only: bool = False, loop: bool = False) -> None:
        """Start A+V from one source time. Wall clock is master (see module doc).

        Modes:
          - selection_only=False (**Play → Out**): playhead → Out, no loop
          - selection_only=True (**Loop cut**): In→Out, optional loop
        """
        if self._playing or not self.info:
            return
        if self.info.kind == MediaKind.IMAGE:
            return

        # Legal marks before arming clocks
        self.ensure_legal_marks()

        self._play_gen += 1
        gen = self._play_gen
        self._playing = True
        self._loop_selection = bool(loop)
        self._play_mode = "selection" if selection_only else "to_out"
        self._audio_restarts_this_seg = 0

        if selection_only:
            end_at = self.out_or_end if self.out_or_end > 0 else self.duration
            # Resume mid-cut when playhead is already inside In→Out
            if self.in_point <= self.position < end_at - 0.05:
                start_at = self.position
            else:
                start_at = self.in_point
            self.seek(start_at, emit=True)
        else:
            # Play → Out: from playhead to Out (export cut end)
            start_at = self.position
            end_at = self.out_or_end if self.out_or_end > 0 else self.duration
            if start_at >= end_at - 0.05:
                start_at = self.in_point
                self.seek(start_at, emit=True)

        # Shared t0 for audio + video threads (play loop starts both)
        self._play_thread = threading.Thread(
            target=self._play_loop, args=(gen, start_at, end_at), daemon=True
        )
        self._play_thread.start()

    def play_selection(self, *, loop: bool = True) -> None:
        """Loop the In→Out cut (review selection)."""
        self.play(selection_only=True, loop=loop)

    def stop(self) -> None:
        self._playing = False
        self._play_gen += 1
        self._loop_selection = False
        self._stop_audio()
        t = self._play_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.5)
        self._play_thread = None

    def toggle_play(self) -> None:
        if self._playing:
            self.stop()
        else:
            self.play()

    def _stop_audio(self) -> None:
        try:
            self._audio.stop()
        except Exception:
            pass
        proc = self._ffplay_proc
        self._ffplay_proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _clamp_play_window(self, start_seconds: float, end_seconds: float | None) -> tuple[float, float]:
        """Return source [start, end] for preview audio/video play."""
        start = max(0.0, float(start_seconds))
        dur = self.duration if self.duration > 0 else 0.0
        if end_seconds is None or end_seconds > 1e8:
            end = self.out_or_end if self.out_or_end > start else dur
        else:
            end = float(end_seconds)
        if dur > 0:
            end = min(end, dur)
            start = min(start, max(0.0, dur - 0.05))
        if end <= start:
            end = start + 0.05
        return start, end

    def _cut_timeline(self) -> CutTimeline:
        """In→Out cut used for fades/play — always live marks (same bounds as export)."""
        inn = max(0.0, float(self.in_point))
        outp = self.out_or_end if self.out_or_end > 0 else max(inn + 0.05, self.duration)
        outp = max(inn + 0.05, float(outp))
        return CutTimeline(
            in_point=inn,
            out_point=outp,
            speed=max(0.25, min(4.0, float(self.preview_speed))),
        )

    def _start_audio(self, start_seconds: float, end_seconds: float | None = None) -> bool:
        """Start preview audio via PCM engine (preferred) or ffplay fallback.

        Export never uses this path.
        """
        if not self.path:
            return False
        start, end = self._clamp_play_window(start_seconds, end_seconds)
        cut = self._cut_timeline()
        # Live gain + fade look
        if self._audio.available:
            ok = self._audio.play(
                start,
                end,
                cut=cut,
                volume=float(self.preview_volume),
                mute=bool(self.preview_mute),
                audio_fade_in=float(self.preview_audio_fade_in),
                audio_fade_out=float(self.preview_audio_fade_out),
                speed=float(self.preview_speed),
            )
            if ok:
                return True
        # Legacy ffplay fallback
        return self._start_audio_ffplay(start, end)

    def _start_audio_ffplay(self, start: float, end: float) -> bool:
        ffplay = find_ffplay()
        if not ffplay:
            return False
        self._stop_audio()
        cut = self._cut_timeline()
        af = build_audio_filter(
            start=start,
            end=end,
            cut=cut,
            volume=float(self.preview_volume),
            mute=bool(self.preview_mute),
            audio_fade_in=float(self.preview_audio_fade_in),
            audio_fade_out=float(self.preview_audio_fade_out),
            input_preseeked=True,
        )
        win = max(0.05, end - start)
        cmd = [
            str(ffplay),
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.4f}",
            "-t",
            f"{win:.4f}",
            "-i",
            str(self.path),
            "-vn",
            "-af",
            af,
        ]
        try:
            creation = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            self._ffplay_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation,
            )
            return True
        except Exception as exc:
            self._status(f"Audio play failed: {exc}")
            self._ffplay_proc = None
            return False

    def restart_audio_from_position(self) -> None:
        """Re-apply current volume/fade/speed from the playhead (live In/Out)."""
        if not self._playing or not self.path:
            return
        # Live volume without full restart when using PCM engine
        if self._audio.available and self._audio.playing:
            self._audio.set_gain(float(self.preview_volume), mute=bool(self.preview_mute))
            self._audio.set_look(
                cut=self._cut_timeline(),
                audio_fade_in=float(self.preview_audio_fade_in),
                audio_fade_out=float(self.preview_audio_fade_out),
                speed=float(self.preview_speed),
            )
            # Fade envelope needs segment rebuild
            end = self._play_end_at if self._play_end_at > self.position else self.out_or_end
            if end <= self.position + 0.05:
                end = self.out_or_end if self.out_or_end > self.position else self.duration
            self._start_audio(self.position, end if end < 1e8 else None)
            return
        end = self._play_end_at if self._play_end_at > self.position else self.out_or_end
        if end <= self.position + 0.05:
            end = self.out_or_end if self.out_or_end > self.position else self.duration
        self._start_audio(self.position, end if end < 1e8 else None)

    def audio_alive(self) -> bool:
        if self._audio.available and self._audio.playing:
            return True
        p = self._ffplay_proc
        return p is not None and p.poll() is None

    def resume_from(
        self,
        seconds: float,
        *,
        selection_only: bool | None = None,
        loop: bool | None = None,
    ) -> None:
        """Seek and continue playback with correct A/V (timeline scrub while playing)."""
        sel = self._play_mode == "selection" if selection_only is None else bool(selection_only)
        do_loop = self._loop_selection if loop is None else bool(loop)
        self.stop()
        self.seek(seconds, emit=True)
        self.play(selection_only=sel, loop=do_loop)

    def _play_loop(self, gen: int, start_at: float, end_at: float) -> None:
        """Play with audio-master clock when PCM engine is active, else wall clock.

        OpenCV POS_MSEC is never the clock. Export does not use this path.
        """
        self.ensure_legal_marks()
        self._play_start_at = start_at
        self._play_end_at = end_at
        if self.duration > 0 and end_at < 1e8:
            end_at = min(end_at, self.duration)
        self._audio_restarts_this_seg = 0

        fps = max(self._fps, 1.0)
        with self._lock:
            if self._cap is not None:
                try:
                    import cv2

                    frame_idx = int(round(start_at * fps))
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                except Exception:
                    pass

        audio_ok = self._start_audio(start_at, end_at if end_at < 1e8 else None)
        use_audio_master = bool(audio_ok and self._audio.available and self._audio.playing)
        if not audio_ok and self.info and self.info.has_audio and not self.preview_mute:
            self._status("Playing without sound (audio engine unavailable)")
        elif use_audio_master:
            self._status("Play (audio-master clock)")

        wall0 = time.perf_counter()
        last_img: Image.Image | None = None
        last_emit = 0.0
        last_audio_check = 0.0

        while self._playing and gen == self._play_gen:
            speed = max(0.25, min(4.0, float(self.preview_speed)))
            if use_audio_master and self._audio.playing:
                target = self._audio.position()
                # Keep wall0 aligned for fallback if audio dies
                wall0 = time.perf_counter() - (target - start_at) / max(speed, 1e-6)
            else:
                target = start_at + (time.perf_counter() - wall0) * speed

            if target >= end_at - 1e-4:
                if self._loop_selection and gen == self._play_gen:
                    self.ensure_legal_marks()
                    start_at = self.in_point
                    end_at = self.out_or_end if self.out_or_end > 0 else self.duration
                    if self.duration > 0:
                        end_at = min(end_at, self.duration)
                    self._play_start_at = start_at
                    self._play_end_at = end_at
                    self._audio_restarts_this_seg = 0
                    with self._lock:
                        if self._cap is not None:
                            try:
                                import cv2

                                frame_idx = int(round(start_at * fps))
                                self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                            except Exception:
                                pass
                    audio_ok = self._start_audio(start_at, end_at if end_at < 1e8 else None)
                    use_audio_master = bool(
                        audio_ok and self._audio.available and self._audio.playing
                    )
                    wall0 = time.perf_counter()
                    continue
                end_pos = min(end_at, self.duration) if self.duration > 0 else end_at
                self.position = end_pos
                try:
                    if self._cap is not None:
                        self.seek(end_pos, emit=True, stop_playback=False)
                    elif last_img is not None:
                        self._emit_frame(last_img, end_pos)
                except Exception:
                    pass
                break

            self.position = target

            now_chk = time.perf_counter()
            if (
                now_chk - last_audio_check > 0.4
                and self.info
                and self.info.has_audio
                and not self.preview_mute
                and target < end_at - 0.35
            ):
                last_audio_check = now_chk
                if not self.audio_alive() and self._audio_restarts_this_seg < 1:
                    self._audio_restarts_this_seg += 1
                    if self._start_audio(target, end_at if end_at < 1e8 else None):
                        use_audio_master = bool(
                            self._audio.available and self._audio.playing
                        )
                        elapsed = (target - start_at) / max(speed, 1e-6)
                        wall0 = time.perf_counter() - elapsed
                        self._status("Audio restarted (preview)")

            img = None
            with self._lock:
                if self._cap is not None:
                    try:
                        import cv2

                        want_idx = int(round(target * fps))
                        guard = 0
                        while guard < 150:
                            try:
                                cur_idx = int(self._cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                            except Exception:
                                cur_idx = want_idx
                            if cur_idx >= want_idx:
                                break
                            ok, frame = self._cap.read()
                            guard += 1
                            if not ok:
                                break
                            img = self._frame_to_image(frame)
                            last_img = img
                        if img is None:
                            if last_img is None:
                                ok, frame = self._cap.read()
                                if ok:
                                    img = self._frame_to_image(frame)
                                    last_img = img
                            else:
                                img = last_img
                    except Exception:
                        img = last_img
                else:
                    img = self._waveform_frame_at(target)
                    last_img = img

            now = time.perf_counter()
            min_emit_dt = 1.0 / min(fps * max(speed, 1.0), 60.0)
            if img is not None and (now - last_emit) >= min_emit_dt:
                self._emit_frame(img, target)
                last_emit = now
                if self.on_position:
                    try:
                        self.on_position(target)
                    except Exception:
                        pass

            time.sleep(max(0.001, min(0.02, 0.4 / fps)))

        self._stop_audio()
        self._playing = False
        if gen == self._play_gen and self.on_position:
            try:
                self.on_position(self.position)
            except Exception:
                pass

    def current_preview_image(self) -> Image.Image | None:
        if self.info and self.info.kind == MediaKind.IMAGE and self._image:
            return self._image.copy()
        if self._waveform and self.info and (
            self.info.kind == MediaKind.AUDIO or (self.info.has_audio and not self.info.has_video)
        ):
            return self._waveform_frame_at(self.position)
        return None


def run_ffmpeg_with_progress(
    args: list[str],
    *,
    duration_hint: float | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    timeout: float | None = None,
) -> None:
    """Run ffmpeg with progress + cancel support."""
    from sekiclip.media_ops.ffmpeg_util import run_ffmpeg as _run

    _run(
        args,
        timeout=timeout,
        check=True,
        on_progress=on_progress,
        duration_hint=duration_hint,
    )
