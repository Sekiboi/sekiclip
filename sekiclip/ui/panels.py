"""Main layout, tool tabs/panels, file list, DnD."""

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


class PanelsMixin:
    def _build(self) -> None:
        prefs = self._prefs
        left_w = int(prefs.get("left_pane_w") or app_prefs.DEFAULT_LEFT_W)
        right_w = int(prefs.get("right_pane_w") or app_prefs.DEFAULT_RIGHT_W)
        log_h = int(prefs.get("log_pane_h") or app_prefs.DEFAULT_LOG_H)

        # Header (fixed height)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(top, text=__app_name__, font=ctk.CTkFont(size=20, weight="bold")).pack(
            side="left"
        )
        ctk.CTkLabel(
            top,
            text="Offline visual editor · free forever",
            text_color=("gray40", "gray60"),
        ).pack(side="left", padx=10)
        self.ff_label = ctk.CTkLabel(top, text="", text_color=("gray40", "gray60"))
        self.ff_label.pack(side="right")

        # Vertical split: work area | status/log (user-draggable)
        sash_bg = self._sash_bg()
        self._vpaned = tk.PanedWindow(
            self,
            orient=tk.VERTICAL,
            sashwidth=6,
            sashrelief=tk.FLAT,
            bd=0,
            bg=sash_bg,
            opaqueresize=True,
        )
        self._vpaned.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        work = ctk.CTkFrame(self._vpaned, fg_color="transparent")
        bottom = ctk.CTkFrame(self._vpaned, fg_color="transparent")
        self._vpaned.add(work, stretch="always", minsize=280)
        self._vpaned.add(bottom, stretch="never", minsize=72)
        self._bottom_pane = bottom

        # Horizontal split: media | preview | tools
        self._hpaned = tk.PanedWindow(
            work,
            orient=tk.HORIZONTAL,
            sashwidth=6,
            sashrelief=tk.FLAT,
            bd=0,
            bg=sash_bg,
            opaqueresize=True,
        )
        self._hpaned.pack(fill="both", expand=True)

        left = ctk.CTkFrame(self._hpaned, width=left_w)
        center = ctk.CTkFrame(self._hpaned)
        right = ctk.CTkFrame(self._hpaned, width=right_w)
        self._left_pane = left
        self._center_pane = center
        self._right_pane = right
        self._hpaned.add(left, stretch="never", minsize=140)
        self._hpaned.add(center, stretch="always", minsize=360)
        self._hpaned.add(right, stretch="never", minsize=220)

        # ── Left: media list ──
        ctk.CTkLabel(left, text="Media", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.file_list = ctk.CTkScrollableFrame(left)
        self.file_list.pack(fill="both", expand=True, padx=6, pady=4)
        self._file_buttons: list[ctk.CTkButton] = []

        lb = ctk.CTkFrame(left, fg_color="transparent")
        lb.pack(fill="x", padx=6, pady=6)
        ctk.CTkButton(lb, text="Add…", width=56, command=self._add_files).pack(side="left", padx=2)
        ctk.CTkButton(lb, text="Clear", width=50, command=self._clear_files).pack(side="left", padx=2)
        ctk.CTkButton(lb, text="Recent", width=56, command=self._show_recent_menu).pack(
            side="left", padx=2
        )
        lb2 = ctk.CTkFrame(left, fg_color="transparent")
        lb2.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(lb2, text="Save session", width=90, command=self._save_session).pack(
            side="left", padx=2
        )
        ctk.CTkButton(lb2, text="Load session", width=90, command=self._load_session).pack(
            side="left", padx=2
        )

        # ── Center: expandable preview + fixed chrome below ──
        # Pack lower chrome first so the preview claims remaining height.
        self.queue_box = ctk.CTkTextbox(center, height=48)
        self.queue_box.pack(side="bottom", fill="x", padx=10, pady=(0, 6))
        # Placeholder text; kept in sync with _QUEUE_IDLE after each export
        self.queue_box.insert("1.0", "Batch queue appears here when exporting multiple files.\n")
        self.queue_box.configure(state="disabled")
        self._last_export_path: Path | None = None

        prog_fr = ctk.CTkFrame(center, fg_color="transparent")
        prog_fr.pack(side="bottom", fill="x", padx=10, pady=(0, 2))
        prow = ctk.CTkFrame(prog_fr, fg_color="transparent")
        prow.pack(fill="x")
        ctk.CTkLabel(prow, text="Export progress").pack(side="left")
        self.btn_cancel = ctk.CTkButton(
            prow, text="Cancel", width=70, command=self._cancel_export, state="disabled"
        )
        self.btn_cancel.pack(side="right")
        self.btn_open_folder = ctk.CTkButton(
            prow, text="Open folder", width=90, command=self._open_last_folder, state="disabled"
        )
        self.btn_open_folder.pack(side="right", padx=4)
        self.btn_export_again = ctk.CTkButton(
            prow, text="Export again", width=96, command=self._export_again, state="disabled"
        )
        self.btn_export_again.pack(side="right", padx=4)
        self.progress = ctk.CTkProgressBar(prog_fr)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=2)
        self.progress_label = ctk.CTkLabel(prog_fr, text="Idle", anchor="w")
        self.progress_label.pack(fill="x")

        self.range_label = ctk.CTkLabel(
            center,
            text="Space=play  I/O=marks  ←/→=frame  Drag handles · Drag sashes to resize panes",
            text_color=("gray40", "gray60"),
            anchor="w",
        )
        self.range_label.pack(side="bottom", fill="x", padx=10, pady=(0, 2))

        scrub_fr = ctk.CTkFrame(center, fg_color="transparent")
        scrub_fr.pack(side="bottom", fill="x", padx=10, pady=(2, 2))
        tl_row = ctk.CTkFrame(scrub_fr, fg_color="transparent")
        tl_row.pack(fill="x")
        ctk.CTkLabel(tl_row, text="Timeline — green=In, red=Out, yellow=playhead").pack(
            side="left"
        )
        ctk.CTkButton(tl_row, text="−", width=32, command=lambda: self._zoom(1.4)).pack(
            side="right", padx=2
        )
        ctk.CTkButton(tl_row, text="+", width=32, command=lambda: self._zoom(0.7)).pack(
            side="right", padx=2
        )
        ctk.CTkButton(tl_row, text="Fit", width=44, command=self._zoom_fit).pack(side="right", padx=2)
        ctk.CTkButton(tl_row, text="Sel", width=44, command=self._zoom_sel).pack(side="right", padx=2)

        self._tl_host = tk.Frame(scrub_fr, bg="#1a1a1e", height=56)
        self._tl_host.pack(fill="x", pady=4)
        self.timeline = RangeTimeline(
            self._tl_host,
            on_change=self._on_timeline_change,
            on_seek=self._on_timeline_seek,
            on_seek_end=self._on_timeline_seek_end,
            height=56,
            bg="#1a1a1e",
        )
        self.timeline.pack(fill="x", expand=True)

        time_fr = ctk.CTkFrame(center, fg_color="transparent")
        time_fr.pack(side="bottom", fill="x", padx=10, pady=2)
        ctk.CTkLabel(time_fr, text="In").pack(side="left")
        self.entry_in = ctk.CTkEntry(time_fr, width=88)
        self.entry_in.pack(side="left", padx=4)
        self.entry_in.bind("<Return>", lambda _e: self._apply_time_fields())
        ctk.CTkLabel(time_fr, text="Out").pack(side="left", padx=(8, 0))
        self.entry_out = ctk.CTkEntry(time_fr, width=88)
        self.entry_out.pack(side="left", padx=4)
        self.entry_out.bind("<Return>", lambda _e: self._apply_time_fields())
        ctk.CTkLabel(time_fr, text="Dur").pack(side="left", padx=(8, 0))
        self.entry_dur = ctk.CTkEntry(time_fr, width=88)
        self.entry_dur.pack(side="left", padx=4)
        self.entry_dur.bind("<Return>", lambda _e: self._apply_duration_field())
        ctk.CTkButton(time_fr, text="Apply times", width=90, command=self._apply_time_fields).pack(
            side="left", padx=6
        )
        self.io_label = ctk.CTkLabel(
            time_fr, text="Drag green/red handles on timeline", text_color=("gray40", "gray60")
        )
        self.io_label.pack(side="left", padx=8)

        transport = ctk.CTkFrame(center, fg_color="transparent")
        transport.pack(side="bottom", fill="x", padx=8, pady=4)
        # Play modes — Space / Play → Out · Loop cut = In→Out loop
        self.btn_play = ctk.CTkButton(
            transport, text="Play → Out", width=88, command=self._toggle_play
        )
        self.btn_play.pack(side="left", padx=2)
        ctk.CTkButton(transport, text="Stop", width=54, command=self._stop).pack(side="left", padx=2)
        ctk.CTkButton(transport, text="|◀", width=40, command=lambda: self._frame_step(-1)).pack(
            side="left", padx=1
        )
        ctk.CTkButton(transport, text="▶|", width=40, command=lambda: self._frame_step(1)).pack(
            side="left", padx=1
        )
        self.btn_loop_cut = ctk.CTkButton(
            transport, text="Loop cut", width=78, command=self._play_selection
        )
        self.btn_loop_cut.pack(side="left", padx=2)
        self.play_mode_label = ctk.CTkLabel(
            transport,
            text="",
            width=120,
            text_color=("gray40", "gray55"),
            font=ctk.CTkFont(size=11),
        )
        self.play_mode_label.pack(side="left", padx=(4, 2))
        self.time_label = ctk.CTkLabel(transport, text="00:00.00 / 00:00.00", width=130)
        self.time_label.pack(side="left", padx=6)
        ctk.CTkButton(transport, text="I", width=32, command=self._mark_in).pack(side="left", padx=1)
        ctk.CTkButton(transport, text="O", width=32, command=self._mark_out).pack(side="left", padx=1)
        ctk.CTkButton(transport, text="Clear", width=54, command=self._clear_io).pack(
            side="left", padx=2
        )

        self.info_line = ctk.CTkLabel(center, text="", anchor="w", text_color=("gray30", "gray70"))
        self.info_line.pack(side="bottom", fill="x", padx=10)

        # Preview fills remaining center space and resizes with the pane
        self.preview_frame = ctk.CTkFrame(
            center, fg_color=("#1a1a1e", "#141418"), corner_radius=6
        )
        self.preview_frame.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 4))
        self.preview_label = ctk.CTkLabel(
            self.preview_frame,
            text="Drop a video here, or click Add…\n"
            "Play includes sound once audio preview is ready\n\n"
            "Any size is fine · export keeps your quality settings",
            fg_color="transparent",
            text_color=("gray50", "gray60"),
            justify="center",
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self._tk_preview = None
        self.preview_label.bind("<ButtonPress-1>", self._crop_press)
        self.preview_label.bind("<B1-Motion>", self._crop_motion)
        self.preview_label.bind("<ButtonRelease-1>", self._crop_release)
        self.preview_frame.bind("<Configure>", self._on_preview_configure)

        # ── Right: tools ──
        tools_hdr = ctk.CTkFrame(right, fg_color="transparent")
        tools_hdr.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(tools_hdr, text="Tools", font=ctk.CTkFont(weight="bold")).pack(
            side="left"
        )
        ctk.CTkLabel(
            tools_hdr,
            text="scroll →",
            text_color=("gray50", "gray55"),
            font=ctk.CTkFont(size=11),
        ).pack(side="right")

        self._tool_names = list(TOOL_NAMES)
        self._tool_var = ctk.StringVar(value="Trim")
        self._tool_buttons: dict[str, ctk.CTkButton] = {}
        self._tool_tabs = ctk.CTkScrollableFrame(
            right,
            orientation="horizontal",
            height=44,
            fg_color=("gray90", "gray17"),
            corner_radius=8,
            scrollbar_button_color=("gray70", "gray35"),
        )
        self._tool_tabs.pack(fill="x", padx=8, pady=(2, 4))
        for name in self._tool_names:
            btn = ctk.CTkButton(
                self._tool_tabs,
                text=name,
                width=76,
                height=28,
                corner_radius=6,
                font=ctk.CTkFont(size=12),
                command=lambda n=name: self._select_tool(n),
            )
            btn.pack(side="left", padx=3, pady=4)
            self._tool_buttons[name] = btn
        self.tool = self._tool_var
        self._bind_tool_tabs_scroll()
        self._style_tool_tabs("Trim")

        self.tool_frame = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.tool_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self._panels: dict[str, ctk.CTkFrame] = {}
        self._build_tool_panels()
        self._bind_preview_traces()
        self._sync_preview_audio()

        self.var_batch = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            right, text="Batch all files in list", variable=self.var_batch
        ).pack(anchor="w", padx=10, pady=(4, 0))

        ctk.CTkButton(
            right,
            text="Export / Save as…",
            height=36,
            command=self._run,
            font=ctk.CTkFont(weight="bold"),
        ).pack(fill="x", padx=10, pady=8)
        self._tools_hint = ctk.CTkLabel(
            right,
            text="Single file: Save As dialog.\n"
            "Batch: pick an output folder.\n"
            "Trim/GIF use timeline In/Out.\n"
            "Drag pane edges to resize.",
            wraplength=max(200, right_w - 30),
            justify="left",
            text_color=("gray40", "gray60"),
        )
        self._tools_hint.pack(padx=10, pady=(0, 8))
        right.bind("<Configure>", self._on_right_pane_configure)

        # ── Bottom: status + resizable log ──
        row = ctk.CTkFrame(bottom, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(4, 0))
        self.status = ctk.CTkLabel(row, text="Ready", anchor="w")
        self.status.pack(side="left")
        ctk.CTkButton(row, text="About", width=70, command=self._about).pack(side="right", padx=4)
        ctk.CTkButton(row, text="Tips", width=56, command=self._show_tips).pack(side="right", padx=2)
        ctk.CTkButton(row, text="Settings", width=80, command=self._settings).pack(side="right")
        self.log = ctk.CTkTextbox(bottom, height=max(60, log_h - 28))
        self.log.pack(fill="both", expand=True, padx=4, pady=(4, 4))
        self.log.insert(
            "1.0",
            "Drop a file on the window, or click Add… (any size is fine).\n"
            "Scrub the timeline, set In/Out, then Export.\n"
            "Tip: drag the gray bars between panes · resize the window freely.\n",
        )
        self.log.configure(state="disabled")

        # Drag-and-drop on root (works with CTk). Size is never a limit.
        self._setup_drag_drop()

        # Periodic session integrity (marks, dead A/V, UI resync)
        self.after(800, self._integrity_tick)

        # Apply sash positions after first layout pass
        self.after(80, lambda: self._apply_pane_sizes(left_w, right_w, log_h))
        self.after(200, self._mark_layout_ready)

    def _build_tool_panels(self) -> None:
        self.var_fmt = ctk.StringVar(value="mp4")
        self.var_preset = ctk.StringVar(value="balanced")
        self.var_reencode = ctk.BooleanVar(value=True)
        self.var_audio_fmt = ctk.StringVar(value="mp3")
        self.var_max_edge = ctk.StringVar(value="1920")
        self.var_quality = ctk.StringVar(value="75")
        self.var_degrees = ctk.StringVar(value="90")
        self.var_bitrate = ctk.StringVar(value="128k")
        self.var_audio_action = ctk.StringVar(value="extract")
        self.var_image_action = ctk.StringVar(value="compress")
        self.var_more = ctk.StringVar(value="remux")
        # Edit tab (UI labels map to internal keys via session_store)
        self.var_edit_action = ctk.StringVar(value="render_cut")
        self.var_crop_margin = ctk.StringVar(value="40")
        self.var_volume = ctk.StringVar(value="1.0")
        self.var_mute = ctk.BooleanVar(value=False)
        self.var_speed = ctk.StringVar(value="1.0")
        self.var_gif_fmt = ctk.StringVar(value="gif")
        self.var_fade_in = ctk.StringVar(value="0.5")
        self.var_fade_out = ctk.StringVar(value="0.5")
        self.var_v_fade_in = ctk.StringVar(value="0.5")
        self.var_v_fade_out = ctk.StringVar(value="0.5")
        self.var_a_fade_in = ctk.StringVar(value="0.5")
        self.var_a_fade_out = ctk.StringVar(value="0.5")
        self.var_fade_video = ctk.BooleanVar(value=True)
        self.var_fade_audio = ctk.BooleanVar(value=True)
        self.var_use_crop = ctk.BooleanVar(value=False)
        self.var_use_logo = ctk.BooleanVar(value=False)
        self.var_use_subs = ctk.BooleanVar(value=False)
        self.var_max_mb = ctk.StringVar(value="25")
        self.var_logo_pos = ctk.StringVar(value="top-right")
        self.var_logo_scale = ctk.StringVar(value="0.15")
        # Export quality (Edit + Trim re-encodes): familiar 1080p / kbps pickers.
        self.var_video_quality = ctk.StringVar(value=VIDEO_QUALITY_DEFAULT_LABEL)
        self.var_audio_quality = ctk.StringVar(value=AUDIO_QUALITY_DEFAULT_LABEL)
        # U2 options (all free; hardware best-effort)
        self.var_prefer_gpu = ctk.BooleanVar(value=False)
        self.var_use_proxy = ctk.BooleanVar(value=False)
        self.var_loud_i = ctk.StringVar(value="-16")
        self.var_loud_tp = ctk.StringVar(value="-1.5")
        self._srt_path: Path | None = None
        self._logo_path: Path | None = None

        for name in ("Convert", "Compress", "Trim", "Edit", "Audio", "Image", "More"):
            self._panels[name] = ctk.CTkFrame(self.tool_frame, fg_color="transparent")

        fr = self._panels["Convert"]
        ctk.CTkLabel(fr, text="Format").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_fmt,
            values=["mp4", "webm", "mkv", "mov", "mp3", "wav", "png", "jpg", "webp"],
        ).pack(fill="x", pady=4)

        fr = self._panels["Compress"]
        ctk.CTkLabel(fr, text="Preset").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_preset, values=list(ops.COMPRESS_PRESETS)
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="chat · discord · whatsapp · email · 720p · 1080p\n"
            "balanced · quality · fast_gpu",
            wraplength=250,
            justify="left",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w")
        ctk.CTkLabel(fr, text="Image quality (1–100)").pack(anchor="w", pady=(6, 0))
        ctk.CTkEntry(fr, textvariable=self.var_quality).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Max edge (px)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Audio bitrate").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_bitrate).pack(fill="x", pady=2)

        fr = self._panels["Trim"]
        ctk.CTkLabel(
            fr,
            text="Uses timeline In/Out.\n"
            "Stream copy = no loss (keyframe snap).\n"
            "Re-encode = accurate + Edit looks.",
            justify="left",
            wraplength=260,
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        ).pack(anchor="w", pady=4)
        ctk.CTkCheckBox(fr, text="Re-encode (accurate)", variable=self.var_reencode).pack(
            anchor="w", pady=4
        )
        ctk.CTkLabel(fr, text="Quick quality").pack(anchor="w", pady=(4, 0))
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_export_preset,
            values=list(session_store.EXPORT_PRESET_LABELS),
            command=self._apply_export_preset,
        ).pack(fill="x", pady=2)
        self._pack_export_quality_ui(fr, when_reencoding=True)
        ctk.CTkCheckBox(
            fr,
            text="GPU encode if available",
            variable=self.var_prefer_gpu,
        ).pack(anchor="w", pady=2)
        ctk.CTkCheckBox(
            fr,
            text="Scrub proxy (smoother, builds once)",
            variable=self.var_use_proxy,
            command=self._on_proxy_toggle,
        ).pack(anchor="w", pady=2)
        ctk.CTkLabel(
            fr,
            text="GPU/proxy fall back if unsupported",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        ).pack(anchor="w")
        ctk.CTkButton(fr, text="Go to In", command=lambda: self._goto_mark("in")).pack(
            fill="x", pady=2
        )
        ctk.CTkButton(fr, text="Go to Out", command=lambda: self._goto_mark("out")).pack(
            fill="x", pady=2
        )

        fr = self._panels["Edit"]
        ctk.CTkLabel(
            fr,
            text="Preview = export. Fades use In→Out cut.",
            wraplength=260,
            justify="left",
            text_color=("gray30", "gray70"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(
            fr,
            text="Reset looks",
            height=28,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            command=self._reset_edit_looks,
        ).pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_edit_action_ui,
            values=list(session_store.EDIT_ACTION_LABELS),
            command=self._on_edit_action_ui,
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Quick quality").pack(anchor="w", pady=(6, 0))
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_export_preset,
            values=list(session_store.EXPORT_PRESET_LABELS),
            command=self._apply_export_preset,
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(
            fr,
            text="Sets video + audio quality for re-encodes",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        ).pack(anchor="w")
        self._pack_export_quality_ui(fr, when_reencoding=False)

        ctk.CTkLabel(fr, text="— Fades (sec) —").pack(anchor="w", pady=(6, 2))
        ctk.CTkCheckBox(fr, text="Video fade", variable=self.var_fade_video).pack(anchor="w")
        row_vf = ctk.CTkFrame(fr, fg_color="transparent")
        row_vf.pack(fill="x")
        ctk.CTkLabel(row_vf, text="In").pack(side="left")
        ctk.CTkEntry(row_vf, width=50, textvariable=self.var_v_fade_in).pack(side="left", padx=2)
        ctk.CTkLabel(row_vf, text="Out").pack(side="left")
        ctk.CTkEntry(row_vf, width=50, textvariable=self.var_v_fade_out).pack(side="left", padx=2)
        ctk.CTkCheckBox(fr, text="Audio fade", variable=self.var_fade_audio).pack(anchor="w")
        row_af = ctk.CTkFrame(fr, fg_color="transparent")
        row_af.pack(fill="x")
        ctk.CTkLabel(row_af, text="In").pack(side="left")
        ctk.CTkEntry(row_af, width=50, textvariable=self.var_a_fade_in).pack(side="left", padx=2)
        ctk.CTkLabel(row_af, text="Out").pack(side="left")
        ctk.CTkEntry(row_af, width=50, textvariable=self.var_a_fade_out).pack(side="left", padx=2)

        ctk.CTkLabel(fr, text="— Volume / speed —").pack(anchor="w", pady=(6, 2))
        ctk.CTkLabel(fr, text="Volume (0–200%)").pack(anchor="w")
        self.volume_slider = ctk.CTkSlider(
            fr, from_=0, to=2.0, number_of_steps=40, command=self._on_volume_slider
        )
        self.volume_slider.set(1.0)
        self.volume_slider.pack(fill="x", pady=2)
        self.volume_label = ctk.CTkLabel(fr, text="100%")
        self.volume_label.pack(anchor="w")
        ctk.CTkCheckBox(fr, text="Mute", variable=self.var_mute).pack(anchor="w", pady=2)
        ctk.CTkLabel(fr, text="Speed").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_speed, values=list(ops.SPEED_PRESETS)
        ).pack(fill="x", pady=2)

        ctk.CTkLabel(fr, text="— Crop / subs / logo —").pack(anchor="w", pady=(6, 2))
        ctk.CTkCheckBox(fr, text="Use crop", variable=self.var_use_crop).pack(anchor="w")
        ctk.CTkButton(fr, text="Edit crop", command=self._toggle_crop_mode).pack(
            fill="x", pady=2
        )
        ctk.CTkLabel(fr, text="Crop margin (px)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_crop_margin).pack(fill="x", pady=2)
        ctk.CTkCheckBox(fr, text="Burn subtitles", variable=self.var_use_subs).pack(anchor="w")
        ctk.CTkButton(fr, text="Choose .srt…", command=self._pick_srt).pack(
            fill="x", pady=2
        )
        ctk.CTkCheckBox(fr, text="Logo overlay", variable=self.var_use_logo).pack(anchor="w")
        ctk.CTkButton(fr, text="Choose logo…", command=self._pick_logo).pack(
            fill="x", pady=2
        )
        ctk.CTkButton(fr, text="Logo ghost", command=self._toggle_logo_ghost).pack(
            fill="x", pady=2
        )
        ctk.CTkLabel(fr, text="Logo position").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_logo_pos,
            values=["top-right", "top-left", "bottom-right", "bottom-left", "center"],
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Logo scale (0–1)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_logo_scale).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="GIF format").pack(anchor="w")
        ctk.CTkOptionMenu(fr, variable=self.var_gif_fmt, values=["gif", "webp"]).pack(
            fill="x", pady=2
        )
        ctk.CTkLabel(fr, text="Max size (MB)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_max_mb).pack(fill="x", pady=2)
        ctk.CTkCheckBox(
            fr,
            text="GPU encode if available",
            variable=self.var_prefer_gpu,
        ).pack(anchor="w", pady=(6, 2))
        ctk.CTkCheckBox(
            fr,
            text="Scrub proxy (smoother, builds once)",
            variable=self.var_use_proxy,
            command=self._on_proxy_toggle,
        ).pack(anchor="w", pady=2)
        # keep shared fade fields in sync helpers
        self.var_fade_in = self.var_v_fade_in
        self.var_fade_out = self.var_v_fade_out
        self.edit_files_label = ctk.CTkLabel(
            fr, text="No .srt / logo", text_color=("gray40", "gray60"), wraplength=250
        )
        self.edit_files_label.pack(anchor="w", pady=4)

        fr = self._panels["Audio"]
        ctk.CTkLabel(
            fr,
            text="Pull audio from video or process audio files.",
            wraplength=260,
            justify="left",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_audio_action,
            values=["extract", "convert", "normalize", "mono", "compress", "volume"],
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Format").pack(anchor="w")
        ctk.CTkOptionMenu(fr, variable=self.var_audio_fmt, values=list(ops.AUDIO_FORMATS)).pack(
            fill="x", pady=2
        )
        ctk.CTkLabel(fr, text="Volume (volume action)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_volume).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Loudnorm I (LUFS)").pack(anchor="w", pady=(6, 0))
        ctk.CTkEntry(fr, textvariable=self.var_loud_i).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="True peak (dBTP)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_loud_tp).pack(fill="x", pady=2)
        ctk.CTkLabel(
            fr,
            text="Used for Action = normalize (single-pass)",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray60"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            fr,
            text="1.0 = 100% · only used when Action = volume",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w")
        ctk.CTkLabel(fr, text="Bitrate (compress)").pack(anchor="w", pady=(6, 0))
        ctk.CTkEntry(fr, textvariable=self.var_bitrate).pack(fill="x", pady=2)
        ctk.CTkLabel(
            fr,
            text="e.g. 128k · only for Action = compress",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w")

        fr = self._panels["Image"]
        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_image_action,
            values=["compress", "resize", "convert", "rotate", "flip", "strip_exif", "to_pdf"],
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Max edge (px)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Rotate (°)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_degrees).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Quality (1–100)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_quality).pack(fill="x", pady=2)

        fr = self._panels["More"]
        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_more,
            values=["remux", "strip_audio", "frame", "rotate_video", "flip_video", "concat"],
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(
            fr,
            text="frame = still at playhead\nconcat = all videos in list",
            wraplength=260,
            justify="left",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", pady=(4, 0))

        # Default to Edit so fades/looks are on the main path (WYSIWYG + export)
        self._select_tool("Edit")

    def _pick_srt(self) -> None:
        p = filedialog.askopenfilename(
            title="Subtitle file",
            filetypes=[("SubRip", "*.srt"), ("All", "*.*")],
        )
        if p:
            self._srt_path = Path(p)
            self._update_edit_files_label()

    def _pick_logo(self) -> None:
        p = filedialog.askopenfilename(
            title="Logo image",
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.webp"), ("All", "*.*")],
        )
        if p:
            self._logo_path = Path(p)
            self._update_edit_files_label()

    def _update_edit_files_label(self) -> None:
        parts = []
        if self._srt_path:
            parts.append(f"SRT: {self._srt_path.name}")
        if self._logo_path:
            parts.append(f"Logo: {self._logo_path.name}")
        if hasattr(self, "edit_files_label"):
            self.edit_files_label.configure(
                text=" · ".join(parts) if parts else "No .srt / logo chosen"
            )
        self._on_edit_setting_changed()

    def _select_tool(self, name: str) -> None:
        """Switch active tool tab and highlight its pill button."""
        if name not in self._tool_names:
            return
        self._tool_var.set(name)
        self._style_tool_tabs(name)
        self._on_tool_change(name)
        # Keep selected pill in view when chosen via code or click near edge
        try:
            btn = self._tool_buttons.get(name)
            if btn is not None:
                self._tool_tabs._parent_canvas.see(btn)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _style_tool_tabs(self, active: str) -> None:
        for n, btn in self._tool_buttons.items():
            if n == active:
                btn.configure(
                    fg_color=("#3B8ED0", "#1F6AA5"),
                    hover_color=("#36719F", "#144870"),
                    text_color=("white", "white"),
                )
            else:
                btn.configure(
                    fg_color=("gray80", "gray28"),
                    hover_color=("gray70", "gray35"),
                    text_color=("gray10", "gray90"),
                )

    def _bind_tool_tabs_scroll(self) -> None:
        """Mouse wheel / trackpad: fast horizontal scroll on tool pills."""

        def _wheel_units(event: Any) -> int:
            """Map OS wheel event → scroll units (higher = more sensitive)."""
            d = int(getattr(event, "delta", 0) or 0)
            if d:
                # Windows: ±120 per notch. Aim ~6–8 units/notch (was 2).
                steps = int(-d / 15)  # 120 → 8 units
                if steps == 0:
                    steps = -1 if d > 0 else 1
                return steps
            num = getattr(event, "num", None)
            if num == 4:
                return -8
            if num == 5:
                return 8
            return 0

        def _wheel_tabs(event: Any) -> str | None:
            canvas = getattr(self._tool_tabs, "_parent_canvas", None)
            if canvas is None:
                return None
            units = _wheel_units(event)
            if units:
                canvas.xview_scroll(units, "units")
            return "break"

        def _wheel_tools(event: Any) -> str | None:
            """Vertical scroll for the tool options panel (also snappier)."""
            canvas = getattr(self.tool_frame, "_parent_canvas", None)
            if canvas is None:
                return None
            units = _wheel_units(event)
            if units:
                canvas.yview_scroll(units, "units")
            return "break"

        widgets_h = [self._tool_tabs]
        try:
            widgets_h.append(self._tool_tabs._parent_canvas)  # type: ignore[attr-defined]
        except Exception:
            pass
        for w in widgets_h:
            w.bind("<MouseWheel>", _wheel_tabs)
            w.bind("<Shift-MouseWheel>", _wheel_tabs)
            w.bind("<Button-4>", _wheel_tabs)
            w.bind("<Button-5>", _wheel_tabs)
        for btn in self._tool_buttons.values():
            btn.bind("<MouseWheel>", _wheel_tabs)
            btn.bind("<Shift-MouseWheel>", _wheel_tabs)
            btn.bind("<Button-4>", _wheel_tabs)
            btn.bind("<Button-5>", _wheel_tabs)

        # Options body under the pills
        self.after(100, self._bind_tool_panel_scroll)

    def _bind_tool_panel_scroll(self) -> None:
        """Bind accelerated vertical wheel on tool options (Edit, Audio, …)."""

        def _wheel_units(event: Any) -> int:
            d = int(getattr(event, "delta", 0) or 0)
            if d:
                steps = int(-d / 15)
                return steps if steps else (-1 if d > 0 else 1)
            num = getattr(event, "num", None)
            if num == 4:
                return -8
            if num == 5:
                return 8
            return 0

        def _wheel(event: Any) -> str | None:
            canvas = getattr(self.tool_frame, "_parent_canvas", None)
            if canvas is None:
                return None
            units = _wheel_units(event)
            if units:
                canvas.yview_scroll(units, "units")
            return "break"

        targets: list[Any] = [self.tool_frame]
        try:
            targets.append(self.tool_frame._parent_canvas)  # type: ignore[attr-defined]
        except Exception:
            pass
        for fr in self._panels.values():
            targets.append(fr)
        for w in targets:
            try:
                w.bind("<MouseWheel>", _wheel)
                w.bind("<Shift-MouseWheel>", _wheel)
                w.bind("<Button-4>", _wheel)
                w.bind("<Button-5>", _wheel)
            except Exception:
                pass
        # Deep-bind common children so wheel works over labels/entries
        try:
            for fr in self._panels.values():
                for child in fr.winfo_children():
                    try:
                        child.bind("<MouseWheel>", _wheel)
                        child.bind("<Button-4>", _wheel)
                        child.bind("<Button-5>", _wheel)
                    except Exception:
                        pass
        except Exception:
            pass

    def _on_tool_change(self, name: str) -> None:
        for n, fr in self._panels.items():
            if n == name:
                fr.pack(fill="both", expand=True)
            else:
                fr.pack_forget()
        # Re-bind wheel on newly shown panel children
        self.after(30, self._bind_tool_panel_scroll)

    # ── files ───────────────────────────────────────────────

    def _refresh_ffmpeg_status(self) -> None:
        self.ff_label.configure(text="ffmpeg: OK" if ops.find_ffmpeg() else "ffmpeg: MISSING")

    def _log(self, msg: str) -> None:
        """Append a line to the bottom output log (main thread)."""
        try:
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            # Keep log from growing forever
            try:
                end_idx = self.log.index("end-1c")
                line_count = int(float(end_idx.split(".")[0]))
                if line_count > 500:
                    self.log.delete("1.0", f"{line_count - 400}.0")
            except Exception:
                pass
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        self.status.configure(text=msg)

    def _rebuild_file_list(self) -> None:
        for b in self._file_buttons:
            b.destroy()
        self._file_buttons.clear()
        for i, p in enumerate(self._files):
            idx = i
            btn = ctk.CTkButton(
                self.file_list,
                text=p.name[:40],
                anchor="w",
                fg_color=("gray75", "gray30") if i == self._selected_idx else ("gray85", "gray25"),
                command=lambda i=idx: self._select_file(i),
            )
            btn.pack(fill="x", pady=2)
            self._file_buttons.append(btn)

    def _setup_drag_drop(self) -> None:
        """Accept file drops on the whole window (size does not matter)."""
        if not _HAS_DND:
            self._log(
                "Drag-and-drop unavailable (install tkinterdnd2). Use Add… to open files."
            )
            return
        try:
            # Root has TkinterDnD.DnDWrapper — this is what actually works with CTk
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Drag-and-drop setup failed: {exc}. Use Add… instead.")
            return
        # Best-effort: also bind common child surfaces (ignore failures)
        for widget in (
            getattr(self, "preview_frame", None),
            getattr(self, "preview_label", None),
            getattr(self, "file_list", None),
            getattr(self, "_left_pane", None),
            getattr(self, "_center_pane", None),
        ):
            if widget is None:
                continue
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add media (any size)",
            filetypes=[
                (
                    "Video",
                    "*.mp4;*.mkv;*.webm;*.mov;*.avi;*.m4v;*.wmv;*.mpeg;*.mpg;*.ts;*.mts",
                ),
                (
                    "Audio",
                    "*.mp3;*.wav;*.flac;*.m4a;*.ogg;*.aac;*.wma;*.opus",
                ),
                (
                    "Images",
                    "*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif;*.tif;*.tiff",
                ),
                ("All files", "*.*"),
            ],
        )
        added = 0
        for p in paths:
            path = Path(p)
            if path.is_file() and path not in self._files:
                self._files.append(path)
                session_store.push_recent_file(path)
                added += 1
        self._rebuild_file_list()
        if added:
            self._select_file(len(self._files) - 1)
        elif self._files and self._selected_idx < 0:
            self._select_file(0)

    def _clear_files(self) -> None:
        self._session.close()
        self._files.clear()
        self._selected_idx = -1
        self._rebuild_file_list()
        self._preview_photo = None
        if self._tk_preview is not None:
            self._tk_preview.place_forget()
        self.preview_label.configure(
            image=None,
            text="Drop a video here, or click Add…\n"
            "Any size is fine · export keeps your quality settings",
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self.info_line.configure(text="")
        self._update_time_labels(0, 0)

    def _on_drop(self, event) -> None:  # type: ignore[no-untyped-def]
        """Handle Explorer / Finder drop. File size is never a filter."""
        data = getattr(event, "data", None) or ""
        paths = _parse_drop(data)
        if not paths:
            self._set_status("Drop ignored — could not read file path(s)")
            self._log(
                "Drop received but no readable files "
                f"(payload preview: {str(data)[:180]!r}). Use Add… if this keeps happening."
            )
            return
        added = 0
        for p in paths:
            if p not in self._files:
                self._files.append(p)
                session_store.push_recent_file(p)
                added += 1
        self._rebuild_file_list()
        if self._files:
            self._select_file(len(self._files) - 1)
        try:
            sizes = ", ".join(
                f"{p.name} ({format_bytes(p.stat().st_size)})" for p in paths[:3]
            )
        except OSError:
            sizes = ", ".join(p.name for p in paths[:3])
        more = f" +{len(paths) - 3} more" if len(paths) > 3 else ""
        self._log(f"Dropped {len(paths)} file(s): {sizes}{more}")
        self._set_status(f"Added {added} file(s)" if added else "File already in list")

    def _select_file(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._files):
            return
        self._selected_idx = idx
        self._rebuild_file_list()
        path = self._files[idx]
        session_store.push_recent_file(path)
        self._set_status(f"Loading {path.name}…")
        self._session.stop()

        def load() -> None:
            try:
                # Don't fire UI callbacks from worker until open finishes.
                prev_frame = self._session.on_frame
                prev_pos = self._session.on_position
                self._session.on_frame = None
                self._session.on_position = None
                info = self._session.open(path)
                self._session.on_frame = prev_frame
                self._session.on_position = prev_pos
                self.after(0, lambda: self._on_loaded(info.summary, info.duration))
            except Exception as exc:  # noqa: BLE001
                self._session.on_frame = self._on_session_frame
                self._session.on_position = self._on_session_position
                self.after(0, lambda: self._load_failed(str(exc)))

        threading.Thread(target=load, daemon=True).start()

    def _on_loaded(self, summary: str, duration: float) -> None:
        self.info_line.configure(text=summary)
        self._set_status("Ready — drag In/Out handles, then Export")
        self.timeline.set_duration(max(duration, 0.001))
        # Keep marks if loading session restored them already
        if self._session.in_point <= 0 and (
            self._session.out_point is None or self._session.out_point >= duration - 1e-3
        ):
            self._session.in_point = 0.0
            self._session.out_point = duration if duration > 0 else None
        self._update_time_labels(self._session.position, duration)
        self._update_io_label()
        self._sync_time_fields()
        self._set_transport_playing(False)
        self._integrity_log_keys.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        try:
            self._session.seek(self._session.position if self._session.position > 0 else 0.0)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Preview seek: {exc}")
        self._push_undo(force=True)

    def _load_failed(self, err: str) -> None:
        self._set_status("Load failed")
        self._log(f"Preview error: {err}")
        messagebox.showerror(__app_name__, f"Could not open for preview:\n{err}")

    # ── preview / timeline ──────────────────────────────────

    def _edit_action_key(self) -> str:
        lab = (self.var_edit_action_ui.get() or "").strip()
        key = session_store.EDIT_LABEL_TO_KEY.get(lab)
        if key:
            return key
        # raw key still accepted
        if lab in session_store.EDIT_KEY_TO_LABEL:
            return lab
        return "render_cut"

    def _on_edit_action_ui(self, _choice: str | None = None) -> None:
        self.var_edit_action.set(self._edit_action_key())
        self._on_edit_setting_changed()

    def _on_proxy_toggle(self) -> None:
        self._session.use_scrub_proxy = bool(self.var_use_proxy.get())
        if self._session.use_scrub_proxy and self._current_path():
            self._set_status("Scrub proxy on — re-open file to build if needed")
            self._log("Scrub proxy enabled (re-select file to generate)")
        else:
            self._set_status("Scrub proxy off")

    def _live_settings(self) -> dict[str, Any]:
        """Current UI edit settings — single source for preview and export."""
        return self._live_look().to_dict()

    def _live_look(self) -> Look:
        """Typed look used by preview, session, and export."""
        self.var_edit_action.set(self._edit_action_key())
        return Look(
            edit_action=self._edit_action_key(),
            video_quality=_normalize_video_quality(self.var_video_quality.get()),
            audio_quality=_normalize_audio_quality(self.var_audio_quality.get()),
            fade_video=bool(self.var_fade_video.get()),
            fade_audio=bool(self.var_fade_audio.get()),
            v_fade_in=str(self.var_v_fade_in.get() or "0.5"),
            v_fade_out=str(self.var_v_fade_out.get() or "0.5"),
            a_fade_in=str(self.var_a_fade_in.get() or "0.5"),
            a_fade_out=str(self.var_a_fade_out.get() or "0.5"),
            mute=bool(self.var_mute.get()),
            volume=str(self.var_volume.get() or "1.0"),
            speed=str(self.var_speed.get() or "1.0"),
            use_crop=bool(self.var_use_crop.get()),
            use_logo=bool(self.var_use_logo.get()),
            use_subs=bool(self.var_use_subs.get()),
            logo_pos=str(self.var_logo_pos.get() or "top-right"),
            logo_scale=str(self.var_logo_scale.get() or "0.15"),
            crop_margin=str(self.var_crop_margin.get() or "40"),
            crop_rect=tuple(self._crop_rect),  # type: ignore[arg-type]
            srt_path=self._srt_path,
            logo_path=self._logo_path,
            logo_ghost=bool(self._logo_ghost),
            gif_fmt=str(self.var_gif_fmt.get() or "gif"),
            max_mb=str(self.var_max_mb.get() or "25"),
        )

    def _reset_edit_looks(self) -> None:
        """Reset fades/volume/crop/logo toggles to defaults (keeps timeline In/Out)."""
        self._suppress_preview_trace = True
        try:
            self.var_edit_action.set("render_cut")
            self.var_edit_action_ui.set(
                session_store.EDIT_KEY_TO_LABEL.get("render_cut", "Full cut")
            )
            self.var_video_quality.set(VIDEO_QUALITY_DEFAULT_LABEL)
            self.var_audio_quality.set(AUDIO_QUALITY_DEFAULT_LABEL)
            self.var_fade_video.set(True)
            self.var_fade_audio.set(True)
            self.var_v_fade_in.set("0.5")
            self.var_v_fade_out.set("0.5")
            self.var_a_fade_in.set("0.5")
            self.var_a_fade_out.set("0.5")
            self.var_mute.set(False)
            self.var_volume.set("1.0")
            try:
                self.volume_slider.set(1.0)
                self.volume_label.configure(text="100%")
            except Exception:
                pass
            self.var_speed.set("1.0")
            self.var_use_crop.set(False)
            self.var_use_logo.set(False)
            self.var_use_subs.set(False)
            self.var_logo_pos.set("top-right")
            self.var_logo_scale.set("0.15")
            self.var_crop_margin.set("40")
            self._crop_rect = (0.1, 0.1, 0.9, 0.9)
            self._crop_mode = False
            self._logo_ghost = False
            self._srt_path = None
            self._logo_path = None
            self._update_edit_files_label()
        finally:
            self._suppress_preview_trace = False
        self._sync_preview_audio()
        self._repaint_preview_from_cache()
        self._set_status("Looks reset — timeline In/Out unchanged")
        self._log("Reset edit looks to defaults.")

    def _bind_preview_traces(self) -> None:
        """Any look change updates the preview immediately (WYSIWYG = export)."""
        for name in (
            "var_fade_video",
            "var_fade_audio",
            "var_v_fade_in",
            "var_v_fade_out",
            "var_a_fade_in",
            "var_a_fade_out",
            "var_mute",
            "var_volume",
            "var_speed",
            "var_use_crop",
            "var_use_logo",
            "var_use_subs",
            "var_logo_pos",
            "var_logo_scale",
            "var_edit_action",
            "var_edit_action_ui",
            "var_video_quality",
            "var_audio_quality",
            "var_crop_margin",
        ):
            v = getattr(self, name, None)
            if v is not None:
                try:
                    v.trace_add("write", lambda *_a: self._on_edit_setting_changed())
                except Exception:
                    pass

    def _on_edit_setting_changed(self) -> None:
        """Live: sync play filters + refresh video (debounced)."""
        if self._suppress_preview_trace or self._undo_suspend:
            return
        if not self._undo_suspend:
            # Debounced snapshot so slider drags don't flood the stack
            if getattr(self, "_undo_look_job", None) is not None:
                try:
                    self.after_cancel(self._undo_look_job)
                except Exception:
                    pass
            self._undo_look_job = self.after(350, lambda: self._push_undo())
        self._sync_preview_audio()
        # Debounce audio restart — respawning ffplay every keystroke is multi-second lag
        try:
            if self._session.playing and not self._scrub_dragging:
                self._schedule_audio_restart()
        except Exception:
            pass
        if self._preview_resize_job is not None:
            try:
                self.after_cancel(self._preview_resize_job)
            except Exception:
                pass
        # Throttled look repaint (video-only; export quality unaffected)
        self._preview_resize_job = self.after(40, self._repaint_preview_from_cache)

