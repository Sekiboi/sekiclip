"""Visual media session: smooth video frames, audio playback, waveform scrub.

Video: OpenCV sequential read during play (not per-frame seek).
Audio: ffplay (same install as ffmpeg) for real-time preview sound.
Images: Pillow stills.
Offline only.
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

from clipwork.media_ops.ffmpeg_util import find_ffmpeg, probe, require_ffmpeg
from clipwork.preview_match import atempo_chain

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg", ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def find_ffplay() -> Path | None:
    """Locate ffplay next to ffmpeg or on PATH."""
    import os

    env = os.environ.get("CLIPWORK_FFPLAY") or os.environ.get("FFPLAY")
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
        self._ffplay_proc: subprocess.Popen[str] | None = None
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
        self._play_start_at: float = 0.0
        self._play_end_at: float = 0.0
        self._play_mode: str = "full"  # "full" | "selection"

    @property
    def duration(self) -> float:
        return float(self.info.duration) if self.info else 0.0

    @property
    def out_or_end(self) -> float:
        if self.out_point is not None:
            return min(self.out_point, self.duration or self.out_point)
        return self.duration

    @property
    def has_preview_audio(self) -> bool:
        return find_ffplay() is not None

    def close(self) -> None:
        self.stop()
        self._stop_audio()
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
        self._fps = info.fps if info.fps and info.fps > 1 else 30.0

        if info.kind == MediaKind.IMAGE:
            self._image = Image.open(path).convert("RGB")
            self._emit_frame(self._image.copy(), 0.0)
        elif info.kind == MediaKind.VIDEO or info.has_video:
            self._open_video(path)
            self.seek(0.0, emit=True)
            if info.has_audio and not find_ffplay():
                self._status("ffplay not found — video only (install full ffmpeg with ffplay)")
            threading.Thread(
                target=self._bg_waveform, args=(path, info.duration), daemon=True
            ).start()
        elif info.kind == MediaKind.AUDIO or info.has_audio:
            if not find_ffplay():
                self._status("ffplay not found — install full ffmpeg build for audio play")
            self._waveform = self._build_waveform(path, info.duration)
            self._emit_frame(self._waveform_frame_at(0.0), 0.0)
        else:
            self._emit_frame(self._placeholder("Unsupported file"), 0.0)
        return info

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
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video for preview: {path.name}")
        # Prefer FFmpeg backend timing when available
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
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

    def _placeholder(self, text: str, size: tuple[int, int] = (960, 540)) -> Image.Image:
        img = Image.new("RGB", size, (28, 28, 32))
        d = ImageDraw.Draw(img)
        d.text((24, size[1] // 2 - 10), text, fill=(200, 200, 210))
        return img

    def _bg_waveform(self, path: Path, duration: float) -> None:
        try:
            self._waveform = self._build_waveform(path, duration)
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
        ff = find_ffmpeg()
        if not ff:
            return self._placeholder("ffmpeg needed for waveform")
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                out = Path(tmp.name)
            cmd = [
                str(ff),
                "-hide_banner",
                "-y",
                "-i",
                str(path),
                "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=960x200:colors=3B82F6",
                "-frames:v",
                "1",
                str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=180)
            if proc.returncode != 0 or not out.is_file():
                out.unlink(missing_ok=True)
                return self._placeholder("Waveform unavailable")
            img = Image.open(out).convert("RGB")
            out.unlink(missing_ok=True)
            canvas = Image.new("RGB", (960, 240), (24, 24, 28))
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

    def _frame_to_image(self, frame) -> Image.Image | None:
        try:
            import cv2
            import numpy as np

            if frame is None:
                return None
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

    def seek(self, seconds: float, *, emit: bool = True) -> None:
        """Seek playhead. Stops playback. emit=False skips UI (used internally)."""
        was_playing = self._playing
        if was_playing:
            self.stop()
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
                # Prefer frame index for more stable seeks
                if self._fps > 1:
                    frame_idx = int(round(seconds * self._fps))
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                else:
                    self._cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
                ok, frame = self._cap.read()
                img = self._frame_to_image(frame) if ok else None
                self.position = self._cap_time() if ok else seconds
                if emit:
                    self._emit_frame(
                        img or self._placeholder(f"No frame @ {seconds:.2f}s"),
                        self.position,
                    )
                return

            # Audio-only
            if emit:
                self._emit_frame(self._waveform_frame_at(seconds), seconds)

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
        """Step playhead by N frames (paused). Returns new time."""
        self.stop()
        fps = max(self._fps, 1.0)
        t = self.position + (delta / fps)
        self.seek(t, emit=True)
        return self.position

    def play(self, *, selection_only: bool = False, loop: bool = False) -> None:
        if self._playing or not self.info:
            return
        if self.info.kind == MediaKind.IMAGE:
            return

        self._play_gen += 1
        gen = self._play_gen
        self._playing = True
        self._loop_selection = bool(loop)
        self._play_mode = "selection" if selection_only else "full"

        if selection_only:
            end_at = self.out_or_end if self.out_or_end > 0 else self.duration
            # Resume mid-selection when playhead is already inside In→Out (scrub-while-play)
            if self.in_point <= self.position < end_at - 0.05:
                start_at = self.position
            else:
                start_at = self.in_point
            self.seek(start_at, emit=True)
        else:
            start_at = self.position
            end_at = self.out_or_end if self.out_or_end > 0 else 1e9
            if start_at >= end_at - 0.05:
                start_at = self.in_point
                self.seek(start_at, emit=True)

        self._play_thread = threading.Thread(
            target=self._play_loop, args=(gen, start_at, end_at), daemon=True
        )
        self._play_thread.start()

    def play_selection(self, *, loop: bool = True) -> None:
        """Play only In→Out (loops by default for review)."""
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

    def _preview_af_filters(self, start: float, end: float) -> str:
        """Export-identical audio chain: atrim → asetpts → volume → atempo → afade.

        UI fade seconds are output seconds after speed (same as render_cut).
        atrim keeps PTS clean so a 1s fade is exactly 1s (not ~10s from -ss quirks).
        """
        start, end = float(start), float(end)
        speed = max(0.25, min(4.0, float(self.preview_speed)))
        src_len = max(0.05, end - start)
        out_len = src_len / speed

        parts: list[str] = [
            f"atrim=start={start:.4f}:end={end:.4f}",
            "asetpts=PTS-STARTPTS",
        ]
        vol = 0.0 if self.preview_mute else max(0.0, min(4.0, float(self.preview_volume)))
        if abs(vol - 1.0) > 1e-3 or self.preview_mute:
            parts.append(f"volume={vol:.4f}")
        parts.extend(atempo_chain(speed))

        inn = float(self.in_point)
        outp = float(self.out_or_end) if self.out_or_end > 0 else end
        sel_src = max(0.05, outp - inn)
        out_sel = sel_src / speed
        max_each = max(0.05, out_sel * 0.49)

        afi = max(0.0, float(self.preview_audio_fade_in))
        afo = max(0.0, float(self.preview_audio_fade_out))
        out_off = max(0.0, (start - inn) / speed) if start >= inn - 1e-6 else 0.0

        if afi > 0 and start <= inn + 0.05:
            d = min(afi, max_each, out_len * 0.98)
            if d > 0.02:
                parts.append(f"afade=t=in:st=0:d={d:.4f}:curve=tri")

        if afo > 0 and end >= outp - 0.05:
            # Exact UI value (e.g. 1.0s), only capped by selection length
            d = min(afo, max_each, out_len * 0.98)
            st = (out_sel - d) - out_off
            if d > 0.02:
                if st >= out_len - 0.02:
                    pass
                elif st <= 0:
                    remaining = d + st
                    if remaining > 0.02:
                        parts.append(f"afade=t=out:st=0:d={remaining:.4f}:curve=tri")
                else:
                    parts.append(f"afade=t=out:st={st:.4f}:d={d:.4f}:curve=tri")

        return ",".join(parts)

    def _start_audio(self, start_seconds: float, end_seconds: float | None = None) -> bool:
        """Play audio with ffplay using the same filter chain shape as export."""
        if not self.path:
            return False
        ffplay = find_ffplay()
        if not ffplay:
            return False
        self._stop_audio()
        start, end = self._clamp_play_window(start_seconds, end_seconds)
        # No -ss before -i: atrim keeps timestamps deterministic (fixes wrong fade length)
        af = self._preview_af_filters(start, end)
        cmd: list[str] = [
            str(ffplay),
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "error",
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
        """Re-apply current volume/fade/speed filters from the playhead."""
        if not self._playing or not self.path:
            return
        end = self._play_end_at if self._play_end_at > self.position else self.out_or_end
        if end <= self.position + 0.05:
            end = self.out_or_end if self.out_or_end > self.position else self.duration
        self._start_audio(self.position, end if end < 1e8 else None)

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
        """Clock-driven playback: sequential video frames + synced audio (respects speed)."""
        self._play_start_at = start_at
        self._play_end_at = end_at
        audio_ok = self._start_audio(start_at, end_at if end_at < 1e8 else None)
        if not audio_ok and self.info and self.info.has_audio:
            self._status("Playing without sound (ffplay not found — use a full ffmpeg build)")

        wall0 = time.perf_counter()
        fps = max(self._fps, 1.0)
        speed = max(0.25, min(4.0, float(self.preview_speed)))
        # Resync capture to start
        with self._lock:
            if self._cap is not None:
                try:
                    import cv2

                    frame_idx = int(round(start_at * fps))
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                except Exception:
                    pass

        last_emit = 0.0
        min_emit_dt = 1.0 / min(fps * max(speed, 1.0), 60.0)

        while self._playing and gen == self._play_gen:
            # Re-read speed so live UI changes apply (matches export)
            speed = max(0.25, min(4.0, float(self.preview_speed)))
            elapsed = (time.perf_counter() - wall0) * speed
            target = start_at + elapsed
            if target >= end_at:
                if self._loop_selection and gen == self._play_gen:
                    start_at = self.in_point
                    end_at = self.out_or_end if self.out_or_end > 0 else self.duration
                    self._play_start_at = start_at
                    self._play_end_at = end_at
                    wall0 = time.perf_counter()
                    self.seek(start_at, emit=False)
                    self._start_audio(start_at, end_at if end_at < 1e8 else None)
                    with self._lock:
                        if self._cap is not None:
                            try:
                                import cv2

                                frame_idx = int(round(start_at * fps))
                                self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_idx))
                            except Exception:
                                pass
                    continue
                self.position = end_at if end_at < 1e9 else self.position
                try:
                    self.seek(self.position, emit=True)
                except Exception:
                    pass
                break

            img = None
            with self._lock:
                if self._cap is not None:
                    try:
                        import cv2

                        # Drop frames until we reach target (smooth catch-up)
                        guard = 0
                        while guard < 90:
                            cap_t = self._cap_time()
                            if cap_t >= target - (0.5 / fps):
                                break
                            ok, frame = self._cap.read()
                            guard += 1
                            if not ok:
                                break
                            img = self._frame_to_image(frame)
                            self.position = self._cap_time()
                        # If we didn't advance enough, still try one read when behind display
                        if img is None:
                            ok, frame = self._cap.read()
                            if ok:
                                img = self._frame_to_image(frame)
                                self.position = self._cap_time()
                            else:
                                # stuck at end
                                self.position = target
                    except Exception:
                        self.position = target
                else:
                    # Audio-only: advance playhead by clock
                    self.position = target
                    img = self._waveform_frame_at(target)

            now = time.perf_counter()
            if img is not None and (now - last_emit) >= min_emit_dt:
                self._emit_frame(img, self.position)
                last_emit = now
            elif img is None and self._cap is None:
                if (now - last_emit) >= min_emit_dt:
                    self._emit_frame(self._waveform_frame_at(self.position), self.position)
                    last_emit = now

            # Sleep until next frame boundary
            next_t = start_at + (time.perf_counter() - wall0) + (1.0 / fps)
            sleep_for = (next_t - start_at) - (time.perf_counter() - wall0)
            # simpler: sleep a fraction of frame time
            time.sleep(max(0.001, min(0.05, 0.5 / fps)))

        self._stop_audio()
        if gen == self._play_gen:
            self._playing = False
            if self.on_position:
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
    from clipwork.media_ops.ffmpeg_util import run_ffmpeg as _run

    _run(
        args,
        timeout=timeout,
        check=True,
        on_progress=on_progress,
        duration_hint=duration_hint,
    )
