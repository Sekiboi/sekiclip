"""Sekiclip main window — composes UI mixins. Free forever."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any

from PIL import Image

from sekiclip import __app_name__, __version__
from sekiclip import jobs
from sekiclip import media_ops as ops
from sekiclip.core import prefs as app_prefs
from sekiclip.core import session_store
from sekiclip.core.diagnostics import build_report
from sekiclip.media_ops.ffmpeg_util import (
    CancelledError,
    commit_staged,
    format_bytes,
    free_disk_bytes,
    paths_same,
    request_cancel,
    staging_path,
)
from sekiclip.core.models import ExportJob, Look
from sekiclip.preview.match import (
    CutTimeline,
    active_subs,
    fit_fades,
    load_srt_cached,
    video_fade_strength_at_source,
)
from sekiclip.preview.session import (
    MediaSession,
    find_ffplay,
    format_time,
    run_ffmpeg_with_progress,
)
from sekiclip.preview.timeline_widget import RangeTimeline
from sekiclip.core.tools_registry import TOOL_NAMES, load_dev_flags_from_env
from sekiclip.ui.quality import (
    AUDIO_QUALITY_BITRATE,
    AUDIO_QUALITY_DEFAULT_KEY,
    AUDIO_QUALITY_DEFAULT_LABEL,
    AUDIO_QUALITY_LABELS,
    AUDIO_QUALITY_MENU,
    EXPORT_QUALITY_HELP,
    VIDEO_QUALITY_DEFAULT_KEY,
    VIDEO_QUALITY_DEFAULT_LABEL,
    VIDEO_QUALITY_LABELS,
    VIDEO_QUALITY_MENU,
    VIDEO_QUALITY_PARAMS,
    normalize_audio_quality as _normalize_audio_quality,
    normalize_video_quality as _normalize_video_quality,
    video_scale_filter as _video_scale_filter,
)
from sekiclip.ui.util import (
    PREVIEW_H,
    PREVIEW_MAX,
    PREVIEW_MIN,
    PREVIEW_W,
    fit_image as _fit_image,
    parse_drop as _parse_drop,
    resource_path as _resource_path,
)
from sekiclip.ui.dialogs import DialogsMixin
from sekiclip.ui.export_ui import ExportMixin
from sekiclip.ui.panels import PanelsMixin
from sekiclip.ui.session_ui import SessionUiMixin

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Install customtkinter: pip install -r requirements.txt") from exc

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except ImportError:
    _HAS_DND = False
    TkinterDnD = None  # type: ignore
    DND_FILES = None  # type: ignore

APP_USER_MODEL_ID = "Sekiboi.Sekiclip"
MIN_W, MIN_H = 960, 600
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = ops.IMAGE_EXTS


if _HAS_DND and TkinterDnD is not None:

    class _CTkBase(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.TkdndVersion = TkinterDnD._require(self)

else:

    class _CTkBase(ctk.CTk):  # type: ignore[no-redef]
        pass


class SekiclipApp(
    ExportMixin,
    DialogsMixin,
    SessionUiMixin,
    PanelsMixin,
    _CTkBase,
):
    def __init__(self) -> None:
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except Exception:
                pass
        self._prefs = app_prefs.load_prefs()
        appearance = str(self._prefs.get("appearance_mode") or "System")
        ctk.set_appearance_mode(appearance)
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{__app_name__} {__version__}")
        self.minsize(MIN_W, MIN_H)
        self.resizable(True, True)
        # Fallback size before prefs restore (never open as a stamp-sized window)
        self.geometry(app_prefs.DEFAULT_GEOMETRY)

        self._files: list[Path] = []
        self._selected_idx: int = -1
        self._busy = False
        self._session = MediaSession()
        self._session.on_frame = self._on_session_frame
        self._session.on_position = self._on_session_position
        self._session.on_status = self._on_session_status
        self._preview_photo = None  # keep ref (CTkImage or PhotoImage)
        self._preview_size = (PREVIEW_W, PREVIEW_H)
        self._last_frame_img: Image.Image | None = None
        self._preview_resize_job: str | None = None
        self._scrub_dragging = False
        self._updating_scrub = False
        self._export_proc_active = False
        self._crop_mode = False
        self._crop_rect = (0.1, 0.1, 0.9, 0.9)  # normalized L,T,R,B on content
        self._crop_drag: str | None = None
        self._logo_ghost = False
        self._updating_time_fields = False
        self._batch_queue_lines: list[str] = []
        self._layout_ready = False
        self._pending_reload_path: Path | None = None  # re-open after in-place replace
        self._suppress_preview_trace = False  # avoid feedback loops when resetting looks
        self._resume_after_scrub = False
        self._scrub_play_mode = "to_out"
        self._scrub_loop = False
        # Scrub: throttle video seeks during drag; never restart audio mid-drag
        self._scrub_seek_job: str | None = None
        self._scrub_pending_t: float | None = None
        self._last_scrub_seek_ms: float = 0.0
        self._audio_restart_job: str | None = None
        # Session integrity watchdog
        self._integrity_job: str | None = None
        self._integrity_log_keys: set[str] = set()
        self._play_mode_ui: str = "to_out"  # "to_out" | "selection"
        # U1
        self._undo_stack: list[dict[str, Any]] = []
        self._redo_stack: list[dict[str, Any]] = []
        self._undo_suspend = False
        self._undo_look_job: str | None = None
        self.var_export_preset = ctk.StringVar(value=session_store.EXPORT_PRESET_LABELS[0])
        self.var_edit_action_ui = ctk.StringVar(
            value=session_store.EDIT_KEY_TO_LABEL.get("render_cut", "Full cut")
        )

        load_dev_flags_from_env()
        self._build()
        self._set_icons()
        self._bind_keys()
        self._restore_window_state()
        self.after(200, self._maybe_first_run)
        self.after(400, self._maybe_show_tips)
        self._refresh_ffmpeg_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # U4 a11y: keep keyboard focus on main window for shortcuts
        try:
            self.focus_set()
        except Exception:
            pass

    def _on_close(self) -> None:
        try:
            self._save_window_state()
        except Exception:
            pass
        self._session.close()
        self.destroy()

    def _set_icons(self) -> None:
        try:
            ico = _resource_path("assets", "sekiclip.ico")
            if ico.is_file():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    # ── layout ──────────────────────────────────────────────

    def _sash_bg(self) -> str:
        """Sash color that reads as a grip without fighting the theme."""
        try:
            mode = ctk.get_appearance_mode()
        except Exception:
            mode = "Dark"
        return "#2a2a2e" if mode == "Dark" else "#c8c8ce"

    def _on_session_frame(self, img: Image.Image | None, t: float) -> None:
        if img is None:
            return
        try:
            frame = img.copy()
        except Exception:
            return
        # Capture t by default arg so later events cannot clobber fade timing
        self.after(0, lambda i=frame, tt=float(t): self._show_frame(i, tt))

    def _on_session_position(self, t: float) -> None:
        tt = float(t)
        self.after(0, lambda tpos=tt: self._sync_scrub(tpos))

    def _on_session_status(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._set_status(m))

    def _sync_preview_audio(self) -> None:
        """Push export-matched mute/volume/fades/speed into the session for play."""
        look = self._export_look()
        self._session.preview_mute = bool(look["mute"])
        self._session.preview_volume = float(look["volume"]) if not look["mute"] else 0.0
        # When mute, volume is 0 in export; keep fade amounts for when unmuted
        self._session.preview_audio_fade_in = float(look["audio_fade_in"])
        self._session.preview_audio_fade_out = float(look["audio_fade_out"])
        self._session.preview_speed = float(look["speed"])

    def _draw_overlays(self, fitted: Image.Image, t: float | None = None) -> Image.Image:
        """Apply the same look export will use (crop, flip, logo, fades, subs)."""
        from PIL import ImageDraw

        look = self._export_look()
        img = fitted.convert("RGBA")
        w, h = img.size
        if t is None:
            t = float(self._session.position)

        # Flip (export flip_h when action is flip)
        if look.get("flip_h"):
            try:
                img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            except Exception:
                pass

        inn = float(look["start"])
        outp = float(look["end"] or self._session.duration or 0)
        if outp <= inn:
            outp = inn + 0.05
        speed = float(look.get("speed") or 1.0)
        cut = CutTimeline(in_point=inn, out_point=outp, speed=speed)
        sel_dur = cut.source_duration
        out_dur = cut.output_duration
        outside = not cut.contains_source(t)

        # Crop: export crops; preview zooms crop to stage (or handles while adjusting)
        use_crop = bool(look.get("use_crop"))
        if use_crop:
            l, top, r, b = look["crop_rect"]  # type: ignore[misc]
            x0, y0 = int(l * w), int(top * h)
            x1, y1 = max(x0 + 2, int(r * w)), max(y0 + 2, int(b * h))
            if self._crop_mode:
                d = ImageDraw.Draw(img, "RGBA")
                d.rectangle([0, 0, w, y0], fill=(0, 0, 0, 140))
                d.rectangle([0, y1, w, h], fill=(0, 0, 0, 140))
                d.rectangle([0, y0, x0, y1], fill=(0, 0, 0, 140))
                d.rectangle([x1, y0, w, y1], fill=(0, 0, 0, 140))
                d.rectangle([x0, y0, x1, y1], outline=(34, 197, 94, 255), width=2)
                for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                    d.rectangle([cx - 4, cy - 4, cx + 4, cy + 4], fill=(34, 197, 94, 255))
            else:
                # True crop fill — matches export composition
                try:
                    cropped = img.crop((x0, y0, x1, y1))
                    stage = Image.new("RGBA", (w, h), (0, 0, 0, 255))
                    cropped.thumbnail((w, h), Image.Resampling.LANCZOS)
                    px = (w - cropped.width) // 2
                    py = (h - cropped.height) // 2
                    stage.paste(cropped, (px, py), cropped if cropped.mode == "RGBA" else None)
                    img = stage
                except Exception:
                    pass

        # Logo (export opacity 0.9)
        if look.get("use_logo") and look.get("logo_path"):
            try:
                logo = Image.open(str(look["logo_path"])).convert("RGBA")
                sc = float(look.get("logo_scale") or 0.15)
                lw = max(8, int(w * sc))
                lh = max(8, int(logo.height * (lw / max(1, logo.width))))
                logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
                op = float(look.get("logo_opacity") or 0.9)
                alpha = logo.split()[-1].point(lambda p: int(p * op))
                logo.putalpha(alpha)
                pos = str(look.get("logo_pos") or "top-right").lower()
                if pos == "top-left":
                    xy = (12, 12)
                elif pos == "bottom-left":
                    xy = (12, h - lh - 12)
                elif pos == "bottom-right":
                    xy = (w - lw - 12, h - lh - 12)
                elif pos == "center":
                    xy = ((w - lw) // 2, (h - lh) // 2)
                else:
                    xy = (w - lw - 12, 12)
                img.paste(logo, xy, logo)
            except Exception:
                pass
        elif self._logo_ghost and self._logo_path and self._logo_path.is_file():
            # Ghost only for placement (not export) — light preview
            try:
                logo = Image.open(str(self._logo_path)).convert("RGBA")
                sc = float(self.var_logo_scale.get() or 0.15)
                lw = max(8, int(w * sc))
                lh = max(8, int(logo.height * (lw / max(1, logo.width))))
                logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
                alpha = logo.split()[-1].point(lambda p: int(p * 0.45))
                logo.putalpha(alpha)
                img.paste(logo, (w - lw - 12, 12), logo)
            except Exception:
                pass

        # Burn-in subtitles at source time t (export burns relative to cut; show active cue)
        if look.get("use_subs") and look.get("srt_path"):
            try:
                cues = load_srt_cached(look["srt_path"])
                text = active_subs(cues, t)
                if text:
                    d = ImageDraw.Draw(img, "RGBA")
                    # Bottom-center multi-line with dark outline
                    lines = text.split("\n")[:4]
                    y = h - 28 - 18 * len(lines)
                    for line in lines:
                        # crude center
                        tw = min(w - 20, 8 * len(line))
                        x = max(10, (w - tw) // 2)
                        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1), (0, 0)):
                            fill = (0, 0, 0, 220) if ox or oy else (255, 255, 240, 255)
                            d.text((x + ox, y + oy), line, fill=fill)
                        y += 18
            except Exception:
                pass

        # Video fades on *output* cut time from **current** In→Out (export match)
        vfi = float(look["video_fade_in"])
        vfo = float(look["video_fade_out"])
        vfi_fit, vfo_fit = fit_fades(out_dur, vfi, vfo)
        strength = 0.0
        if not outside:
            strength = video_fade_strength_at_source(cut, t, vfi, vfo)
        if strength > 0.001:
            rgb = img.convert("RGB")
            black = Image.new("RGB", (w, h), (0, 0, 0))
            rgb = Image.blend(rgb, black, min(1.0, strength))
            img = rgb.convert("RGBA")

        # Outside the trimmed cut: strong dim so In/Out ownership is obvious
        if outside and self._session.info and self._session.duration > 0:
            rgb = img.convert("RGB")
            black = Image.new("RGB", (w, h), (0, 0, 0))
            rgb = Image.blend(rgb, black, 0.72)
            img = rgb.convert("RGBA")

        # Status badges (honest about what export will do)
        d = ImageDraw.Draw(img, "RGBA")
        badges: list[str] = []
        try:
            badges.append(f"CUT {format_time(inn)}→{format_time(outp)}")
            if outside and self._session.info:
                badges.append("OUTSIDE CUT")
            if look.get("mute"):
                badges.append("MUTE")
            else:
                vol = float(look.get("volume") or 1.0)
                if abs(vol - 1.0) > 0.02:
                    badges.append(f"VOL {int(vol * 100)}%")
            sp = float(look.get("speed") or 1.0)
            if abs(sp - 1.0) > 0.02:
                badges.append(f"{sp:g}×")
            afi, afo = float(look["audio_fade_in"]), float(look["audio_fade_out"])
            if afi > 0 or afo > 0:
                badges.append(f"A-fade {afi:g}/{afo:g}s @cut")
            if vfi > 0 or vfo > 0:
                badges.append(f"V-fade {vfi:g}/{vfo:g}s @cut")
            if vfo_fit > 0 and not outside:
                t_out = cut.source_to_output(t)
                fade_start_out = out_dur - vfo_fit
                if t_out + 0.05 >= fade_start_out:
                    left = max(0.0, out_dur - t_out)
                    badges.append(f"OUT {left:.1f}s left")
                else:
                    fade_start_src = cut.output_to_source(fade_start_out)
                    badges.append(f"fade-out@{format_time(fade_start_src)}")
            if vfi_fit > 0 and not outside:
                t_out = cut.source_to_output(t)
                if t_out < vfi_fit:
                    badges.append(f"IN {t_out:.1f}/{vfi_fit:.1f}s")
            if strength > 0.05 and not outside:
                badges.append("FADING")
            if look.get("use_subs"):
                badges.append("SUBS")
            if look.get("use_crop"):
                badges.append("CROP")
            if look.get("use_logo"):
                badges.append("LOGO")
            if look.get("flip_h"):
                badges.append("FLIP H")
        except Exception:
            pass

        if badges:
            text = " · ".join(badges)
            tw = min(w - 16, max(80, 8 + 7 * len(text)))
            bh = 22
            d.rectangle([6, h - bh - 6, 6 + tw, h - 6], fill=(0, 0, 0, 170))
            try:
                d.text((12, h - bh - 2), text, fill=(240, 240, 245, 255))
            except Exception:
                pass

        return img.convert("RGB")

    def _preview_stage(self) -> tuple[int, int]:
        """Current letterbox stage size (follows the resizable preview pane)."""
        w, h = self._preview_size
        return max(PREVIEW_MIN[0], w), max(PREVIEW_MIN[1], h)

    def _on_preview_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        """Resize letterbox stage when the preview pane grows or shrinks."""
        w, h = int(event.width), int(event.height)
        if w < 40 or h < 40:
            return
        # Leave a little padding so the frame border stays visible
        pw = max(PREVIEW_MIN[0], w - 8)
        ph = max(PREVIEW_MIN[1], h - 8)
        pw -= pw % 2
        ph -= ph % 2
        if abs(pw - self._preview_size[0]) < 4 and abs(ph - self._preview_size[1]) < 4:
            return
        self._preview_size = (pw, ph)
        if self._preview_resize_job is not None:
            try:
                self.after_cancel(self._preview_resize_job)
            except Exception:
                pass
        # Debounce repaint while the user drags the sash/window
        self._preview_resize_job = self.after(80, self._repaint_preview_from_cache)

    def _repaint_preview_from_cache(self) -> None:
        """Re-apply looks on the last decoded frame at the current playhead."""
        self._preview_resize_job = None
        img = self._last_frame_img
        t = float(self._session.position)
        if img is None:
            # No cache yet — force a real decode
            self._force_preview_at(t)
            return
        try:
            self._show_frame(img, t)
        except Exception:
            self._force_preview_at(t)

    def _force_preview_at(self, t: float) -> None:
        """Decode + paint at exact source time t (timeline interactions).

        Always uses the requested time for fade math — never decoder MSEC.
        """
        if not self._session.info:
            return
        try:
            t = max(0.0, float(t))
            if self._session.duration > 0:
                t = min(t, self._session.duration)
            # Decode a fresh frame; do not stop if already stopped
            self._session.seek(t, emit=True, stop_playback=bool(self._session.playing))
            self._update_time_labels(self._session.position, self._session.duration)
        except Exception as exc:  # noqa: BLE001
            try:
                self._log(f"Preview refresh: {exc}")
            except Exception:
                pass
            # Last resort: paint cache with correct t
            if self._last_frame_img is not None:
                try:
                    self._show_frame(self._last_frame_img, t)
                except Exception:
                    pass

    def _on_right_pane_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not hasattr(self, "_tools_hint"):
            return
        wrap = max(160, int(event.width) - 28)
        try:
            self._tools_hint.configure(wraplength=wrap)
        except Exception:
            pass

    def _show_frame(self, img: Image.Image, t: float) -> None:
        """Paint preview on the main thread only (letterboxed + export FX)."""
        try:
            try:
                self._last_frame_img = img.copy()
            except Exception:
                self._last_frame_img = img
            stage = self._preview_stage()
            fitted = _fit_image(img, stage)
            # Always apply export-preview overlays so the stage matches the cut
            fitted = self._draw_overlays(fitted, t)
            w, h = stage
            try:
                # Fresh CTkImage each paint so fade/look changes always show
                light = fitted
                dark = fitted.copy()
                ctk_img = ctk.CTkImage(light_image=light, dark_image=dark, size=(w, h))
                self._preview_photo = ctk_img
                if self._tk_preview is not None:
                    self._tk_preview.place_forget()
                self.preview_label.configure(image=None)  # force detach old image
                self.preview_label.configure(image=ctk_img, text="")
                self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
                return
            except Exception as ctk_exc:
                try:
                    from PIL import ImageTk

                    if self._tk_preview is None:
                        self._tk_preview = tk.Label(
                            self.preview_frame, bg="#141418", borderwidth=0
                        )
                    photo = ImageTk.PhotoImage(fitted, master=self)
                    self._preview_photo = photo
                    self._tk_preview.configure(image=photo)
                    self.preview_label.place_forget()
                    self._tk_preview.place(relx=0.5, rely=0.5, anchor="center")
                except Exception as tk_exc:
                    self._log(f"Preview paint failed (CTk: {ctk_exc}; Tk: {tk_exc})")
                    self.preview_label.configure(
                        image=None,
                        text=f"Preview unavailable\n{type(ctk_exc).__name__}: {ctk_exc}",
                    )
                    self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Preview paint: {type(exc).__name__}: {exc}")

    def _sync_scrub(self, t: float) -> None:
        if self._scrub_dragging or self._updating_scrub:
            return
        self._updating_scrub = True
        try:
            self.timeline.set_position(t)
        finally:
            self._updating_scrub = False
        dur = self._session.duration
        self._update_time_labels(t, dur)
        if self._session.playing:
            self._set_transport_playing(True)

    def _schedule_audio_restart(self) -> None:
        """Debounce ffplay restarts (look changes while playing)."""
        if self._audio_restart_job is not None:
            try:
                self.after_cancel(self._audio_restart_job)
            except Exception:
                pass
        self._audio_restart_job = self.after(100, self._do_audio_restart)

    def _do_audio_restart(self) -> None:
        self._audio_restart_job = None
        if self._scrub_dragging or not self._session.playing:
            return
        self._sync_preview_audio()
        try:
            self._session.restart_audio_from_position()
        except Exception:
            pass

    def _schedule_scrub_video(self, t: float) -> None:
        """Throttle OpenCV seeks during hold-drag (≈16 ms / 60 Hz coalesced to ~50 ms)."""
        self._scrub_pending_t = float(t)
        if self._scrub_seek_job is not None:
            return
        self._scrub_seek_job = self.after(50, self._flush_scrub_video)

    def _flush_scrub_video(self) -> None:
        self._scrub_seek_job = None
        t = self._scrub_pending_t
        if t is None:
            return
        self._scrub_pending_t = None
        # Video-only during scrub — no audio
        self._force_preview_at(float(t))

    def _on_timeline_change(self, in_t: float, out_t: float, pos: float) -> None:
        """In/Out/range drag — live marks feed fade math immediately."""
        self._session.in_point = float(in_t)
        self._session.out_point = float(out_t)
        self._session.position = float(pos)
        self._update_io_label()
        self._sync_time_fields()
        self._sync_preview_audio()  # fade N uses new cut even if not playing
        # Throttled repaint so dragging In/Out updates fade overlay without lag pile-up
        if self._session.playing and not self._scrub_dragging:
            self._repaint_preview_from_cache()
            self._schedule_audio_restart()
        else:
            self._schedule_scrub_video(pos)

    def _on_timeline_seek(self, t: float) -> None:
        """Scrub playhead (click/hold-drag): video-only, throttled; no audio mid-drag."""
        self._scrub_dragging = True
        if self._session.playing:
            self._resume_after_scrub = True
            self._scrub_play_mode = getattr(self._session, "_play_mode", "to_out")
            self._scrub_loop = bool(getattr(self._session, "_loop_selection", False))
            self._session.stop()
            self._set_transport_playing(False)
        # Cancel any pending audio restart while scrubbing
        if self._audio_restart_job is not None:
            try:
                self.after_cancel(self._audio_restart_job)
            except Exception:
                pass
            self._audio_restart_job = None
        self._session.position = float(t)
        self._schedule_scrub_video(t)
        # Lightweight timeline pos only (avoid full set_range thrash every motion)
        try:
            self.timeline.set_position(t)
        except Exception:
            pass
        self._update_time_labels(t, self._session.duration)

    def _on_timeline_seek_end(self, t: float) -> None:
        """Mouse-up: final accurate frame; resume play only if scrub interrupted play."""
        self._push_undo()
        # Flush pending throttled seek
        if self._scrub_seek_job is not None:
            try:
                self.after_cancel(self._scrub_seek_job)
            except Exception:
                pass
            self._scrub_seek_job = None
        self._scrub_pending_t = None
        self._force_preview_at(t)
        self.timeline.set_range(
            self._session.in_point,
            self._session.out_or_end or self._session.duration,
            self._session.position,
        )
        self._scrub_dragging = False
        # Optional scrub audio tick on mouse-up (not during drag)
        try:
            self._session.scrub_audio_at(float(t))
        except Exception:
            pass
        if self._resume_after_scrub:
            self._resume_after_scrub = False
            self._sync_preview_audio()
            self._session.resume_from(
                t,
                selection_only=(self._scrub_play_mode == "selection"),
                loop=self._scrub_loop,
            )
            self._set_transport_playing(True)

    def _update_time_labels(self, pos: float, dur: float) -> None:
        self.time_label.configure(text=f"{format_time(pos)} / {format_time(dur)}")

    def _update_io_label(self) -> None:
        inn = format_time(self._session.in_point)
        out = format_time(self._session.out_or_end)
        dur = max(0.0, (self._session.out_or_end or 0) - self._session.in_point)
        self.io_label.configure(text=f"In {inn} → Out {out}  ({format_time(dur)})")
        self.range_label.configure(
            text=f"In→Out {inn}–{out}  ·  drag=scrub  ·  green/red=marks  ·  "
            f"Alt+drag=range  ·  Space=Play→Out  ·  Loop cut"
        )
        if not self._updating_scrub:
            self.timeline.set_range(
                self._session.in_point,
                self._session.out_or_end or self._session.duration,
                self._session.position,
            )

    def _sync_time_fields(self) -> None:
        self._updating_time_fields = True
        try:
            self.entry_in.delete(0, "end")
            self.entry_in.insert(0, format_time(self._session.in_point))
            self.entry_out.delete(0, "end")
            self.entry_out.insert(0, format_time(self._session.out_or_end))
            dur = max(0.0, self._session.out_or_end - self._session.in_point)
            self.entry_dur.delete(0, "end")
            self.entry_dur.insert(0, format_time(dur))
        finally:
            self._updating_time_fields = False

    def _parse_timecode(self, text: str) -> float | None:
        text = (text or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            if ":" in text:
                parts = text.split(":")
                if len(parts) == 2:
                    m, s = parts
                    return max(0.0, int(m) * 60 + float(s))
                if len(parts) == 3:
                    h, m, s = parts
                    return max(0.0, int(h) * 3600 + int(m) * 60 + float(s))
            return max(0.0, float(text))
        except ValueError:
            return None

    def _apply_time_fields(self) -> None:
        inn = self._parse_timecode(self.entry_in.get())
        out = self._parse_timecode(self.entry_out.get())
        if inn is None or out is None:
            messagebox.showwarning(__app_name__, "Enter times as mm:ss.xx or seconds.")
            return
        if out <= inn:
            messagebox.showwarning(__app_name__, "Out must be after In.")
            return
        self._push_undo()
        dur = self._session.duration or out
        self._session.in_point = min(inn, dur)
        self._session.out_point = min(out, dur)
        self.timeline.set_range(self._session.in_point, self._session.out_or_end, self._session.position)
        self._update_io_label()
        self._sync_time_fields()
        self._session.seek(self._session.in_point)
        self._push_undo()

    def _apply_duration_field(self) -> None:
        inn = self._parse_timecode(self.entry_in.get())
        dur = self._parse_timecode(self.entry_dur.get())
        if inn is None or dur is None or dur <= 0:
            messagebox.showwarning(__app_name__, "Enter valid In and duration.")
            return
        self._push_undo()
        self._session.in_point = inn
        self._session.out_point = min(self._session.duration or (inn + dur), inn + dur)
        self.timeline.set_range(self._session.in_point, self._session.out_or_end, self._session.position)
        self._update_io_label()
        self._push_undo()
        self._sync_time_fields()

    def _set_transport_playing(self, playing: bool) -> None:
        """Update Play button + mode hint."""
        if not hasattr(self, "btn_play"):
            return
        if playing:
            self.btn_play.configure(text="Pause")
            mode = getattr(self._session, "play_mode_label", "") or ""
            if hasattr(self, "play_mode_label"):
                self.play_mode_label.configure(text=mode)
        else:
            self.btn_play.configure(text="Play → Out")
            if hasattr(self, "play_mode_label"):
                self.play_mode_label.configure(text="")

    def _integrity_note(self, key: str, msg: str) -> None:
        """Log an integrity fix once per key until media reloads."""
        if key in self._integrity_log_keys:
            return
        self._integrity_log_keys.add(key)
        self._log(f"Integrity: {msg}")

    def _integrity_tick(self) -> None:
        """Keep marks legal, heal dead preview A/V, resync labels (~0.75s)."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            self._run_integrity_checks()
        except Exception as exc:  # noqa: BLE001
            try:
                self._integrity_note("tick_err", f"watchdog error ({type(exc).__name__})")
            except Exception:
                pass
        try:
            if self.winfo_exists():
                self.after(750, self._integrity_tick)
        except Exception:
            pass

    def _run_integrity_checks(self) -> None:
        sess = self._session
        if not sess.info or not sess.path:
            return

        # 1) Legal In/Out/playhead
        notes = sess.ensure_legal_marks()
        for n in notes:
            self._integrity_note(f"mark:{n}", n)
            self._update_io_label()
            self._sync_time_fields()
            try:
                self.timeline.set_range(
                    sess.in_point, sess.out_or_end or sess.duration, sess.position
                )
            except Exception:
                pass

        # 2) Dead OpenCV capture while we expect video
        if sess.info.has_video and not sess.video_capture_ok():
            if sess.try_reopen_video():
                self._integrity_note("cap_reopen", "re-opened video preview capture")
                if not sess.playing:
                    try:
                        sess.seek(sess.position, emit=True, stop_playback=False)
                    except Exception:
                        pass
            else:
                self._integrity_note("cap_dead", "video preview capture unavailable")

        # 3) Playing but transport UI stale / natural end
        if not sess.playing and hasattr(self, "btn_play"):
            txt = str(self.btn_play.cget("text") or "")
            if txt == "Pause":
                self._set_transport_playing(False)

        # 4) Resync time label if drifted from session while idle
        if not sess.playing and not self._scrub_dragging and not self._updating_scrub:
            try:
                self._update_time_labels(sess.position, sess.duration)
            except Exception:
                pass

        # 5) Orphan staging next to open file (leftover after cancel/crash)
        if not self._busy and not self._export_proc_active:
            try:
                from sekiclip.media_ops.ffmpeg_util import staging_path

                for p in list(self._files)[:8]:
                    sp = staging_path(p)
                    if sp.is_file():
                        # Only remove tiny/empty leftovers or very old? Safe: remove if not busy
                        age_ok = True
                        try:
                            import time as _time

                            age_ok = (_time.time() - sp.stat().st_mtime) > 30
                        except OSError:
                            age_ok = False
                        if age_ok:
                            sp.unlink(missing_ok=True)
                            self._integrity_note(
                                f"stage:{sp.name}",
                                f"removed leftover staging file {sp.name}",
                            )
            except Exception:
                pass

    # ── U1: recent / session / undo ─────────────────────────

    def _toggle_play(self) -> None:
        """Space / Play → Out: playhead to Out, wall-clock master, A+V same t0."""
        if not self._session.info:
            return
        if self._session.playing:
            self._session.stop()
            self._set_transport_playing(False)
            self._set_status("Paused")
        else:
            self._session.ensure_legal_marks()
            self._sync_preview_audio()
            self._play_mode_ui = "to_out"
            self._session.play(selection_only=False, loop=False)
            self._set_transport_playing(True)
            afi = self._session.preview_audio_fade_in
            afo = self._session.preview_audio_fade_out
            fade = ""
            if afi > 0 or afo > 0:
                fade = f" · A-fade {afi:g}/{afo:g}s on cut"
            try:
                am = self._session.preview_metrics().get("audio") or {}
                eng = am.get("engine", "?") if isinstance(am, dict) else "?"
            except Exception:
                eng = "?"
            self._set_status(f"Play → Out · engine={eng}{fade}")
            self._log(f"Preview metrics: {self._session.preview_metrics()}")

    def _play_selection(self) -> None:
        """Loop cut: In→Out repeatedly for review."""
        if not self._session.info:
            return
        self._session.ensure_legal_marks()
        self._sync_preview_audio()
        self._play_mode_ui = "selection"
        self._session.play_selection(loop=True)
        self._set_transport_playing(True)
        afi = self._session.preview_audio_fade_in
        afo = self._session.preview_audio_fade_out
        fade = ""
        if afi > 0 or afo > 0:
            fade = f" · A-fade {afi:g}/{afo:g}s"
        self._set_status("Loop cut In→Out — Stop to end" + fade)

    def _frame_step(self, delta: int) -> None:
        if not self._session.info:
            return
        t = self._session.frame_step(delta)
        self.timeline.set_position(t)
        self._update_time_labels(t, self._session.duration)
        self._set_transport_playing(False)

    def _stop(self) -> None:
        self._session.stop()
        self._set_transport_playing(False)
        self._session.seek(self._session.in_point)
        self.timeline.set_position(self._session.position)
        self._set_status("Stopped at In")

    def _mark_in(self) -> None:
        self._push_undo()
        self._session.set_in()
        self.timeline.set_range(
            self._session.in_point, self._session.out_or_end, self._session.position
        )
        self._update_io_label()
        self._sync_time_fields()
        self._push_undo()
        self._log(f"In → {format_time(self._session.in_point)}")

    def _mark_out(self) -> None:
        self._push_undo()
        self._session.set_out()
        self.timeline.set_range(
            self._session.in_point, self._session.out_or_end, self._session.position
        )
        self._update_io_label()
        self._sync_time_fields()
        self._push_undo()
        self._log(f"Out → {format_time(self._session.out_or_end)}")

    def _clear_io(self) -> None:
        self._push_undo()
        self._session.clear_in_out()
        self.timeline.set_range(0.0, self._session.duration or 1.0, self._session.position)
        self._update_io_label()
        self._sync_time_fields()
        self._push_undo()
        self._log("In/Out cleared")

    def _goto_mark(self, which: str) -> None:
        if which == "in":
            self._session.seek(self._session.in_point)
        else:
            self._session.seek(self._session.out_or_end or 0)
        self.timeline.set_position(self._session.position)

    def _zoom(self, factor: float) -> None:
        self.timeline.zoom(factor, center=self._session.position)

    def _zoom_fit(self) -> None:
        self.timeline.zoom_fit()

    def _zoom_sel(self) -> None:
        self.timeline.zoom_selection()

    def _bind_keys(self) -> None:
        self.bind("<space>", lambda _e: self._toggle_play())
        self.bind("<Key-i>", lambda _e: self._mark_in())
        self.bind("<Key-I>", lambda _e: self._mark_in())
        self.bind("<Key-o>", lambda _e: self._mark_out())
        self.bind("<Key-O>", lambda _e: self._mark_out())
        self.bind("<Left>", lambda _e: self._frame_step(-1))
        self.bind("<Right>", lambda _e: self._frame_step(1))
        self.bind("<Escape>", lambda _e: self._cancel_export())
        self.bind("<Control-z>", lambda _e: self._undo())
        self.bind("<Control-Z>", lambda _e: self._undo())
        self.bind("<Control-y>", lambda _e: self._redo())
        self.bind("<Control-Y>", lambda _e: self._redo())
        self.bind("<Control-s>", lambda _e: self._save_session())
        self.bind("<Control-S>", lambda _e: self._save_session())
        self.bind("<Control-o>", lambda _e: self._add_files())
        self.bind("<Control-O>", lambda _e: self._add_files())
        self.bind("<Key-question>", lambda _e: self._show_keyboard_help())
        self.bind("<F1>", lambda _e: self._show_keyboard_help())
        self.focus_set()

    def _on_volume_slider(self, value: float) -> None:
        self.var_volume.set(f"{float(value):.2f}")
        if hasattr(self, "volume_label"):
            self.volume_label.configure(text=f"{int(float(value) * 100)}%")
        self._on_edit_setting_changed()

    def _toggle_crop_mode(self) -> None:
        self._crop_mode = not self._crop_mode
        self._logo_ghost = False
        if self._crop_mode and hasattr(self, "var_use_crop"):
            self.var_use_crop.set(True)
        self._set_status(
            "Crop overlay ON — drag corners on preview"
            if self._crop_mode
            else "Crop overlay off"
        )
        self._on_edit_setting_changed()

    def _toggle_logo_ghost(self) -> None:
        self._logo_ghost = not self._logo_ghost
        self._crop_mode = False
        if self._logo_ghost and hasattr(self, "var_use_logo"):
            self.var_use_logo.set(True)
        self._set_status("Logo ghost ON" if self._logo_ghost else "Logo ghost off")
        self._on_edit_setting_changed()

    def _crop_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._crop_mode:
            return
        w = max(1, self.preview_label.winfo_width())
        h = max(1, self.preview_label.winfo_height())
        nx, ny = event.x / w, event.y / h
        l, t, r, b = self._crop_rect
        hit = 0.04
        # Corner hits
        corners = {
            "tl": (l, t),
            "tr": (r, t),
            "bl": (l, b),
            "br": (r, b),
        }
        for name, (cx, cy) in corners.items():
            if abs(nx - cx) < hit and abs(ny - cy) < hit:
                self._crop_drag = name
                return
        # Inside = move
        if l <= nx <= r and t <= ny <= b:
            self._crop_drag = "move"
            self._crop_move_origin = (nx, ny, l, t, r, b)
        else:
            # New rect from point
            self._crop_drag = "new"
            self._crop_rect = (nx, ny, nx + 0.05, ny + 0.05)

    def _crop_motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._crop_mode or not self._crop_drag:
            return
        w = max(1, self.preview_label.winfo_width())
        h = max(1, self.preview_label.winfo_height())
        nx = max(0.0, min(1.0, event.x / w))
        ny = max(0.0, min(1.0, event.y / h))
        l, t, r, b = self._crop_rect
        d = self._crop_drag
        if d == "tl":
            l, t = min(nx, r - 0.05), min(ny, b - 0.05)
        elif d == "tr":
            r, t = max(nx, l + 0.05), min(ny, b - 0.05)
        elif d == "bl":
            l, b = min(nx, r - 0.05), max(ny, t + 0.05)
        elif d == "br":
            r, b = max(nx, l + 0.05), max(ny, t + 0.05)
        elif d == "move" and hasattr(self, "_crop_move_origin"):
            ox, oy, ol, ot, or_, ob = self._crop_move_origin
            dx, dy = nx - ox, ny - oy
            span_x, span_y = or_ - ol, ob - ot
            l = max(0.0, min(1.0 - span_x, ol + dx))
            t = max(0.0, min(1.0 - span_y, ot + dy))
            r, b = l + span_x, t + span_y
        elif d == "new":
            r, b = max(nx, l + 0.05), max(ny, t + 0.05)
        self._crop_rect = (l, t, r, b)
        self._repaint_preview_from_cache()

    def _crop_release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        self._crop_drag = None

    def _current_path(self) -> Path | None:
        if 0 <= self._selected_idx < len(self._files):
            return self._files[self._selected_idx]
        return None

