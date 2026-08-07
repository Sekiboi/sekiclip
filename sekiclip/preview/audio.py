"""Persistent preview audio engine (offline only).

Pipeline: ffmpeg → s16le PCM pipe → sounddevice OutputStream.

- One decoder stream per play segment (not process-per-scrub tick).
- Seek restarts the decoder from the new time.
- Volume / mute applied in the callback.
- Export never uses this module.

Falls back gracefully if sounddevice or ffmpeg is missing.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from sekiclip.media_ops.ffmpeg_util import find_ffmpeg
from sekiclip.preview.match import CutTimeline, build_audio_filter

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # s16le
BYTES_PER_FRAME = CHANNELS * SAMPLE_WIDTH
BLOCK_FRAMES = 2048
QUEUE_MAX_BLOCKS = 48


def _creation_flags() -> int:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0


class PreviewAudioEngine:
    """Long-lived preview audio player driven by an ffmpeg PCM pipe."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.volume: float = 1.0
        self.mute: bool = False
        self.preview_speed: float = 1.0
        self.audio_fade_in: float = 0.0
        self.audio_fade_out: float = 0.0
        self._cut: CutTimeline | None = None

        self._lock = threading.Lock()
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stream = None  # sd.OutputStream
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_MAX_BLOCKS)
        self._playing = False
        self._start_src = 0.0
        self._end_src = 0.0
        self._frames_played = 0
        self._gen = 0
        self._pending: bytes = b""
        self.on_status: Callable[[str], None] | None = None
        self._metrics: dict[str, int | float | str] = {
            "engine": "none",
            "restarts": 0,
            "underruns": 0,
        }

    # ── public API ──────────────────────────────────────────

    def open(self, path: Path | None) -> None:
        self.stop()
        self.path = Path(path) if path else None

    def close(self) -> None:
        self.stop()
        self.path = None

    @property
    def available(self) -> bool:
        if find_ffmpeg() is None:
            return False
        try:
            import sounddevice  # noqa: F401

            return True
        except ImportError:
            return False

    @property
    def playing(self) -> bool:
        return self._playing

    def metrics(self) -> dict[str, int | float | str]:
        return dict(self._metrics)

    def set_gain(self, volume: float, *, mute: bool = False) -> None:
        self.volume = max(0.0, min(4.0, float(volume)))
        self.mute = bool(mute)

    def set_look(
        self,
        *,
        cut: CutTimeline | None = None,
        volume: float | None = None,
        mute: bool | None = None,
        audio_fade_in: float | None = None,
        audio_fade_out: float | None = None,
        speed: float | None = None,
    ) -> None:
        if cut is not None:
            self._cut = cut
        if volume is not None:
            self.volume = max(0.0, min(4.0, float(volume)))
        if mute is not None:
            self.mute = bool(mute)
        if audio_fade_in is not None:
            self.audio_fade_in = max(0.0, float(audio_fade_in))
        if audio_fade_out is not None:
            self.audio_fade_out = max(0.0, float(audio_fade_out))
        if speed is not None:
            self.preview_speed = max(0.25, min(4.0, float(speed)))

    def position(self) -> float:
        """Current source-time estimate from samples played + start."""
        with self._lock:
            frames = self._frames_played
            start = self._start_src
            speed = max(0.25, min(4.0, float(self.preview_speed)))
        # PCM is after atempo, so wall/sample time ≈ output time; map back to source
        out_t = frames / float(SAMPLE_RATE)
        return start + out_t * speed

    def play(
        self,
        start: float,
        end: float,
        *,
        cut: CutTimeline | None = None,
        volume: float | None = None,
        mute: bool | None = None,
        audio_fade_in: float | None = None,
        audio_fade_out: float | None = None,
        speed: float | None = None,
    ) -> bool:
        """Start (or restart) playback of [start, end) on the source timeline."""
        self.set_look(
            cut=cut,
            volume=volume,
            mute=mute,
            audio_fade_in=audio_fade_in,
            audio_fade_out=audio_fade_out,
            speed=speed,
        )
        return self._start_segment(float(start), float(end))

    def scrub_blip(self, t: float, *, duration: float = 0.08) -> None:
        """Optional short audio tick while scrubbing (non-blocking)."""
        if not self.path or self.mute or self.volume <= 0:
            return
        if self._playing:
            return  # don't fight continuous play
        end = float(t) + max(0.04, min(0.2, float(duration)))
        # Fire-and-forget short segment
        threading.Thread(
            target=self._blip_worker, args=(float(t), end), daemon=True
        ).start()

    def stop(self) -> None:
        self._gen += 1
        self._playing = False
        self._kill_io()
        with self._lock:
            self._frames_played = 0
            self._pending = b""

    # ── internals ───────────────────────────────────────────

    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _drain_queue(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._q = queue.Queue(maxsize=QUEUE_MAX_BLOCKS)

    def _kill_io(self) -> None:
        self._stop_stream()
        self._stop_ffmpeg()
        self._drain_queue()

    def _stop_ffmpeg(self) -> None:
        proc = self._ffmpeg
        self._ffmpeg = None
        if proc is None:
            return
        try:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    def _start_segment(self, start: float, end: float) -> bool:
        if not self.path or not self.path.is_file():
            return False
        if not self.available:
            self._metrics["engine"] = "unavailable"
            return False

        import numpy as np
        import sounddevice as sd

        # Tear down previous segment without double-bumping gen until we arm
        self._playing = False
        self._kill_io()
        self._gen += 1
        gen = self._gen
        start = max(0.0, float(start))
        end = max(start + 0.05, float(end))
        self._start_src = start
        self._end_src = end
        self._frames_played = 0
        self._pending = b""
        self._drain_queue()

        cut = self._cut or CutTimeline(start, end, speed=self.preview_speed)
        af = build_audio_filter(
            start=start,
            end=end,
            cut=cut,
            volume=1.0,  # gain applied in callback for live control
            mute=False,
            audio_fade_in=self.audio_fade_in,
            audio_fade_out=self.audio_fade_out,
            input_preseeked=True,
        )
        win = max(0.05, end - start)
        ff = find_ffmpeg()
        if not ff:
            return False

        cmd = [
            str(ff),
            "-hide_banner",
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
            "-ac",
            str(CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        try:
            self._ffmpeg = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
        except Exception as exc:
            self._status(f"Audio decode failed: {exc}")
            self._metrics["engine"] = "ffmpeg-fail"
            return False

        self._metrics["engine"] = "pcm+sounddevice"
        self._metrics["restarts"] = int(self._metrics.get("restarts", 0) or 0) + 1
        self._playing = True

        def reader() -> None:
            proc = self._ffmpeg
            if proc is None or proc.stdout is None:
                self._q.put(None)
                return
            try:
                while gen == self._gen and self._playing:
                    chunk = proc.stdout.read(BLOCK_FRAMES * BYTES_PER_FRAME)
                    if not chunk:
                        break
                    try:
                        self._q.put(chunk, timeout=1.0)
                    except queue.Full:
                        # Drop oldest pressure — prefer low latency over backlog
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._q.put(chunk, timeout=0.2)
                        except queue.Full:
                            pass
            except Exception:
                pass
            finally:
                try:
                    self._q.put(None)
                except Exception:
                    pass

        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()

        def callback(outdata, frames, _time_info, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                self._metrics["underruns"] = int(self._metrics.get("underruns", 0) or 0) + 1
            need = frames * BYTES_PER_FRAME
            buf = self._pending
            while len(buf) < need and self._playing and gen == self._gen:
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    break
                if item is None:
                    # EOS — pad silence and stop soon
                    self._playing = False
                    break
                buf += item
            if len(buf) < need:
                # underrun / eos
                out = np.zeros((frames, CHANNELS), dtype=np.float32)
                if len(buf) >= BYTES_PER_FRAME:
                    n = (len(buf) // BYTES_PER_FRAME) * BYTES_PER_FRAME
                    usable = buf[:n]
                    arr = np.frombuffer(usable, dtype=np.int16).astype(np.float32) / 32768.0
                    arr = arr.reshape(-1, CHANNELS)
                    m = min(frames, arr.shape[0])
                    out[:m] = arr[:m]
                    buf = buf[n:]
                outdata[:] = out
                self._pending = buf
                return
            take = buf[:need]
            self._pending = buf[need:]
            arr = np.frombuffer(take, dtype=np.int16).astype(np.float32) / 32768.0
            arr = arr.reshape(frames, CHANNELS)
            gain = 0.0 if self.mute else float(self.volume)
            if abs(gain - 1.0) > 1e-3:
                arr *= gain
            outdata[:] = arr
            with self._lock:
                self._frames_played += frames

        try:
            self._stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                blocksize=BLOCK_FRAMES,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._status(f"Audio device failed: {exc}")
            self._playing = False
            self._stop_ffmpeg()
            self._metrics["engine"] = "device-fail"
            return False
        return True

    def _blip_worker(self, start: float, end: float) -> None:
        """Tiny one-shot blip without owning the main play stream for long."""
        if not self.path or not self.available:
            return
        # Only if idle
        if self._playing:
            return
        self._start_segment(start, end)
        # Auto-stop after short duration
        time.sleep(max(0.05, end - start) + 0.05)
        if not self._playing:
            return
        # If still the blip (very short), stop
        if self.position() >= end - 0.02:
            self.stop()
