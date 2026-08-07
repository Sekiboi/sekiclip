"""Session save/load, undo stack, window geometry."""

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


class SessionUiMixin:
    def _show_recent_menu(self) -> None:
        recent = session_store.get_recent_files()
        if not recent:
            messagebox.showinfo(__app_name__, "No recent files yet.\nAdd or drop a file first.")
            return
        win = ctk.CTkToplevel(self)
        win.title("Recent files")
        win.geometry("420x360")
        win.transient(self)
        ctk.CTkLabel(win, text="Open a recent file", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(12, 6), anchor="w"
        )
        box = ctk.CTkScrollableFrame(win)
        box.pack(fill="both", expand=True, padx=10, pady=6)

        def open_one(p: Path) -> None:
            win.destroy()
            self._open_path_list([p])

        for p in recent:
            ctk.CTkButton(
                box,
                text=p.name,
                anchor="w",
                command=lambda path=p: open_one(path),
            ).pack(fill="x", pady=2)
            ctk.CTkLabel(
                box,
                text=str(p),
                anchor="w",
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray60"),
            ).pack(fill="x", padx=4)

    def _open_path_list(self, paths: list[Path]) -> None:
        added = 0
        for path in paths:
            path = Path(path)
            if path.is_file() and path not in self._files:
                self._files.append(path)
                session_store.push_recent_file(path)
                added += 1
            elif path.is_file() and path in self._files:
                session_store.push_recent_file(path)
        self._rebuild_file_list()
        if not paths:
            return
        target = Path(paths[-1])
        if target in self._files:
            self._select_file(self._files.index(target))
        elif self._files:
            self._select_file(len(self._files) - 1)
        if added:
            self._log(f"Opened {added} file(s) from recent/session")

    def _capture_undo_state(self) -> dict[str, Any]:
        look = self._live_settings()
        return {
            "in": float(self._session.in_point),
            "out": float(self._session.out_or_end or 0) or None,
            "pos": float(self._session.position),
            "look": {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in look.items()
                if k not in ("logo_ghost",)
            },
            "crop_rect": tuple(self._crop_rect),
            "edit_ui": self.var_edit_action_ui.get(),
        }

    def _push_undo(self, *, force: bool = False) -> None:
        if self._undo_suspend and not force:
            return
        snap = self._capture_undo_state()
        if self._undo_stack and not force:
            last = self._undo_stack[-1]
            if (
                abs(float(last["in"]) - float(snap["in"])) < 1e-4
                and abs(float(last.get("out") or 0) - float(snap.get("out") or 0)) < 1e-4
                and last.get("look") == snap.get("look")
            ):
                return
        self._undo_stack.append(snap)
        if len(self._undo_stack) > 40:
            self._undo_stack = self._undo_stack[-40:]
        self._redo_stack.clear()

    def _apply_undo_state(self, snap: dict[str, Any]) -> None:
        self._undo_suspend = True
        try:
            look = dict(snap.get("look") or {})
            self.var_edit_action.set(str(look.get("edit_action") or "render_cut"))
            key = str(look.get("edit_action") or "render_cut")
            self.var_edit_action_ui.set(
                session_store.EDIT_KEY_TO_LABEL.get(key, "Full cut")
            )
            vq = _normalize_video_quality(str(look.get("video_quality") or "1080p"))
            aq = _normalize_audio_quality(str(look.get("audio_quality") or "256k"))
            self.var_video_quality.set(VIDEO_QUALITY_LABELS.get(vq, VIDEO_QUALITY_DEFAULT_LABEL))
            self.var_audio_quality.set(AUDIO_QUALITY_LABELS.get(aq, AUDIO_QUALITY_DEFAULT_LABEL))
            self.var_fade_video.set(bool(look.get("fade_video", True)))
            self.var_fade_audio.set(bool(look.get("fade_audio", True)))
            self.var_v_fade_in.set(str(look.get("v_fade_in") or "0.5"))
            self.var_v_fade_out.set(str(look.get("v_fade_out") or "0.5"))
            self.var_a_fade_in.set(str(look.get("a_fade_in") or "0.5"))
            self.var_a_fade_out.set(str(look.get("a_fade_out") or "0.5"))
            self.var_mute.set(bool(look.get("mute")))
            self.var_volume.set(str(look.get("volume") or "1.0"))
            try:
                self.volume_slider.set(float(self.var_volume.get() or 1.0))
                self.volume_label.configure(
                    text=f"{int(float(self.var_volume.get() or 1.0) * 100)}%"
                )
            except Exception:
                pass
            self.var_speed.set(str(look.get("speed") or "1.0"))
            self.var_use_crop.set(bool(look.get("use_crop")))
            self.var_use_logo.set(bool(look.get("use_logo")))
            self.var_use_subs.set(bool(look.get("use_subs")))
            self.var_logo_pos.set(str(look.get("logo_pos") or "top-right"))
            self.var_logo_scale.set(str(look.get("logo_scale") or "0.15"))
            self.var_crop_margin.set(str(look.get("crop_margin") or "40"))
            self.var_gif_fmt.set(str(look.get("gif_fmt") or "gif"))
            self.var_max_mb.set(str(look.get("max_mb") or "25"))
            cr = snap.get("crop_rect") or look.get("crop_rect")
            if cr and len(cr) == 4:
                self._crop_rect = (float(cr[0]), float(cr[1]), float(cr[2]), float(cr[3]))
            srt = look.get("srt_path") or ""
            logo = look.get("logo_path") or ""
            music = look.get("music_path") or ""
            self._srt_path = Path(srt) if srt and Path(srt).is_file() else None
            self._logo_path = Path(logo) if logo and Path(logo).is_file() else None
            self._music_path = Path(music) if music and Path(music).is_file() else None
            self.var_color_look.set(str(look.get("color_look") or "none"))
            self.var_color_strength.set(str(look.get("color_strength") or "1.0"))
            self.var_vfx.set(str(look.get("vfx") or "none"))
            self.var_vfx_strength.set(str(look.get("vfx_strength") or "1.0"))
            self.var_title.set(str(look.get("title") or ""))
            self.var_title_sub.set(str(look.get("title_sub") or ""))
            self.var_title_position.set(str(look.get("title_position") or "center"))
            self.var_end_card.set(str(look.get("end_card") or ""))
            self.var_end_card_hold.set(str(look.get("end_card_hold") or "3.0"))
            self.var_music_volume.set(str(look.get("music_volume") or "0.35"))
            self.var_music_fade_in.set(str(look.get("music_fade_in") or "1.0"))
            self.var_music_fade_out.set(str(look.get("music_fade_out") or "1.5"))
            self.var_music_duck.set(bool(look.get("music_duck")))
            self.var_transition.set(str(look.get("transition") or "crossfade"))
            self.var_transition_dur.set(str(look.get("transition_dur") or "0.6"))
            self._update_edit_files_label()
            inn = float(snap.get("in") or 0)
            out = snap.get("out")
            pos = float(snap.get("pos") or inn)
            self._session.in_point = inn
            self._session.out_point = float(out) if out is not None else self._session.duration
            self._session.ensure_legal_marks()
            self.timeline.set_range(
                self._session.in_point,
                self._session.out_or_end or self._session.duration,
                pos,
            )
            self._force_preview_at(pos)
            self._update_io_label()
            self._sync_time_fields()
            self._sync_preview_audio()
        finally:
            self._undo_suspend = False

    def _undo(self) -> None:
        if len(self._undo_stack) < 2:
            self._set_status("Nothing to undo")
            return
        cur = self._undo_stack.pop()
        self._redo_stack.append(cur)
        prev = self._undo_stack[-1]
        self._apply_undo_state(prev)
        self._set_status("Undo")
        self._log("Undo")

    def _redo(self) -> None:
        if not self._redo_stack:
            self._set_status("Nothing to redo")
            return
        snap = self._redo_stack.pop()
        self._undo_stack.append(snap)
        self._apply_undo_state(snap)
        self._set_status("Redo")
        self._log("Redo")

    def _save_session(self) -> None:
        if not self._current_path():
            messagebox.showwarning(__app_name__, "Open a media file first.")
            return
        path_str = filedialog.asksaveasfilename(
            title="Save session",
            defaultextension=".sekiclip.json",
            filetypes=[("Sekiclip session", "*.sekiclip.json"), ("JSON", "*.json"), ("All", "*.*")],
            initialfile=f"{self._current_path().stem}.sekiclip.json",
        )
        if not path_str:
            return
        payload = session_store.build_session_dict(
            media_path=self._current_path(),
            in_point=self._session.in_point,
            out_point=self._session.out_point,
            position=self._session.position,
            look=self._live_settings(),
            tool=self.tool.get() if hasattr(self, "tool") else "Edit",
        )
        out = session_store.save_session_file(path_str, payload)
        self._log(f"Session saved: {out.name}")
        self._set_status(f"Session saved · {out.name}")

    def _load_session(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Load session",
            filetypes=[("Sekiclip session", "*.sekiclip.json"), ("JSON", "*.json"), ("All", "*.*")],
        )
        if not path_str:
            return
        try:
            data = session_store.load_session_file(path_str)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(__app_name__, f"Could not load session:\n{exc}")
            return
        media = str(data.get("media") or "")
        if media and Path(media).is_file():
            self._open_path_list([Path(media)])
        look = data.get("look") or {}
        snap = {
            "in": float(data.get("in_point") or 0),
            "out": data.get("out_point"),
            "pos": float(data.get("position") or 0),
            "look": look,
            "crop_rect": look.get("crop_rect"),
            "edit_ui": session_store.EDIT_KEY_TO_LABEL.get(
                str(look.get("edit_action") or "render_cut"), "Full cut"
            ),
        }

        def apply_after_load() -> None:
            tool = str(data.get("tool") or "Edit")
            if tool in getattr(self, "_tool_names", []):
                self._select_tool(tool)
            self._apply_undo_state(snap)
            self._push_undo(force=True)
            self._log(f"Session loaded: {Path(path_str).name}")
            self._set_status("Session loaded")

        self.after(400, apply_after_load)

    def _restore_window_state(self) -> None:
        prefs = self._prefs
        if not prefs.get("remember_window", True):
            self.geometry(app_prefs.DEFAULT_GEOMETRY)
            return
        geo = str(prefs.get("geometry") or app_prefs.DEFAULT_GEOMETRY)
        try:
            self.geometry(geo)
        except Exception:
            self.geometry(app_prefs.DEFAULT_GEOMETRY)
        if prefs.get("zoomed"):
            try:
                self.state("zoomed")
            except Exception:
                try:
                    self.attributes("-zoomed", True)
                except Exception:
                    pass
        # Keep window on-screen if monitor setup changed
        self.after(50, self._ensure_on_screen)

    def _ensure_on_screen(self) -> None:
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x, y = self.winfo_x(), self.winfo_y()
            w, h = self.winfo_width(), self.winfo_height()
            if w < 200 or h < 160:
                return
            nx = min(max(0, x), max(0, sw - 120))
            ny = min(max(0, y), max(0, sh - 80))
            if (nx, ny) != (x, y):
                self.geometry(f"+{nx}+{ny}")
        except Exception:
            pass

    def _save_window_state(self) -> None:
        data = app_prefs.load_prefs()
        if not data.get("remember_window", True):
            return
        try:
            zoomed = False
            try:
                zoomed = bool(self.state() == "zoomed")
            except Exception:
                zoomed = False
            data["zoomed"] = zoomed
            if not zoomed:
                # geometry() includes size+position; skip while maximized (OS-specific)
                data["geometry"] = self.geometry()
            # Pane widths / log height from sash coords
            try:
                self.update_idletasks()
                total_w = max(1, self._hpaned.winfo_width())
                total_h = max(1, self._vpaned.winfo_height())
                s0 = self._hpaned.sash_coord(0)[0]
                s1 = self._hpaned.sash_coord(1)[0]
                data["left_pane_w"] = max(140, min(480, int(s0)))
                data["right_pane_w"] = max(220, min(560, int(total_w - s1)))
                log_top = self._vpaned.sash_coord(0)[1]
                data["log_pane_h"] = max(72, min(400, int(total_h - log_top)))
            except Exception:
                pass
            app_prefs.save_prefs(data)
            self._prefs = data
        except Exception:
            pass

    def _apply_pane_sizes(self, left_w: int, right_w: int, log_h: int) -> None:
        """Place sashes from saved prefs after the first geometry pass."""
        try:
            self.update_idletasks()
            total_w = max(1, self._hpaned.winfo_width())
            total_h = max(1, self._vpaned.winfo_height())
            # Left sash
            lx = max(140, min(left_w, total_w - right_w - 360))
            self._hpaned.sash_place(0, lx, 1)
            # Right sash (from left edge)
            rx = max(lx + 360, min(total_w - right_w, total_w - 220))
            self._hpaned.sash_place(1, rx, 1)
            # Log sash from top of vpaned
            ly = max(280, total_h - max(72, log_h))
            self._vpaned.sash_place(0, 1, ly)
        except Exception:
            pass

    def _mark_layout_ready(self) -> None:
        self._layout_ready = True

