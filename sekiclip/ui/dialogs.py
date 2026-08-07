"""First-run, settings, about, tips, keyboard help."""

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


class DialogsMixin:
    def _maybe_first_run(self) -> None:
        data = app_prefs.load_prefs()
        if data.get("first_run_completed"):
            return
        win = ctk.CTkToplevel(self)
        win.title(f"Welcome to {__app_name__}")
        win.geometry("520x480")
        win.minsize(440, 400)
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass

        ff = ops.find_ffmpeg()
        fp = ops.find_ffprobe()
        tools_ok = bool(ff and fp)
        if tools_ok:
            setup_note = "Video and audio tools are ready on this PC."
        else:
            setup_note = (
                "Video and audio tools were not found.\n"
                "Images still work. Reinstall from the full Setup package,\n"
                "or place ffmpeg.exe and ffprobe.exe in the app’s vendor folder."
            )

        ctk.CTkLabel(
            win,
            text=f"{__app_name__} — offline media editor",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(padx=16, pady=(16, 6), anchor="w")
        ctk.CTkLabel(
            win,
            text=(
                "Free forever · no watermarks · no paid plan\n"
                "Nothing is uploaded. Your layout is remembered on this PC.\n\n"
                "• Drop a file or Add… · drag green/red marks for In/Out\n"
                "• Space = Play → Out · Loop cut · I/O keys · ←/→ frame\n"
                "• Preview may look softer than export (by design)\n"
                "• Export with Save as… or batch · Cancel anytime"
            ),
            justify="left",
            wraplength=480,
        ).pack(padx=16, anchor="w")
        ctk.CTkLabel(
            win,
            text="Ready check",
            font=ctk.CTkFont(weight="bold"),
        ).pack(padx=16, pady=(12, 2), anchor="w")
        ctk.CTkLabel(
            win,
            text=setup_note,
            justify="left",
            wraplength=480,
            text_color=("gray30", "gray70"),
        ).pack(padx=16, pady=(4, 8), anchor="w")

        var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            win,
            text="Allow local diagnostics copy (off by default; never uploads)",
            variable=var,
        ).pack(padx=16, anchor="w")
        if app_prefs.is_portable_mode():
            ctk.CTkLabel(
                win,
                text=f"Portable data folder:\n{app_prefs.user_data_dir()}",
                justify="left",
                font=ctk.CTkFont(size=11),
                text_color=("gray40", "gray60"),
            ).pack(padx=16, pady=(8, 0), anchor="w")

        def close() -> None:
            data["first_run_completed"] = True
            data["diagnostics_enabled"] = bool(var.get())
            data["show_tips_next_start"] = True
            app_prefs.save_prefs(data)
            self._prefs = data
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            if not tools_ok:
                self._set_status("Video tools missing · images still work")
            else:
                self._set_status("Ready")
            self.after(300, self._maybe_show_tips)

        ctk.CTkButton(win, text="Continue", command=close, width=120).pack(pady=16)

    def _maybe_show_tips(self) -> None:
        data = app_prefs.load_prefs()
        if not data.get("show_tips_next_start"):
            return
        data["show_tips_next_start"] = False
        app_prefs.save_prefs(data)
        self._prefs = data
        self._show_tips()

    def _show_tips(self) -> None:
        """Short getting-started tips (not a full tutorial)."""
        win = ctk.CTkToplevel(self)
        win.title("Quick tips")
        win.geometry("440x360")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text=(
                "Getting started\n\n"
                "1. Add… or drop a media file\n"
                "2. Drag the yellow playhead; green/red = In/Out\n"
                "3. Edit looks (fades, etc.) if needed\n"
                "4. Export / Save as…\n\n"
                "Space = Play → Out · Loop cut reviews In→Out\n"
                "Ctrl+Z / Y = undo / redo · ? = keyboard list\n"
                "Preview may look softer than the exported file.\n"
                "Video/audio need ffmpeg on PATH or in vendor/."
            ),
            justify="left",
            wraplength=400,
        ).pack(padx=16, pady=16, anchor="w")
        ctk.CTkButton(win, text="Close", command=win.destroy, width=100).pack(pady=8)

    def _settings(self) -> None:
        data = app_prefs.load_prefs()
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("460x520")
        win.minsize(380, 400)
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(scroll, text="Appearance", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(12, 4), anchor="w"
        )
        mode_var = ctk.StringVar(value=str(data.get("appearance_mode") or "System"))
        ctk.CTkOptionMenu(
            scroll, variable=mode_var, values=["System", "Light", "Dark"], width=200
        ).pack(padx=12, anchor="w")

        ctk.CTkLabel(scroll, text="Window", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(12, 4), anchor="w"
        )
        remember_var = ctk.BooleanVar(value=bool(data.get("remember_window", True)))
        ctk.CTkCheckBox(
            scroll,
            text="Remember size, position, and pane layout",
            variable=remember_var,
        ).pack(padx=12, anchor="w")

        def reset_layout() -> None:
            nonlocal data
            data = app_prefs.reset_layout_prefs(data)
            data["remember_window"] = bool(remember_var.get())
            data["appearance_mode"] = mode_var.get()
            app_prefs.save_prefs(data)
            self._prefs = data
            try:
                if self.state() == "zoomed":
                    self.state("normal")
            except Exception:
                pass
            self.geometry(app_prefs.DEFAULT_GEOMETRY)
            self._apply_pane_sizes(
                app_prefs.DEFAULT_LEFT_W,
                app_prefs.DEFAULT_RIGHT_W,
                app_prefs.DEFAULT_LOG_H,
            )
            self._set_status("Layout reset to defaults")

        ctk.CTkButton(scroll, text="Reset layout to defaults", command=reset_layout).pack(
            padx=12, pady=8, anchor="w"
        )

        ctk.CTkLabel(scroll, text="Privacy", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(8, 4), anchor="w"
        )
        var = ctk.BooleanVar(value=bool(data.get("diagnostics_enabled")))
        ctk.CTkCheckBox(
            scroll,
            text="Allow local diagnostics copy (never uploads)",
            variable=var,
        ).pack(padx=12, anchor="w")

        ctk.CTkLabel(scroll, text="Updates (optional)", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(12, 4), anchor="w"
        )
        upd_var = ctk.BooleanVar(value=bool(data.get("update_check_enabled")))
        ctk.CTkCheckBox(
            scroll,
            text="Enable update checks (opt-in; only when you ask)",
            variable=upd_var,
        ).pack(padx=12, anchor="w")
        ctk.CTkLabel(scroll, text="Version URL (optional)").pack(padx=12, pady=(6, 0), anchor="w")
        url_var = ctk.StringVar(value=str(data.get("update_check_url") or ""))
        ctk.CTkEntry(scroll, textvariable=url_var, width=380).pack(padx=12, pady=2, anchor="w")
        ctk.CTkLabel(
            scroll,
            text="Plain text version line, or leave blank until you publish.",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
            wraplength=400,
            justify="left",
        ).pack(padx=12, anchor="w")

        def do_check() -> None:
            from sekiclip.core.updates import check_for_update

            url = (url_var.get() or "").strip() or None
            res = check_for_update(url=url)
            messagebox.showinfo(__app_name__, res.message)

        ctk.CTkButton(scroll, text="Check for updates now", command=do_check).pack(
            padx=12, pady=8, anchor="w"
        )

        ctk.CTkLabel(scroll, text="Tips", font=ctk.CTkFont(weight="bold")).pack(
            padx=12, pady=(8, 4), anchor="w"
        )
        tips_var = ctk.BooleanVar(value=bool(data.get("show_tips_next_start")))
        ctk.CTkCheckBox(
            scroll,
            text="Show quick tips on next start",
            variable=tips_var,
        ).pack(padx=12, anchor="w")
        ctk.CTkButton(scroll, text="Show tips now", command=self._show_tips).pack(
            padx=12, pady=6, anchor="w"
        )

        def save() -> None:
            data["diagnostics_enabled"] = bool(var.get())
            data["remember_window"] = bool(remember_var.get())
            data["appearance_mode"] = mode_var.get()
            data["update_check_enabled"] = bool(upd_var.get())
            data["update_check_url"] = (url_var.get() or "").strip()
            data["show_tips_next_start"] = bool(tips_var.get())
            app_prefs.save_prefs(data)
            self._prefs = data
            try:
                ctk.set_appearance_mode(mode_var.get())
                bg = self._sash_bg()
                self._hpaned.configure(bg=bg)
                self._vpaned.configure(bg=bg)
            except Exception:
                pass
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        ctk.CTkButton(win, text="Save", command=save, width=100).pack(pady=10)

    def _about(self) -> None:
        data = app_prefs.load_prefs()
        win = ctk.CTkToplevel(self)
        win.title("About")
        win.geometry("480x360")
        win.minsize(400, 300)
        ctk.CTkLabel(
            win,
            text=(
                f"{__app_name__} {__version__}\n"
                "MIT · free forever · offline only\n"
                "No watermarks · no paid plan · no locked export quality\n\n"
                "Trim, convert, compress, edit looks, batch export.\n"
                "Preview is for timing; final quality is the exported file.\n\n"
                "Video/audio: ffmpeg · Images: Pillow\n"
                "Preview: OpenCV + local audio engine when available"
                + (
                    f"\n\nPortable data: {app_prefs.user_data_dir()}"
                    if app_prefs.is_portable_mode()
                    else ""
                )
            ),
            justify="left",
            wraplength=440,
        ).pack(padx=14, pady=14, anchor="w")

        def copy_diag() -> None:
            if not data.get("diagnostics_enabled"):
                messagebox.showinfo(__app_name__, "Enable diagnostics in Settings first.")
                return
            self.clipboard_clear()
            self.clipboard_append(build_report())
            messagebox.showinfo(__app_name__, "Diagnostics copied to clipboard.")

        ctk.CTkButton(win, text="Copy diagnostics", command=copy_diag).pack(pady=6)
        ctk.CTkButton(win, text="Close", command=win.destroy).pack(pady=4)

    def _show_keyboard_help(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Keyboard")
        win.geometry("400x340")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text=(
                "Space — Play → Out / Pause\n"
                "I / O — set In / Out\n"
                "← / → — frame step\n"
                "Ctrl+Z — undo\n"
                "Ctrl+Y — redo\n"
                "Ctrl+S — save session\n"
                "Ctrl+O — add files\n"
                "? — this help\n"
                "Esc — cancel export\n\n"
                "Timeline: drag track to scrub · green/red = marks\n"
                "Alt+drag selection = move range"
            ),
            justify="left",
        ).pack(padx=16, pady=16, anchor="w")
        ctk.CTkButton(win, text="Close", command=win.destroy).pack(pady=8)

