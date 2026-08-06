"""Clipwork GUI — offline media editor with visual preview. Free forever."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any

from PIL import Image

from clipwork import __app_name__, __version__
from clipwork import jobs
from clipwork import media_ops as ops
from clipwork import prefs as app_prefs
from clipwork.diagnostics import build_report
from clipwork.media_ops.ffmpeg_util import (
    CancelledError,
    commit_staged,
    paths_same,
    request_cancel,
    staging_path,
)
from clipwork.media_preview import (
    MediaSession,
    format_time,
    run_ffmpeg_with_progress,
)
from clipwork.timeline_widget import RangeTimeline

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

APP_USER_MODEL_ID = "Sekiboi.Clipwork"
# Comfortable floor for laptops; panes remain usable below classic 1100×720.
MIN_W, MIN_H = 960, 600
# Default / fallback preview stage (16:9). Actual stage follows the resizable pane.
PREVIEW_W, PREVIEW_H = 960, 540
PREVIEW_MAX = (PREVIEW_W, PREVIEW_H)
PREVIEW_MIN = (320, 180)

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = ops.IMAGE_EXTS


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base.joinpath(*parts)
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def _parse_drop(data: str) -> list[Path]:
    out: list[Path] = []
    token = ""
    in_brace = False
    for ch in data:
        if ch == "{":
            in_brace = True
            token = ""
        elif ch == "}":
            in_brace = False
            if token:
                out.append(Path(token))
            token = ""
        elif ch == " " and not in_brace:
            if token:
                out.append(Path(token))
            token = ""
        else:
            token += ch
    if token:
        out.append(Path(token))
    return [p for p in out if p.is_file()]


def _fit_image(img: Image.Image, max_size: tuple[int, int] = PREVIEW_MAX) -> Image.Image:
    """Letterbox into a fixed stage so framing stays stable while scrubbing/playing."""
    stage_w, stage_h = max_size
    src = img.convert("RGB")
    src.thumbnail((stage_w, stage_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (stage_w, stage_h), (20, 20, 24))
    x = (stage_w - src.width) // 2
    y = (stage_h - src.height) // 2
    canvas.paste(src, (x, y))
    return canvas


if _HAS_DND and TkinterDnD is not None:

    class _CTkBase(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.TkdndVersion = TkinterDnD._require(self)

else:

    class _CTkBase(ctk.CTk):  # type: ignore[no-redef]
        pass


class ClipworkApp(_CTkBase):
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

        self._build()
        self._set_icons()
        self._bind_keys()
        self._restore_window_state()
        self.after(200, self._maybe_first_run)
        self._refresh_ffmpeg_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        try:
            self._save_window_state()
        except Exception:
            pass
        self._session.close()
        self.destroy()

    def _set_icons(self) -> None:
        try:
            ico = _resource_path("assets", "clipwork.ico")
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
        ctk.CTkButton(lb, text="Add…", width=70, command=self._add_files).pack(side="left", padx=2)
        ctk.CTkButton(lb, text="Clear", width=60, command=self._clear_files).pack(side="left", padx=2)

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
        self.btn_play = ctk.CTkButton(transport, text="Play", width=64, command=self._toggle_play)
        self.btn_play.pack(side="left", padx=2)
        ctk.CTkButton(transport, text="Stop", width=54, command=self._stop).pack(side="left", padx=2)
        ctk.CTkButton(transport, text="|◀", width=40, command=lambda: self._frame_step(-1)).pack(
            side="left", padx=1
        )
        ctk.CTkButton(transport, text="▶|", width=40, command=lambda: self._frame_step(1)).pack(
            side="left", padx=1
        )
        ctk.CTkButton(
            transport, text="Play sel.", width=72, command=self._play_selection
        ).pack(side="left", padx=2)
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
            text="Add a video, audio, or image file to preview\n"
            "Play includes sound once audio preview is ready\n\n"
            "Resize the window or drag the gray sashes between panes",
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
        if _HAS_DND:
            try:
                self.preview_frame.drop_target_register(DND_FILES)
                self.preview_frame.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

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

        self._tool_names = [
            "Convert",
            "Compress",
            "Trim",
            "Edit",
            "Audio",
            "Image",
            "More",
        ]
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
        ctk.CTkButton(row, text="Settings", width=80, command=self._settings).pack(side="right")
        self.log = ctk.CTkTextbox(bottom, height=max(60, log_h - 28))
        self.log.pack(fill="both", expand=True, padx=4, pady=(4, 4))
        self.log.insert(
            "1.0",
            "Open a file to preview. Scrub the timeline, set In/Out, then Export.\n"
            "Tip: drag the gray bars between panes · resize the window freely.\n",
        )
        self.log.configure(state="disabled")

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
        # Edit tab
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
        self.var_cut_quality = ctk.StringVar(value="high")  # high / balanced / fast
        self._srt_path: Path | None = None
        self._logo_path: Path | None = None

        for name in ("Convert", "Compress", "Trim", "Edit", "Audio", "Image", "More"):
            self._panels[name] = ctk.CTkFrame(self.tool_frame, fg_color="transparent")

        fr = self._panels["Convert"]
        ctk.CTkLabel(fr, text="Output format").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_fmt,
            values=["mp4", "webm", "mkv", "mov", "mp3", "wav", "png", "jpg", "webp"],
        ).pack(fill="x", pady=4)

        fr = self._panels["Compress"]
        ctk.CTkLabel(fr, text="Share / quality preset").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_preset, values=list(ops.COMPRESS_PRESETS)
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="chat/discord/whatsapp/email/720p/1080p\nbalanced/quality/fast_gpu",
            wraplength=250,
            justify="left",
            text_color=("gray40", "gray60"),
        ).pack(anchor="w")
        ctk.CTkLabel(fr, text="Image quality / max edge / audio bitrate").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_quality).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_bitrate).pack(fill="x", pady=2)

        fr = self._panels["Trim"]
        ctk.CTkLabel(
            fr,
            text="Uses timeline In/Out marks.\n"
            "Re-encode recommended for accuracy.\n"
            "Video/audio fades from Edit apply here too.",
            justify="left",
            wraplength=260,
        ).pack(anchor="w", pady=4)
        ctk.CTkCheckBox(fr, text="Re-encode (frame-accurate)", variable=self.var_reencode).pack(
            anchor="w", pady=4
        )
        ctk.CTkButton(fr, text="Go to In", command=lambda: self._goto_mark("in")).pack(
            fill="x", pady=2
        )
        ctk.CTkButton(fr, text="Go to Out", command=lambda: self._goto_mark("out")).pack(
            fill="x", pady=2
        )

        fr = self._panels["Edit"]
        ctk.CTkLabel(
            fr,
            text="Preview updates live — what you see is what you export.\n"
            "Timeline In/Out set the cut range.",
            wraplength=260,
            justify="left",
            text_color=("gray30", "gray70"),
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
            variable=self.var_edit_action,
            values=[
                "render_cut",
                "fade",
                "crop",
                "volume",
                "speed",
                "gif",
                "flip",
                "target_size",
                "burn_subs",
                "logo",
            ],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Cut quality").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_cut_quality, values=["high", "balanced", "fast"]
        ).pack(fill="x", pady=2)

        ctk.CTkLabel(fr, text="— Fades (seconds) —").pack(anchor="w", pady=(6, 2))
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
        self.volume_slider = ctk.CTkSlider(
            fr, from_=0, to=2.0, number_of_steps=40, command=self._on_volume_slider
        )
        self.volume_slider.set(1.0)
        self.volume_slider.pack(fill="x", pady=2)
        self.volume_label = ctk.CTkLabel(fr, text="100%")
        self.volume_label.pack(anchor="w")
        ctk.CTkCheckBox(fr, text="Mute", variable=self.var_mute).pack(anchor="w", pady=2)
        ctk.CTkOptionMenu(
            fr, variable=self.var_speed, values=list(ops.SPEED_PRESETS)
        ).pack(fill="x", pady=2)

        ctk.CTkLabel(fr, text="— Optional on render cut —").pack(anchor="w", pady=(6, 2))
        ctk.CTkCheckBox(fr, text="Apply crop overlay", variable=self.var_use_crop).pack(anchor="w")
        ctk.CTkButton(fr, text="Toggle crop overlay", command=self._toggle_crop_mode).pack(
            fill="x", pady=2
        )
        ctk.CTkEntry(fr, textvariable=self.var_crop_margin).pack(fill="x", pady=2)
        ctk.CTkCheckBox(fr, text="Burn subtitles", variable=self.var_use_subs).pack(anchor="w")
        ctk.CTkButton(fr, text="Choose subtitle .srt…", command=self._pick_srt).pack(
            fill="x", pady=2
        )
        ctk.CTkCheckBox(fr, text="Logo overlay", variable=self.var_use_logo).pack(anchor="w")
        ctk.CTkButton(fr, text="Choose logo image…", command=self._pick_logo).pack(
            fill="x", pady=2
        )
        ctk.CTkButton(fr, text="Toggle logo ghost", command=self._toggle_logo_ghost).pack(
            fill="x", pady=2
        )
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_logo_pos,
            values=["top-right", "top-left", "bottom-right", "bottom-left", "center"],
        ).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_logo_scale).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="GIF format / max MB (other actions)").pack(anchor="w")
        ctk.CTkOptionMenu(fr, variable=self.var_gif_fmt, values=["gif", "webp"]).pack(
            fill="x", pady=2
        )
        ctk.CTkEntry(fr, textvariable=self.var_max_mb).pack(fill="x", pady=2)
        # keep shared fade fields in sync helpers
        self.var_fade_in = self.var_v_fade_in
        self.var_fade_out = self.var_v_fade_out
        self.edit_files_label = ctk.CTkLabel(
            fr, text="No .srt / logo chosen", text_color=("gray40", "gray60"), wraplength=250
        )
        self.edit_files_label.pack(anchor="w", pady=4)

        fr = self._panels["Audio"]
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_audio_action,
            values=["extract", "convert", "normalize", "mono", "compress", "volume"],
        ).pack(fill="x", pady=4)
        ctk.CTkOptionMenu(fr, variable=self.var_audio_fmt, values=list(ops.AUDIO_FORMATS)).pack(
            fill="x", pady=4
        )
        ctk.CTkEntry(fr, textvariable=self.var_volume).pack(fill="x", pady=2)

        fr = self._panels["Image"]
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_image_action,
            values=["compress", "resize", "convert", "rotate", "flip", "strip_exif", "to_pdf"],
        ).pack(fill="x", pady=4)
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_degrees).pack(fill="x", pady=2)

        fr = self._panels["More"]
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_more,
            values=["remux", "strip_audio", "frame", "rotate_video", "flip_video", "concat"],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="frame uses playhead.\nconcat uses all videos in the list.",
            wraplength=260,
            justify="left",
        ).pack(anchor="w")

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
        """Mouse wheel / trackpad scrolls the tool strip horizontally."""

        def _wheel(event: Any) -> str | None:
            canvas = getattr(self._tool_tabs, "_parent_canvas", None)
            if canvas is None:
                return None
            delta = 0
            if getattr(event, "delta", 0):
                # Windows / macOS: positive = up/away; map to left/right
                delta = -1 if event.delta > 0 else 1
            elif getattr(event, "num", None) == 4:
                delta = -1
            elif getattr(event, "num", None) == 5:
                delta = 1
            if delta:
                canvas.xview_scroll(delta * 2, "units")
            return "break"

        # Bind on strip and children so wheel works when hovering pills
        widgets = [self._tool_tabs]
        try:
            widgets.append(self._tool_tabs._parent_canvas)  # type: ignore[attr-defined]
        except Exception:
            pass
        for w in widgets:
            w.bind("<MouseWheel>", _wheel)
            w.bind("<Shift-MouseWheel>", _wheel)
            w.bind("<Button-4>", _wheel)
            w.bind("<Button-5>", _wheel)
        # Re-bind when buttons are added (already exist); also bind each pill
        for btn in self._tool_buttons.values():
            btn.bind("<MouseWheel>", _wheel)
            btn.bind("<Shift-MouseWheel>", _wheel)
            btn.bind("<Button-4>", _wheel)
            btn.bind("<Button-5>", _wheel)

    def _on_tool_change(self, name: str) -> None:
        for n, fr in self._panels.items():
            if n == name:
                fr.pack(fill="both", expand=True)
            else:
                fr.pack_forget()

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

    def _export_log(self, msg: str) -> None:
        """Thread-safe log line for export workers."""
        self.after(0, lambda m=msg: self._log(m))

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

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add media",
            filetypes=[
                (
                    "Media",
                    "*.mp4;*.mkv;*.webm;*.mov;*.avi;*.mp3;*.wav;*.flac;*.m4a;*.png;*.jpg;*.jpeg;*.webp",
                ),
                ("All", "*.*"),
            ],
        )
        for p in paths:
            path = Path(p)
            if path.is_file() and path not in self._files:
                self._files.append(path)
        self._rebuild_file_list()
        if self._files and self._selected_idx < 0:
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
            image=None, text="Add a video, audio, or image file to preview"
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        self.info_line.configure(text="")
        self._update_time_labels(0, 0)

    def _on_drop(self, event) -> None:  # type: ignore[no-untyped-def]
        for p in _parse_drop(event.data):
            if p not in self._files:
                self._files.append(p)
        self._rebuild_file_list()
        if self._files:
            self._select_file(len(self._files) - 1)

    def _select_file(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._files):
            return
        self._selected_idx = idx
        self._rebuild_file_list()
        path = self._files[idx]
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
        self._session.in_point = 0.0
        self._session.out_point = duration if duration > 0 else None
        self._update_time_labels(0, duration)
        self._update_io_label()
        self._sync_time_fields()
        self.btn_play.configure(text="Play")
        try:
            self._session.seek(0.0)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Preview seek: {exc}")

    def _load_failed(self, err: str) -> None:
        self._set_status("Load failed")
        self._log(f"Preview error: {err}")
        messagebox.showerror(__app_name__, f"Could not open for preview:\n{err}")

    # ── preview / timeline ──────────────────────────────────
    def _on_session_frame(self, img: Image.Image | None, t: float) -> None:
        if img is None:
            return
        try:
            frame = img.copy()
        except Exception:
            return
        self.after(0, lambda i=frame, tt=t: self._show_frame(i, tt))

    def _on_session_position(self, t: float) -> None:
        self.after(0, lambda: self._sync_scrub(t))

    def _on_session_status(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._set_status(m))

    def _live_settings(self) -> dict[str, Any]:
        """Current UI edit settings — single source for preview and export."""
        return {
            "edit_action": self.var_edit_action.get(),
            "cut_quality": self.var_cut_quality.get(),
            "fade_video": bool(self.var_fade_video.get()),
            "fade_audio": bool(self.var_fade_audio.get()),
            "v_fade_in": self.var_v_fade_in.get(),
            "v_fade_out": self.var_v_fade_out.get(),
            "a_fade_in": self.var_a_fade_in.get(),
            "a_fade_out": self.var_a_fade_out.get(),
            "mute": bool(self.var_mute.get()),
            "volume": self.var_volume.get(),
            "speed": self.var_speed.get(),
            "use_crop": bool(self.var_use_crop.get()),
            "use_logo": bool(self.var_use_logo.get()),
            "use_subs": bool(self.var_use_subs.get()),
            "logo_pos": self.var_logo_pos.get(),
            "logo_scale": self.var_logo_scale.get(),
            "crop_margin": self.var_crop_margin.get(),
            "crop_rect": tuple(self._crop_rect),
            "srt_path": self._srt_path,
            "logo_path": self._logo_path,
            "logo_ghost": bool(self._logo_ghost),
            "gif_fmt": self.var_gif_fmt.get(),
            "max_mb": self.var_max_mb.get(),
        }

    def _reset_edit_looks(self) -> None:
        """Reset fades/volume/crop/logo toggles to defaults (keeps timeline In/Out)."""
        self._suppress_preview_trace = True
        try:
            self.var_edit_action.set("render_cut")
            self.var_cut_quality.set("high")
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

    def _sync_preview_audio(self) -> None:
        """Preview play uses the same mute/volume/audio fades as export."""
        try:
            mute = bool(self.var_mute.get())
            vol = float(self.var_volume.get() or 1.0)
        except Exception:
            mute, vol = False, 1.0
        self._session.preview_mute = mute
        self._session.preview_volume = max(0.0, min(4.0, vol))
        # Audio fades (seconds) — applied by ffplay during Play / Play sel.
        _vfi, _vfo, afi, afo = self._fade_seconds()
        self._session.preview_audio_fade_in = afi
        self._session.preview_audio_fade_out = afo

    def _bind_preview_traces(self) -> None:
        """Any look change updates the preview immediately (WYSIWYG)."""
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
            "var_cut_quality",
            "var_crop_margin",
        ):
            v = getattr(self, name, None)
            if v is not None:
                try:
                    v.trace_add("write", lambda *_a: self._on_edit_setting_changed())
                except Exception:
                    pass

    def _on_edit_setting_changed(self) -> None:
        """Live: sync audio + refresh preview (debounced)."""
        if self._suppress_preview_trace:
            return
        self._sync_preview_audio()
        if self._preview_resize_job is not None:
            try:
                self.after_cancel(self._preview_resize_job)
            except Exception:
                pass
        # Short debounce so typing fade seconds doesn't thrash paint
        self._preview_resize_job = self.after(40, self._repaint_preview_from_cache)

    def _draw_overlays(self, fitted: Image.Image, t: float | None = None) -> Image.Image:
        """Live export-faithful preview from current UI settings."""
        from PIL import ImageDraw

        a = self._live_settings()
        img = fitted.convert("RGBA")
        w, h = img.size
        if t is None:
            t = float(self._session.position)

        act = str(a.get("edit_action") or "")
        if act == "flip":
            try:
                img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            except Exception:
                pass

        d = ImageDraw.Draw(img, "RGBA")
        inn = float(self._session.in_point)
        outp = float(self._session.out_or_end or self._session.duration or 0)
        sel_dur = max(0.05, outp - inn)
        t_rel = t - inn
        outside = t < inn - 1e-3 or t > outp + 1e-3

        use_crop = bool(a.get("use_crop")) or self._crop_mode
        if use_crop:
            l, top, r, b = self._crop_rect
            x0, y0, x1, y1 = int(l * w), int(top * h), int(r * w), int(b * h)
            d.rectangle([0, 0, w, y0], fill=(0, 0, 0, 140))
            d.rectangle([0, y1, w, h], fill=(0, 0, 0, 140))
            d.rectangle([0, y0, x0, y1], fill=(0, 0, 0, 140))
            d.rectangle([x1, y0, w, y1], fill=(0, 0, 0, 140))
            d.rectangle([x0, y0, x1, y1], outline=(34, 197, 94, 255), width=2)
            if self._crop_mode:
                for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                    d.rectangle([cx - 4, cy - 4, cx + 4, cy + 4], fill=(34, 197, 94, 255))

        use_logo = bool(a.get("use_logo")) or bool(a.get("logo_ghost"))
        logo_path = a.get("logo_path")
        if use_logo and logo_path and Path(str(logo_path)).is_file():
            try:
                logo = Image.open(str(logo_path)).convert("RGBA")
                sc = float(a.get("logo_scale") or 0.15)
                lw = max(8, int(w * sc))
                lh = max(8, int(logo.height * (lw / max(1, logo.width))))
                logo = logo.resize((lw, lh), Image.Resampling.LANCZOS)
                op = 0.7 if a.get("logo_ghost") and not a.get("use_logo") else 0.9
                alpha = logo.split()[-1].point(lambda p: int(p * op))
                logo.putalpha(alpha)
                pos = str(a.get("logo_pos") or "top-right").lower()
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

        # Video fade relative to In/Out (matches export). Use RGB blend — reliable on all Pillow builds.
        vfi = vfo = 0.0
        try:
            if a.get("fade_video"):
                vfi = max(0.0, float(a.get("v_fade_in") or 0))
                vfo = max(0.0, float(a.get("v_fade_out") or 0))
        except Exception:
            pass
        # Clamp fade lengths so short selections still show a ramp
        vfi_c = min(vfi, sel_dur * 0.49) if vfi > 0 else 0.0
        vfo_c = min(vfo, sel_dur * 0.49) if vfo > 0 else 0.0
        fade_strength = 0.0  # 0 = full picture, 1 = full black
        if not outside and (vfi_c > 0 or vfo_c > 0):
            if vfi_c > 0 and t_rel < vfi_c:
                fade_strength = max(
                    fade_strength, 1.0 - max(0.0, min(1.0, t_rel / max(vfi_c, 1e-6)))
                )
            if vfo_c > 0 and t_rel > sel_dur - vfo_c:
                into = t_rel - (sel_dur - vfo_c)
                fade_strength = max(
                    fade_strength, max(0.0, min(1.0, into / max(vfo_c, 1e-6)))
                )
        if fade_strength > 0.001:
            rgb = img.convert("RGB")
            black = Image.new("RGB", (w, h), (0, 0, 0))
            rgb = Image.blend(rgb, black, min(1.0, fade_strength))
            img = rgb.convert("RGBA")
            d = ImageDraw.Draw(img, "RGBA")

        if outside and self._session.info and self._session.duration > 0:
            rgb = img.convert("RGB")
            black = Image.new("RGB", (w, h), (0, 0, 0))
            rgb = Image.blend(rgb, black, 0.55)
            img = rgb.convert("RGBA")
            d = ImageDraw.Draw(img, "RGBA")

        badges: list[str] = []
        try:
            if outside and self._session.info:
                badges.append("OUTSIDE SEL")
            if a.get("mute"):
                badges.append("MUTE")
            else:
                vol = float(a.get("volume") or 1.0)
                if abs(vol - 1.0) > 0.02:
                    badges.append(f"VOL {int(vol * 100)}%")
            sp = float(a.get("speed") or 1.0)
            if abs(sp - 1.0) > 0.02:
                badges.append(f"{sp:g}×")
            if a.get("fade_audio"):
                afi = float(a.get("a_fade_in") or 0)
                afo = float(a.get("a_fade_out") or 0)
                if afi > 0 or afo > 0:
                    badges.append(f"A-fade {afi:g}/{afo:g}s")
            if vfi > 0 or vfo > 0:
                badges.append(f"V-fade {vfi:g}/{vfo:g}s")
            if fade_strength > 0.05 and not outside:
                badges.append("FADING")
            if a.get("use_subs") and a.get("srt_path"):
                badges.append("SUBS")
            if a.get("use_crop") or self._crop_mode:
                badges.append("CROP")
            if a.get("use_logo") and a.get("logo_path"):
                badges.append("LOGO")
            if act == "flip":
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
        self._preview_resize_job = None
        img = self._last_frame_img
        if img is None:
            return
        try:
            self._show_frame(img, self._session.position)
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

    def _mark_layout_ready(self) -> None:
        self._layout_ready = True

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
            self.btn_play.configure(text="Pause")

    def _on_timeline_change(self, in_t: float, out_t: float, pos: float) -> None:
        self._session.in_point = in_t
        self._session.out_point = out_t
        self._session.position = pos
        self._update_io_label()
        self._sync_time_fields()
        # Fades are relative to In/Out — refresh so edges stay correct
        self._repaint_preview_from_cache()

    def _on_timeline_seek(self, t: float) -> None:
        self._scrub_dragging = True
        self._session.stop()
        self.btn_play.configure(text="Play")
        self._session.seek(t)
        self._update_time_labels(t, self._session.duration)
        self._scrub_dragging = False
        # Seek already emits a frame; ensure fade overlay is current
        self._repaint_preview_from_cache()

    def _update_time_labels(self, pos: float, dur: float) -> None:
        self.time_label.configure(text=f"{format_time(pos)} / {format_time(dur)}")

    def _update_io_label(self) -> None:
        inn = format_time(self._session.in_point)
        out = format_time(self._session.out_or_end)
        dur = max(0.0, (self._session.out_or_end or 0) - self._session.in_point)
        self.io_label.configure(text=f"In {inn} → Out {out}  ({format_time(dur)})")
        self.range_label.configure(
            text=f"Selection {inn} → {out}  ·  Space play · I/O marks · ←/→ frame · drag handles"
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
        dur = self._session.duration or out
        self._session.in_point = min(inn, dur)
        self._session.out_point = min(out, dur)
        self.timeline.set_range(self._session.in_point, self._session.out_or_end, self._session.position)
        self._update_io_label()
        self._sync_time_fields()
        self._session.seek(self._session.in_point)

    def _apply_duration_field(self) -> None:
        inn = self._parse_timecode(self.entry_in.get())
        dur = self._parse_timecode(self.entry_dur.get())
        if inn is None or dur is None or dur <= 0:
            messagebox.showwarning(__app_name__, "Enter valid In and duration.")
            return
        self._session.in_point = inn
        self._session.out_point = min(self._session.duration or (inn + dur), inn + dur)
        self.timeline.set_range(self._session.in_point, self._session.out_or_end, self._session.position)
        self._update_io_label()
        self._sync_time_fields()

    def _toggle_play(self) -> None:
        if not self._session.info:
            return
        if self._session.playing:
            self._session.stop()
            self.btn_play.configure(text="Play")
        else:
            self._sync_preview_audio()
            self._session.play()
            self.btn_play.configure(text="Pause")
            afi = self._session.preview_audio_fade_in
            afo = self._session.preview_audio_fade_out
            if afi > 0 or afo > 0:
                self._set_status(
                    f"Playing with audio fade in {afi:g}s / out {afo:g}s"
                )

    def _play_selection(self) -> None:
        if not self._session.info:
            return
        self._sync_preview_audio()
        self._session.play_selection(loop=True)
        self.btn_play.configure(text="Pause")
        afi = self._session.preview_audio_fade_in
        afo = self._session.preview_audio_fade_out
        extra = ""
        if afi > 0 or afo > 0:
            extra = f" · audio fade in {afi:g}s / out {afo:g}s"
        self._set_status("Playing selection (loop) — Stop to end" + extra)

    def _frame_step(self, delta: int) -> None:
        if not self._session.info:
            return
        t = self._session.frame_step(delta)
        self.timeline.set_position(t)
        self._update_time_labels(t, self._session.duration)
        self.btn_play.configure(text="Play")

    def _stop(self) -> None:
        self._session.stop()
        self.btn_play.configure(text="Play")
        self._session.seek(self._session.in_point)
        self.timeline.set_position(self._session.position)

    def _mark_in(self) -> None:
        self._session.set_in()
        self.timeline.set_range(
            self._session.in_point, self._session.out_or_end, self._session.position
        )
        self._update_io_label()
        self._sync_time_fields()
        self._log(f"In → {format_time(self._session.in_point)}")

    def _mark_out(self) -> None:
        self._session.set_out()
        self.timeline.set_range(
            self._session.in_point, self._session.out_or_end, self._session.position
        )
        self._update_io_label()
        self._sync_time_fields()
        self._log(f"Out → {format_time(self._session.out_or_end)}")

    def _clear_io(self) -> None:
        self._session.clear_in_out()
        self.timeline.set_range(0.0, self._session.duration or 1.0, self._session.position)
        self._update_io_label()
        self._sync_time_fields()
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
        self.focus_set()

    def _on_volume_slider(self, value: float) -> None:
        self.var_volume.set(f"{float(value):.2f}")
        if hasattr(self, "volume_label"):
            self.volume_label.configure(text=f"{int(float(value) * 100)}%")
        self._on_edit_setting_changed()

    def _cut_quality_params(self) -> tuple[int, str]:
        """Map UI quality label → (crf, x264 preset)."""
        q = (self.var_cut_quality.get() or "high").lower()
        if q == "fast":
            return 23, "veryfast"
        if q == "balanced":
            return 20, "medium"
        return 18, "medium"  # high

    def _fade_seconds(self) -> tuple[float, float, float, float]:
        """Live video/audio fades (0 when checkbox off)."""
        try:
            vfi = float(self.var_v_fade_in.get() or 0) if self.var_fade_video.get() else 0.0
            vfo = float(self.var_v_fade_out.get() or 0) if self.var_fade_video.get() else 0.0
            afi = float(self.var_a_fade_in.get() or 0) if self.var_fade_audio.get() else 0.0
            afo = float(self.var_a_fade_out.get() or 0) if self.var_fade_audio.get() else 0.0
        except (TypeError, ValueError):
            vfi = vfo = afi = afo = 0.0
        return max(0.0, vfi), max(0.0, vfo), max(0.0, afi), max(0.0, afo)

    def _crop_pixels(self, src: Path) -> tuple[int, int, int | None, int | None]:
        """Crop x,y,w,h from current overlay (or margin)."""
        if not self.var_use_crop.get() and not self._crop_mode:
            return 0, 0, None, None
        info = ops.probe(src)
        vw = vh = 0
        for s in info.get("streams") or []:
            if s.get("codec_type") == "video":
                vw = int(s.get("width") or 0)
                vh = int(s.get("height") or 0)
                break
        if vw < 2 or vh < 2:
            return 0, 0, None, None
        l, t, r, b = self._crop_rect
        x = max(0, int(l * vw))
        y = max(0, int(t * vh))
        w = max(2, int((r - l) * vw))
        h = max(2, int((b - t) * vh))
        w -= w % 2
        h -= h % 2
        if w >= 2 and h >= 2:
            return x, y, w, h
        try:
            margin = int(self.var_crop_margin.get() or 0)
        except (TypeError, ValueError):
            margin = 0
        if margin > 0:
            m = margin
            w = max(2, vw - 2 * m)
            h = max(2, vh - 2 * m)
            w -= w % 2
            h -= h % 2
            return m, m, w, h
        return 0, 0, None, None

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

    def _cancel_export(self) -> None:
        request_cancel()
        self._set_status("Cancelling export…")
        self._log("Cancel requested")

    def _open_last_folder(self) -> None:
        path = self._last_export_path
        if not path:
            return
        folder = path if path.is_dir() else path.parent
        try:
            if sys.platform == "win32":
                import os

                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                import subprocess as sp

                sp.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(__app_name__, f"Could not open folder:\n{exc}")

    def _set_queue_text(self, text: str) -> None:
        self.queue_box.configure(state="normal")
        self.queue_box.delete("1.0", "end")
        self.queue_box.insert("1.0", text)
        self.queue_box.configure(state="disabled")

    _QUEUE_IDLE = "Batch queue appears here when exporting multiple files.\n"

    def _reset_export_chrome(
        self,
        *,
        status: str = "Ready",
        keep_open_folder: bool = False,
    ) -> None:
        """Clear export-only UI so the app feels ready for the next job.

        Keeps project state: media list, preview, In/Out, tool settings.
        """
        self._busy = False
        self._export_proc_active = False
        self._last_prog_log_frac = -1.0
        self._batch_queue_lines = []
        try:
            self.btn_cancel.configure(state="disabled")
        except Exception:
            pass
        if keep_open_folder and self._last_export_path:
            try:
                self.btn_open_folder.configure(state="normal")
            except Exception:
                pass
        else:
            try:
                self.btn_open_folder.configure(state="disabled")
            except Exception:
                pass
            # Drop path only when we are fully clearing export residue
            if not keep_open_folder:
                self._last_export_path = None
        self._set_progress(0.0, "Idle", to_log=False)
        self._set_status(status)
        self._set_queue_text(self._QUEUE_IDLE)

    # ── export ──────────────────────────────────────────────
    def _current_path(self) -> Path | None:
        if 0 <= self._selected_idx < len(self._files):
            return self._files[self._selected_idx]
        return None

    def _set_progress(self, frac: float, label: str, *, to_log: bool = False) -> None:
        self.progress.set(max(0.0, min(1.0, frac)))
        self.progress_label.configure(text=label)
        if to_log:
            self._log(f"  … {label}")

    def _remember_output_dir(self, path: Path) -> None:
        try:
            data = app_prefs.load_prefs()
            data["last_output_dir"] = str(path.parent)
            app_prefs.save_prefs(data)
        except Exception:
            pass

    def _export_defaults(self, tool: str, src: Path | None) -> tuple[str, str, list[tuple[str, str]]]:
        """Return (initial_dir, suggested_name, filetypes) for the Save dialog."""
        prefs = app_prefs.load_prefs()
        last = str(prefs.get("last_output_dir") or "")
        if src:
            initial_dir = last if last and Path(last).is_dir() else str(src.parent)
            stem = src.stem
            ext = src.suffix.lower() or ".mp4"
        else:
            initial_dir = last if last and Path(last).is_dir() else str(Path.home())
            stem = "export"
            ext = ".mp4"

        video_ft = [
            ("MP4 video", "*.mp4"),
            ("MKV video", "*.mkv"),
            ("WebM video", "*.webm"),
            ("MOV video", "*.mov"),
            ("All files", "*.*"),
        ]
        audio_ft = [
            ("MP3 audio", "*.mp3"),
            ("WAV audio", "*.wav"),
            ("M4A audio", "*.m4a"),
            ("FLAC audio", "*.flac"),
            ("OGG audio", "*.ogg"),
            ("All files", "*.*"),
        ]
        image_ft = [
            ("JPEG image", "*.jpg"),
            ("PNG image", "*.png"),
            ("WebP image", "*.webp"),
            ("All files", "*.*"),
        ]

        if tool == "More" and self.var_more.get() == "concat":
            return initial_dir, f"{stem}_concat.mp4", video_ft

        assert src is not None
        ext = src.suffix.lower()

        if tool == "Trim":
            # Default to original name — user can replace the open file in one Save
            return initial_dir, f"{stem}{src.suffix or '.mp4'}", video_ft + audio_ft

        if tool == "Convert":
            fmt = (self.var_fmt.get() or "mp4").lower()
            if fmt == "jpg":
                fmt = "jpeg"
            if fmt in ("png", "jpeg", "webp") or ext in IMAGE_EXTS:
                f = fmt if fmt in ("png", "jpeg", "webp") else "png"
                suf = ".jpg" if f == "jpeg" else f".{f}"
                return initial_dir, f"{stem}_convert{suf}", image_ft
            if fmt in ops.AUDIO_FORMATS or ext in AUDIO_EXTS:
                f = fmt if fmt in ops.AUDIO_FORMATS else "mp3"
                return initial_dir, f"{stem}_convert.{f}", audio_ft
            f = fmt if fmt in ops.VIDEO_FORMATS else "mp4"
            return initial_dir, f"{stem}_convert.{f}", video_ft

        if tool == "Compress":
            if ext in IMAGE_EXTS:
                return initial_dir, f"{stem}_compress.jpg", image_ft
            if ext in AUDIO_EXTS:
                return initial_dir, f"{stem}_compress.mp3", audio_ft
            return initial_dir, f"{stem}_compress.mp4", video_ft

        if tool == "Audio":
            act = self.var_audio_action.get()
            fmt = (self.var_audio_fmt.get() or "mp3").lower()
            if act == "extract":
                return initial_dir, f"{stem}_audio.{fmt}", audio_ft
            if act == "normalize":
                return initial_dir, f"{stem}_norm.mp3", audio_ft
            if act == "mono":
                return initial_dir, f"{stem}_mono.mp3", audio_ft
            if act == "compress":
                return initial_dir, f"{stem}_compress.mp3", audio_ft
            return initial_dir, f"{stem}_convert.{fmt}", audio_ft

        if tool == "Image":
            act = self.var_image_action.get()
            if act == "to_pdf":
                return initial_dir, f"{stem}.pdf", [("PDF", "*.pdf"), ("All files", "*.*")]
            if act == "compress":
                return initial_dir, f"{stem}_compress.jpg", image_ft
            if act == "convert":
                return initial_dir, f"{stem}_convert.png", image_ft
            if act == "resize":
                return initial_dir, f"{stem}_resize{src.suffix or '.png'}", image_ft
            if act == "rotate":
                return initial_dir, f"{stem}_rot{src.suffix or '.png'}", image_ft
            if act == "flip":
                return initial_dir, f"{stem}_flip{src.suffix or '.png'}", image_ft
            if act == "strip_exif":
                return initial_dir, f"{stem}_noexif{src.suffix or '.jpg'}", image_ft
            return initial_dir, f"{stem}_edit{src.suffix or '.png'}", image_ft

        if tool == "Edit":
            act = self.var_edit_action.get()
            if act == "gif":
                fmt = self.var_gif_fmt.get() or "gif"
                return initial_dir, f"{stem}_clip.{fmt}", [
                    ("GIF", "*.gif"),
                    ("WebP", "*.webp"),
                    ("All files", "*.*"),
                ]
            if act == "target_size":
                return initial_dir, f"{stem}_sized.mp4", video_ft
            if act == "render_cut":
                # Default to original name so Save can replace the open project file
                return initial_dir, f"{stem}{src.suffix or '.mp4'}", video_ft
            if act == "fade":
                return initial_dir, f"{stem}{src.suffix or '.mp4'}", video_ft
            if act in ("crop", "speed", "flip", "volume", "burn_subs", "logo"):
                tag = act
                return initial_dir, f"{stem}_{tag}{src.suffix or '.mp4'}", video_ft + audio_ft
            return initial_dir, f"{stem}_edit{src.suffix or '.mp4'}", video_ft

        more = self.var_more.get()
        if more == "remux":
            return initial_dir, f"{stem}_remux.mp4", video_ft
        if more == "strip_audio":
            return initial_dir, f"{stem}_silent.mp4", video_ft
        if more == "frame":
            return initial_dir, f"{stem}_frame.jpg", image_ft
        if more == "rotate_video":
            return initial_dir, f"{stem}_rot.mp4", video_ft
        if more == "flip_video":
            return initial_dir, f"{stem}_flip.mp4", video_ft
        return initial_dir, f"{stem}_export{src.suffix or '.mp4'}", video_ft + audio_ft + image_ft

    def _ask_save_path(self, tool: str, src: Path | None) -> Path | None:
        """Show Save As dialog. Existing files (including the open one) may be replaced."""
        initial_dir, name, filetypes = self._export_defaults(tool, src)
        # Default extension from suggested name
        def_ext = Path(name).suffix or ".mp4"
        path_str = filedialog.asksaveasfilename(
            parent=self,
            title="Save exported file as… (can replace the open file)",
            initialdir=initial_dir,
            initialfile=name,
            defaultextension=def_ext,
            filetypes=filetypes,
        )
        if not path_str:
            return None
        dest = Path(path_str)
        # Ensure parent exists
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(__app_name__, f"Cannot create folder:\n{exc}")
            return None
        # Confirm replace — special copy when replacing the file you're editing
        if dest.is_file():
            inplace = paths_same(src, dest)
            if inplace:
                msg = (
                    f"Replace the file you're editing?\n\n{dest.name}\n\n"
                    "Clipwork will encode safely to a temp file, then swap it in.\n"
                    "This cannot be undone."
                )
            else:
                msg = (
                    f"Replace existing file?\n\n{dest.name}\n\n"
                    "This cannot be undone."
                )
            if not messagebox.askyesno(__app_name__, msg, icon="warning"):
                return None
        self._remember_output_dir(dest)
        return dest

    def _ask_output_folder(self) -> Path | None:
        prefs = app_prefs.load_prefs()
        last = str(prefs.get("last_output_dir") or "")
        initial = last if last and Path(last).is_dir() else str(Path.home())
        path_str = filedialog.askdirectory(
            parent=self, title="Choose folder for batch exports", initialdir=initial
        )
        if not path_str:
            return None
        folder = Path(path_str)
        try:
            data = app_prefs.load_prefs()
            data["last_output_dir"] = str(folder)
            app_prefs.save_prefs(data)
        except Exception:
            pass
        return folder

    def _run(self) -> None:
        if self._busy:
            return
        src = self._current_path()
        tool = self.tool.get()
        batch = bool(self.var_batch.get()) and tool != "More"

        if tool == "More" and self.var_more.get() == "concat":
            files = [p for p in self._files if p.suffix.lower() in VIDEO_EXTS]
            if len(files) < 2:
                messagebox.showwarning(__app_name__, "Add at least two videos for concat.")
                return
            batch = False
        elif batch:
            if not self._files:
                messagebox.showwarning(__app_name__, "Add files to the list for batch export.")
                return
        elif not src:
            messagebox.showwarning(__app_name__, "Select a media file first.")
            return

        if tool != "Image" and not ops.find_ffmpeg():
            if tool not in ("Image",) and not (
                src and src.suffix.lower() in IMAGE_EXTS and tool in ("Convert", "Compress")
            ):
                messagebox.showerror(__app_name__, "ffmpeg not found (required for video/audio).")
                return

        if batch:
            dest_folder = self._ask_output_folder()
            if dest_folder is None:
                self._reset_export_chrome(status="Ready")
                return
            dest: Path | None = dest_folder
        else:
            dest = self._ask_save_path(tool, src)
            if dest is None:
                self._reset_export_chrome(status="Ready")
                return

        self._busy = True
        self._export_proc_active = True
        self._last_prog_log_frac = -1.0
        self._batch_queue_lines = []
        self.btn_cancel.configure(state="normal")
        self.btn_open_folder.configure(state="disabled")
        self._last_export_path = None
        self._session.stop()
        self.btn_play.configure(text="Play")
        label = dest.name if dest else "…"
        self._set_status(f"Exporting to {label}…")
        self._set_progress(0.02, "Preparing export…")
        self._log("—")
        self._log(f"Export started · tool={tool}" + (" · batch" if batch else ""))
        if src:
            self._log(f"  Source: {src.name}")
        self._log(f"  Destination: {dest}")
        if tool in ("Trim", "Edit") and self._session.info:
            self._log(
                f"  Selection: {format_time(self._session.in_point)} → "
                f"{format_time(self._session.out_or_end)} "
                f"({format_time(max(0, self._session.out_or_end - self._session.in_point))})"
            )
        self._log("  Preparing… (starting encoder)")
        self._set_queue_text(
            "Export running…\n"
            + (f"Batch: {len(self._files)} files\n" if batch else f"{label}\n")
        )

        def work() -> None:
            try:
                if batch:
                    lines = self._export_batch(tool, list(self._files), dest)  # type: ignore[arg-type]
                else:
                    lines = self._export_worker(tool, src, dest)  # type: ignore[arg-type]
                # Prefer concrete path from worker (in-place replace returns final path)
                done_dest = Path(lines[0]) if lines else dest
                self.after(0, lambda: self._export_done(True, lines, done_dest if not batch else dest))
            except CancelledError:
                self._export_log("  Cancelled by user.")
                # Clean leftover staging file if any
                try:
                    if dest is not None and src is not None and paths_same(src, dest):
                        sp = staging_path(dest)
                        if sp.is_file():
                            sp.unlink()
                except Exception:
                    pass
                self.after(0, lambda: self._export_done(False, ["Cancelled by user"], None))
            except Exception as exc:  # noqa: BLE001
                self._export_log(f"  Error: {exc}")
                try:
                    if dest is not None and src is not None and paths_same(src, dest):
                        sp = staging_path(dest)
                        if sp.is_file():
                            sp.unlink()
                except Exception:
                    pass
                self.after(0, lambda: self._export_done(False, [str(exc)], None))

        threading.Thread(target=work, daemon=True).start()

    def _batch_runner_for_tool(self, tool: str) -> tuple[str, str, Any]:
        """Return (op_name, name_tag, callable(src, dest)->Path) for batch."""
        if tool == "Compress":
            preset = self.var_preset.get() or "balanced"

            def run(src: Path, dest: Path) -> Path:
                ext = src.suffix.lower()
                if ext in IMAGE_EXTS:
                    return ops.compress_image(
                        src,
                        dest,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    )
                if ext in AUDIO_EXTS:
                    return ops.compress_audio(
                        src, dest, bitrate=self.var_bitrate.get() or "128k"
                    )
                return ops.compress_video(src, dest, preset=preset)

            return "compress", f"compress_{preset}", run

        if tool == "Convert":
            fmt = self.var_fmt.get() or "mp4"

            def run(src: Path, dest: Path) -> Path:
                ext = src.suffix.lower()
                if ext in IMAGE_EXTS or fmt in ("png", "jpg", "webp"):
                    f = fmt if fmt in ("png", "jpg", "webp") else "png"
                    return ops.convert_image(src, dest, fmt=f)
                if ext in AUDIO_EXTS or fmt in ops.AUDIO_FORMATS:
                    f = fmt if fmt in ops.AUDIO_FORMATS else "mp3"
                    return ops.convert_audio(src, dest, fmt=f)
                f = fmt if fmt in ops.VIDEO_FORMATS else "mp4"
                return ops.convert_video(src, dest, fmt=f)

            return "convert", "convert", run

        if tool == "Trim":
            start = self._session.in_point
            end = self._session.out_or_end
            reenc = bool(self.var_reencode.get())

            def run(src: Path, dest: Path) -> Path:
                return ops.trim_media(
                    src, dest, start=start, end=end if end else None, reencode=reenc
                )

            return "trim", "trim", run

        if tool == "Edit":
            return self._batch_edit_runner()

        if tool == "Audio":
            act = self.var_audio_action.get()
            fmt = self.var_audio_fmt.get() or "mp3"
            vol = float(self.var_volume.get() or 1.0)

            def run(src: Path, dest: Path) -> Path:
                if act == "extract":
                    return ops.extract_audio(src, dest, fmt=fmt)
                if act == "normalize":
                    return ops.normalize_audio(src, dest)
                if act == "mono":
                    return ops.to_mono(src, dest)
                if act == "volume":
                    return ops.adjust_volume(src, dest, volume=vol)
                if act == "compress":
                    return ops.compress_audio(
                        src, dest, bitrate=self.var_bitrate.get() or "128k"
                    )
                return ops.convert_audio(src, dest, fmt=fmt)

            return f"audio_{act}", act, run

        if tool == "Image":
            act = self.var_image_action.get()

            def run(src: Path, dest: Path) -> Path:
                if act == "compress":
                    return ops.compress_image(
                        src,
                        dest,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    )
                if act == "resize":
                    return ops.resize_image(
                        src, dest, max_edge=int(self.var_max_edge.get() or 1920)
                    )
                if act == "rotate":
                    return ops.rotate_image(
                        src, dest, degrees=int(self.var_degrees.get() or 90)
                    )
                if act == "flip":
                    return ops.flip_image(src, dest)
                if act == "strip_exif":
                    return ops.strip_exif(src, dest)
                if act == "to_pdf":
                    return ops.images_to_pdf([src], dest)
                return ops.convert_image(src, dest, fmt="png")

            return f"image_{act}", act, run

        raise RuntimeError(f"Batch not supported for tool: {tool}")

    def _batch_edit_runner(self) -> tuple[str, str, Any]:
        a = self._live_settings()
        act = str(a.get("edit_action") or "render_cut")
        try:
            margin = int(a.get("crop_margin") or 0)
        except (TypeError, ValueError):
            margin = 0
        vol = float(a.get("volume") or 1.0)
        mute = bool(a.get("mute"))
        speed = float(a.get("speed") or 1.0)
        vfi, vfo, afi, afo = self._fade_seconds()
        max_mb = float(a.get("max_mb") or 25)
        start = self._session.in_point
        end = self._session.out_or_end
        gif_fmt = str(a.get("gif_fmt") or "gif")
        crf, preset = self._cut_quality_params()
        logo_path = a.get("logo_path")
        srt_path = a.get("srt_path")
        use_logo = bool(a.get("use_logo") and logo_path)
        use_subs = bool(a.get("use_subs") and srt_path)
        logo_pos = str(a.get("logo_pos") or "top-right")
        logo_scale = float(a.get("logo_scale") or 0.15)

        def run(src: Path, dest: Path) -> Path:
            if act == "render_cut":
                cx, cy, cw, ch = self._crop_pixels(src)
                return ops.render_cut(
                    src,
                    dest,
                    start=start,
                    end=end if end else None,
                    crop_x=cx,
                    crop_y=cy,
                    crop_w=cw,
                    crop_h=ch,
                    speed=speed,
                    volume=vol,
                    mute=mute,
                    video_fade_in=vfi,
                    video_fade_out=vfo,
                    audio_fade_in=afi,
                    audio_fade_out=afo,
                    logo=logo_path if use_logo else None,
                    logo_position=logo_pos,
                    logo_scale=logo_scale,
                    srt=srt_path if use_subs else None,
                    crf=crf,
                    preset=preset,
                )
            if act == "crop":
                return ops.crop_video(src, dest, margin=margin)
            if act == "volume":
                return ops.adjust_volume(src, dest, volume=vol, mute=mute)
            if act == "speed":
                return ops.change_speed(src, dest, speed=speed)
            if act == "gif":
                return ops.export_gif(
                    src, dest, start=start, end=end if end else None, fmt=gif_fmt
                )
            if act == "fade":
                return ops.fade_media(
                    src,
                    dest,
                    fade_in=0.0,
                    fade_out=0.0,
                    video_fade_in=vfi,
                    video_fade_out=vfo,
                    audio_fade_in=afi,
                    audio_fade_out=afo,
                    start=start,
                    end=end if end else None,
                    crf=crf,
                    preset=preset,
                )
            if act == "flip":
                return ops.flip_video(src, dest, horizontal=True)
            if act == "target_size":
                return ops.target_size_video(src, dest, max_mb=max_mb)
            if act == "burn_subs":
                if not srt_path:
                    raise RuntimeError("Choose a .srt subtitle file first")
                return ops.burn_subtitles(src, Path(str(srt_path)), dest)
            if act == "logo":
                if not logo_path:
                    raise RuntimeError("Choose a logo image first")
                return ops.logo_overlay(
                    src,
                    Path(str(logo_path)),
                    dest,
                    position=logo_pos,
                    scale=logo_scale,
                )
            raise RuntimeError(f"Unknown edit action: {act}")

        return f"edit_{act}", act, run

    def _export_batch(self, tool: str, files: list[Path], out_dir: Path) -> list[str]:
        op_name, tag, run_one = self._batch_runner_for_tool(tool)
        total = len(files)

        queue_lines: list[str] = [f"Batch → {out_dir}", f"Operation: {op_name}", ""]
        self._export_log(f"  Batch: {total} file(s) → {out_dir}")
        self._export_log(f"  Operation: {op_name}")

        def on_prog(i: int, n: int, name: str) -> None:
            self.after(
                0,
                lambda: self._set_progress(
                    i / max(n, 1), f"Batch {i}/{n}: {name}", to_log=False
                ),
            )
            self._export_log(f"  [{i}/{n}] Processing {name}…")
            self.after(
                0,
                lambda: self._set_queue_text(
                    "\n".join(queue_lines + [f"… {i}/{n} {name}"])
                ),
            )

        def run_one_logged(src: Path, dest: Path) -> Path:
            self._export_log(f"  → {src.name}")
            return run_one(src, dest)

        results = ops.batch_to_folder(
            files,
            out_dir,
            op_name=op_name,
            run_one=run_one_logged,
            name_tag=tag,
            on_progress=on_prog,
        )
        lines: list[str] = []
        ok_n = 0
        for r in results:
            if r["ok"]:
                ok_n += 1
                lines.append(str(r["dest"]))
                queue_lines.append(f"✓ {Path(r['src']).name} → {Path(str(r['dest'])).name}")
                self._export_log(f"  ✓ {Path(r['src']).name} → {Path(str(r['dest'])).name}")
            else:
                lines.append(f"FAIL {Path(r['src']).name}: {r['error']}")
                queue_lines.append(f"✗ {Path(r['src']).name}: {r['error']}")
                self._export_log(f"  ✗ {Path(r['src']).name}: {r['error']}")
        lines.insert(0, f"Batch done: {ok_n}/{total} ok → {out_dir}")
        self._export_log(f"  Batch complete: {ok_n}/{total} succeeded")
        self.after(0, lambda: self._set_queue_text("\n".join(queue_lines)))
        if ok_n == 0:
            raise RuntimeError("All batch jobs failed")
        return lines

    def _release_open_file_for_replace(self, timeout: float = 12.0) -> None:
        """Close preview handles so Windows will allow replacing the open path."""
        done = threading.Event()

        def _close() -> None:
            try:
                self._session.close()
                self.btn_play.configure(text="Play")
            except Exception:
                pass
            finally:
                done.set()

        self.after(0, _close)
        if not done.wait(timeout):
            raise RuntimeError(
                "Could not release the open file (preview still holding it). Try again."
            )

    def _reload_after_inplace_replace(self, path: Path) -> None:
        """Re-open the project file after a successful in-place replace."""
        path = Path(path)
        # Keep list entry pointing at the same path
        if 0 <= self._selected_idx < len(self._files):
            self._files[self._selected_idx] = path
        elif path not in self._files:
            self._files.append(path)
            self._selected_idx = len(self._files) - 1
        self._log(f"  Reloading preview: {path.name}")
        self._select_file(self._selected_idx if self._selected_idx >= 0 else 0)

    def _export_worker(self, tool: str, src: Path | None, dest: Path) -> list[str]:
        results: list[str] = []
        final_dest = Path(dest)
        # Encode to a sibling temp when replacing the open source (ffmpeg can't read+write same path)
        inplace = bool(src is not None and paths_same(src, final_dest))
        write_dest = staging_path(final_dest) if inplace else final_dest
        dest = write_dest  # all encode targets use write_dest

        def prog(frac: float, label: str) -> None:
            # Progress bar always; bottom log throttled (~every 5%)
            to_log = False
            try:
                prev = getattr(self, "_last_prog_log_frac", -1.0)
                if frac >= 0.999 or prev < 0 or (frac - prev) >= 0.05:
                    to_log = True
                    self._last_prog_log_frac = frac
            except Exception:
                to_log = True
            self.after(
                0,
                lambda f=frac, l=label, tl=to_log: self._set_progress(f, l, to_log=tl),
            )

        def note(msg: str) -> None:
            self._export_log(msg)
            self.after(0, lambda m=msg: self._set_status(m[:80]))

        def finish(paths: list[str]) -> list[str]:
            """If in-place, swap temp → original after releasing the open handle."""
            if not inplace:
                return paths
            note(f"  Swapping temp encode into {final_dest.name}…")
            # Ensure encode landed on the staging path
            staged = Path(dest)
            if paths:
                cand = Path(paths[0])
                if cand.is_file():
                    staged = cand
            try:
                self._release_open_file_for_replace()
                commit_staged(staged, final_dest)
            except Exception:
                # Leave staging file for diagnosis if swap failed
                raise
            self._pending_reload_path = final_dest
            note(f"  Replaced open file: {final_dest.name}")
            return [str(final_dest)]

        # Prepare target
        if inplace:
            note(f"  Safe replace of open file: {final_dest.name}")
            note("  Encoding to temp, then swapping in (you can edit the same file).")
            if write_dest.exists():
                try:
                    write_dest.unlink()
                except OSError as exc:
                    raise RuntimeError(f"Cannot clear temp file {write_dest.name}: {exc}") from exc
        elif final_dest.exists():
            note(f"  Replacing existing file: {final_dest.name}")
            try:
                final_dest.unlink()
            except OSError as exc:
                raise RuntimeError(f"Cannot overwrite {final_dest.name}: {exc}") from exc

        if tool == "More" and self.var_more.get() == "concat":
            vids = [p for p in self._files if p.suffix.lower() in VIDEO_EXTS]
            note(f"  Concatenating {len(vids)} videos (re-encode for compatibility)…")
            prog(0.05, "Concat: starting…")
            jr = jobs.run_job(
                "concat",
                lambda: ops.concat_videos(vids, dest, reencode=True),
                inputs=vids,
            )
            if not jr.ok:
                raise RuntimeError(jr.error)
            note("  Concat finished.")
            prog(1.0, "100% · done")
            out_list = [str(jr.paths[0] if jr.paths else dest)]
            return finish(out_list)

        assert src is not None
        ext = src.suffix.lower()

        if tool == "Trim":
            start = self._session.in_point
            end = self._session.out_or_end
            dur = None
            if end and end > start:
                dur = end - start
            reenc = bool(self.var_reencode.get())
            out_path = dest
            suffix = dest.suffix or src.suffix or ".mp4"
            vfi, vfo, afi, afo = self._fade_seconds()
            has_fades = (vfi > 0 or vfo > 0 or afi > 0 or afo > 0) and suffix.lower() not in (
                ".mp3",
                ".wav",
                ".flac",
                ".m4a",
                ".ogg",
                ".aac",
            )
            # Fades require a re-encode — use one-pass cut so Trim matches the preview
            if has_fades:
                note(
                    f"  Trim + fades (video {vfi:.2f}/{vfo:.2f}s · audio {afi:.2f}/{afo:.2f}s)"
                )
                note(
                    f"  Cutting {format_time(start)} → {format_time(end or 0)}"
                    + (f" ({format_time(dur)})" if dur else "")
                )
                prog(0.03, "Trim: encoding with fades…")
                try:
                    out = ops.render_cut(
                        src,
                        out_path,
                        start=start,
                        end=end if end else None,
                        video_fade_in=vfi,
                        video_fade_out=vfo,
                        audio_fade_in=afi,
                        audio_fade_out=afo,
                        crf=23,
                        preset="veryfast",
                        on_progress=prog,
                    )
                except CancelledError:
                    note("  Trim cancelled.")
                    raise
                note(f"  Writing finished: {Path(out).name}")
                prog(1.0, "100% · done")
                return finish([str(out)])

            mode = "re-encode (accurate)" if reenc else "stream copy (fast)"
            note(f"  Trim mode: {mode}")
            note(
                f"  Cutting {format_time(start)} → {format_time(end or 0)}"
                + (f" ({format_time(dur)})" if dur else "")
            )
            if reenc and ops.find_ffmpeg():
                note("  Starting encoder (libx264 veryfast)…")
                prog(0.03, "Trim: encoding…")
                # -ss after -i = accurate cut for re-encode; -t = selection length
                args = ["-i", str(src), "-ss", str(start)]
                if dur:
                    args += ["-t", str(dur)]
                elif end is not None:
                    args += ["-to", str(end)]
                if suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"):
                    args += ["-c:a", "libmp3lame", "-q:a", "2", str(out_path)]
                else:
                    # veryfast keeps quality fine for everyday trims and finishes much quicker
                    args += [
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-movflags",
                        "+faststart",
                        str(out_path),
                    ]
                hint = float(dur if dur and dur > 0 else (self._session.duration or 1))
                try:
                    run_ffmpeg_with_progress(args, duration_hint=hint, on_progress=prog)
                    jr = jobs.JobResult("trim", [out_path], True, 0.0)
                except CancelledError:
                    note("  Trim cancelled.")
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(str(exc)) from exc
            else:
                note("  Stream-copy trim (may snap to keyframes)…")
                prog(0.2, "Trim: copying…")
                jr = jobs.run_job(
                    "trim",
                    lambda: ops.trim_media(
                        src, out_path, start=start, end=end if end else None, reencode=reenc
                    ),
                    inputs=[src],
                )
            if not jr.ok and not out_path.is_file():
                raise RuntimeError(jr.error or "trim failed")
            note(f"  Writing finished: {out_path.name}")
            prog(1.0, "100% · done")
            results.append(str(out_path if out_path.is_file() else jr.paths[0]))
            return finish(results)

        def go(name: str, fn):  # type: ignore[no-untyped-def]
            note(f"  Running {name}…")
            prog(0.1, f"{name}: working…")
            jr = jobs.run_job(name, fn, inputs=[src])
            if not jr.ok:
                raise RuntimeError(jr.error or name)
            note(f"  {name} finished → {dest.name}")
            prog(1.0, "100% · done")
            paths = [str(p) for p in jr.paths] if jr.paths else [str(dest)]
            return finish(paths)

        if tool == "Convert":
            fmt = self.var_fmt.get()
            # Prefer extension from chosen save path when it matches a known format
            dest_fmt = dest.suffix.lstrip(".").lower()
            if dest_fmt == "jpg":
                dest_fmt = "jpeg"
            if ext in IMAGE_EXTS or dest_fmt in ("png", "jpeg", "webp") or fmt in ("png", "jpg", "webp"):
                f = dest_fmt if dest_fmt in ("png", "jpeg", "webp") else (
                    fmt if fmt in ("png", "jpg", "webp") else "png"
                )
                if f == "jpg":
                    f = "jpeg"
                return go("convert_image", lambda: ops.convert_image(src, dest, fmt=f))
            if ext in AUDIO_EXTS or dest_fmt in ops.AUDIO_FORMATS or fmt in ops.AUDIO_FORMATS:
                f = dest_fmt if dest_fmt in ops.AUDIO_FORMATS else (
                    fmt if fmt in ops.AUDIO_FORMATS else "mp3"
                )
                return go("convert_audio", lambda: ops.convert_audio(src, dest, fmt=f))
            f = dest_fmt if dest_fmt in ops.VIDEO_FORMATS else (
                fmt if fmt in ops.VIDEO_FORMATS else "mp4"
            )
            return go("convert_video", lambda: ops.convert_video(src, dest, fmt=f))

        if tool == "Compress":
            if ext in IMAGE_EXTS or dest.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return go(
                    "compress_image",
                    lambda: ops.compress_image(
                        src,
                        dest,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    ),
                )
            if ext in AUDIO_EXTS or dest.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}:
                return go(
                    "compress_audio",
                    lambda: ops.compress_audio(
                        src, dest, bitrate=self.var_bitrate.get() or "128k"
                    ),
                )
            return go(
                "compress_video",
                lambda: ops.compress_video(
                    src, dest, preset=self.var_preset.get() or "balanced"
                ),
            )

        if tool == "Edit":
            a = self._live_settings()
            act = str(a.get("edit_action") or "render_cut")
            if act in ("render_cut", "fade"):
                start = self._session.in_point
                end = self._session.out_or_end
                vfi, vfo, afi, afo = self._fade_seconds()
                crf, preset = self._cut_quality_params()
                vol = float(a.get("volume") or 1.0)
                mute = bool(a.get("mute"))
                speed = float(a.get("speed") or 1.0)
                cx, cy, cw, ch = (0, 0, None, None)
                logo = None
                srt = None
                if act == "render_cut":
                    if a.get("use_crop") or self._crop_mode:
                        cx, cy, cw, ch = self._crop_pixels(src)
                    if a.get("use_logo") and a.get("logo_path"):
                        logo = Path(str(a["logo_path"]))
                    if a.get("use_subs") and a.get("srt_path"):
                        srt = Path(str(a["srt_path"]))
                sel = f"{format_time(start)} → {format_time(end or 0)}"
                note(f"  One-pass {'render cut' if act == 'render_cut' else 'fade'}: {sel}")
                note(
                    f"  Fades video {vfi:.2f}/{vfo:.2f}s · audio {afi:.2f}/{afo:.2f}s"
                    f" · quality={a.get('cut_quality') or 'high'}"
                )
                if act == "render_cut":
                    bits = []
                    if cw and ch:
                        bits.append(f"crop {cw}x{ch}")
                    if abs(speed - 1.0) > 1e-3:
                        bits.append(f"speed {speed}x")
                    if mute:
                        bits.append("mute")
                    elif abs(vol - 1.0) > 1e-3:
                        bits.append(f"vol {vol:.2f}")
                    if logo:
                        bits.append("logo")
                    if srt:
                        bits.append("subs")
                    if bits:
                        note(f"  Options: {', '.join(bits)}")
                prog(0.03, f"{act}: encoding…")
                try:
                    if act == "render_cut":
                        out = ops.render_cut(
                            src,
                            dest,
                            start=start,
                            end=end if end else None,
                            crop_x=cx,
                            crop_y=cy,
                            crop_w=cw,
                            crop_h=ch,
                            speed=speed,
                            volume=vol,
                            mute=mute,
                            video_fade_in=vfi,
                            video_fade_out=vfo,
                            audio_fade_in=afi,
                            audio_fade_out=afo,
                            logo=logo,
                            logo_position=str(a.get("logo_pos") or "top-right"),
                            logo_scale=float(a.get("logo_scale") or 0.15),
                            srt=srt,
                            crf=crf,
                            preset=preset,
                            on_progress=prog,
                        )
                    else:
                        out = ops.fade_media(
                            src,
                            dest,
                            fade_in=0.0,
                            fade_out=0.0,
                            video_fade_in=vfi,
                            video_fade_out=vfo,
                            audio_fade_in=afi,
                            audio_fade_out=afo,
                            start=start,
                            end=end if end else None,
                            crf=crf,
                            preset=preset,
                            on_progress=prog,
                        )
                except CancelledError:
                    note(f"  {act} cancelled.")
                    raise
                note(f"  Writing finished: {Path(out).name}")
                prog(1.0, "100% · done")
                return finish([str(out)])
            if act == "crop":
                cx, cy, cw, ch = self._crop_pixels(src)
                if cw and ch:
                    return go(
                        "crop",
                        lambda: ops.crop_video(src, dest, x=cx, y=cy, width=cw, height=ch),
                    )
                try:
                    margin = int(a.get("crop_margin") or 0)
                except (TypeError, ValueError):
                    margin = 0
                return go(
                    "crop",
                    lambda: ops.crop_video(src, dest, margin=margin),
                )
            if act == "volume":
                return go(
                    "volume",
                    lambda: ops.adjust_volume(
                        src,
                        dest,
                        volume=float(a.get("volume") or 1.0),
                        mute=bool(a.get("mute")),
                    ),
                )
            if act == "speed":
                return go(
                    "speed",
                    lambda: ops.change_speed(
                        src, dest, speed=float(a.get("speed") or 1.0)
                    ),
                )
            if act == "gif":
                return go(
                    "gif",
                    lambda: ops.export_gif(
                        src,
                        dest,
                        start=self._session.in_point,
                        end=self._session.out_or_end or None,
                        fmt=str(a.get("gif_fmt") or "gif"),
                    ),
                )
            if act == "flip":
                return go("flip", lambda: ops.flip_video(src, dest, horizontal=True))
            if act == "target_size":
                return go(
                    "target_size",
                    lambda: ops.target_size_video(
                        src, dest, max_mb=float(a.get("max_mb") or 25)
                    ),
                )
            if act == "burn_subs":
                srt_p = a.get("srt_path")
                if not srt_p:
                    raise RuntimeError("Choose a .srt subtitle file first")
                return go(
                    "burn_subs",
                    lambda: ops.burn_subtitles(src, Path(str(srt_p)), dest),
                )
            if act == "logo":
                logo_p = a.get("logo_path")
                if not logo_p:
                    raise RuntimeError("Choose a logo image first")
                return go(
                    "logo",
                    lambda: ops.logo_overlay(
                        src,
                        Path(str(logo_p)),
                        dest,
                        position=str(a.get("logo_pos") or "top-right"),
                        scale=float(a.get("logo_scale") or 0.15),
                    ),
                )
            raise RuntimeError(f"Unknown edit action: {act}")

        if tool == "Audio":
            act = self.var_audio_action.get()
            fmt = self.var_audio_fmt.get() or "mp3"
            dest_fmt = dest.suffix.lstrip(".").lower() or fmt
            if act == "extract":
                return go(
                    "extract_audio",
                    lambda: ops.extract_audio(src, dest, fmt=dest_fmt if dest_fmt in ops.AUDIO_FORMATS else fmt),
                )
            if act == "convert":
                return go(
                    "convert_audio",
                    lambda: ops.convert_audio(src, dest, fmt=dest_fmt if dest_fmt in ops.AUDIO_FORMATS else fmt),
                )
            if act == "normalize":
                return go("normalize", lambda: ops.normalize_audio(src, dest))
            if act == "mono":
                return go("mono", lambda: ops.to_mono(src, dest))
            if act == "volume":
                return go(
                    "volume",
                    lambda: ops.adjust_volume(
                        src, dest, volume=float(self.var_volume.get() or 1.0)
                    ),
                )
            return go(
                "compress_audio",
                lambda: ops.compress_audio(src, dest, bitrate=self.var_bitrate.get() or "128k"),
            )

        if tool == "Image":
            act = self.var_image_action.get()
            if act == "compress":
                return go(
                    "compress_image",
                    lambda: ops.compress_image(
                        src,
                        dest,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    ),
                )
            if act == "resize":
                return go(
                    "resize",
                    lambda: ops.resize_image(
                        src, dest, max_edge=int(self.var_max_edge.get() or 1920)
                    ),
                )
            if act == "convert":
                f = dest.suffix.lstrip(".").lower() or "png"
                if f == "jpg":
                    f = "jpeg"
                return go("convert_image", lambda: ops.convert_image(src, dest, fmt=f if f in ("png", "jpeg", "webp") else "png"))
            if act == "rotate":
                return go(
                    "rotate_image",
                    lambda: ops.rotate_image(
                        src, dest, degrees=int(self.var_degrees.get() or 90)
                    ),
                )
            if act == "flip":
                return go("flip", lambda: ops.flip_image(src, dest))
            if act == "strip_exif":
                return go("strip_exif", lambda: ops.strip_exif(src, dest))
            return go("images_to_pdf", lambda: ops.images_to_pdf([src], dest))

        more = self.var_more.get()
        if more == "remux":
            f = dest.suffix.lstrip(".").lower() or "mp4"
            return go("remux", lambda: ops.remux(src, dest, fmt=f if f in ops.VIDEO_FORMATS else "mp4"))
        if more == "strip_audio":
            return go("strip_audio", lambda: ops.strip_audio(src, dest))
        if more == "frame":
            t = self._session.position
            return go("frame", lambda: ops.grab_frame(src, dest, time=t))
        if more == "rotate_video":
            return go(
                "rotate_video",
                lambda: ops.rotate_video(
                    src, dest, degrees=int(self.var_degrees.get() or 90)
                ),
            )
        if more == "flip_video":
            return go("flip_video", lambda: ops.flip_video(src, dest, horizontal=True))
        raise RuntimeError(f"Unknown action: {more}")

    def _export_done(
        self, ok: bool, lines: list[str], dest: Path | None = None
    ) -> None:
        """Finish export: confirm once, then clear export chrome for the next task."""
        self._busy = False
        self._export_proc_active = False
        try:
            self.btn_cancel.configure(state="disabled")
        except Exception:
            pass

        path: Path | None = None
        if ok:
            self._log("Export complete.")
            path = dest
            for line in lines:
                p = Path(line)
                if p.exists():
                    path = p
                    break
            self._last_export_path = path
            if path:
                self._log(f"  Saved → {path}")
            for line in lines:
                self._log("  ✓ " + line)
            # One-shot confirmation; then wipe transient export UI
            summary = str(lines[0]) if lines else (str(path) if path else "done")
            try:
                messagebox.showinfo(__app_name__, f"Saved:\n{summary}")
            except Exception:
                pass
            # Project stays loaded; export chrome returns to idle for the next job
            self._reset_export_chrome(status="Ready", keep_open_folder=bool(path))
            # In-place replace closed the preview — reload the new file
            reload_path = self._pending_reload_path
            self._pending_reload_path = None
            if reload_path and Path(reload_path).is_file():
                self.after(80, lambda p=Path(reload_path): self._reload_after_inplace_replace(p))
            self._log("Ready for next export.")
            return

        cancelled = bool(lines and "Cancel" in (lines[0] or ""))
        self._log("Export stopped." if cancelled else "Export failed.")
        for line in lines:
            self._log("  ✗ " + line)
        # Drop pending reload; try to restore preview if we closed for a failed replace
        failed_reload = self._pending_reload_path
        self._pending_reload_path = None
        if cancelled:
            try:
                messagebox.showinfo(__app_name__, "Export cancelled.")
            except Exception:
                pass
            self._reset_export_chrome(status="Ready")
        else:
            err = lines[0] if lines else "Unknown error"
            try:
                messagebox.showerror(__app_name__, f"Export failed:\n{err}")
            except Exception:
                pass
            self._reset_export_chrome(status="Ready")
        # If session was closed during a failed in-place attempt, reopen original
        if failed_reload is None and self._session.info is None and self._current_path():
            cur = self._current_path()
            if cur and cur.is_file():
                self.after(80, lambda p=cur: self._reload_after_inplace_replace(p))
        self._log("Ready for next export.")

    # ── dialogs ─────────────────────────────────────────────
    def _maybe_first_run(self) -> None:
        data = app_prefs.load_prefs()
        if data.get("first_run_completed"):
            return
        win = ctk.CTkToplevel(self)
        win.title(f"Welcome to {__app_name__}")
        win.geometry("500x340")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text=(
                f"{__app_name__} is an offline media editor.\n\n"
                "• Preview with sound; drag green/red timeline handles to select\n"
                "• Space play · I/O marks · arrows frame-step · Play sel. loops range\n"
                "• Drag pane edges to resize Media / Preview / Tools / Log\n"
                "• Export Save As or batch folder · Cancel anytime · Open folder\n"
                "• Window size and layout are remembered · nothing is uploaded\n"
            ),
            justify="left",
        ).pack(padx=16, pady=16)
        var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(win, text="Enable anonymous diagnostics export", variable=var).pack(
            padx=16, anchor="w"
        )

        def close() -> None:
            data["first_run_completed"] = True
            data["diagnostics_enabled"] = bool(var.get())
            app_prefs.save_prefs(data)
            win.destroy()

        ctk.CTkButton(win, text="Continue", command=close).pack(pady=16)

    def _settings(self) -> None:
        data = app_prefs.load_prefs()
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("440x360")
        win.minsize(360, 300)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="Appearance", font=ctk.CTkFont(weight="bold")).pack(
            padx=16, pady=(16, 4), anchor="w"
        )
        mode_var = ctk.StringVar(value=str(data.get("appearance_mode") or "System"))
        ctk.CTkOptionMenu(
            win, variable=mode_var, values=["System", "Light", "Dark"], width=200
        ).pack(padx=16, anchor="w")

        ctk.CTkLabel(win, text="Window", font=ctk.CTkFont(weight="bold")).pack(
            padx=16, pady=(14, 4), anchor="w"
        )
        remember_var = ctk.BooleanVar(value=bool(data.get("remember_window", True)))
        ctk.CTkCheckBox(
            win,
            text="Remember size, position, and pane layout",
            variable=remember_var,
        ).pack(padx=16, anchor="w")
        ctk.CTkLabel(
            win,
            text="Drag the gray bars between panes to resize Media, Preview, Tools, and Log.",
            wraplength=400,
            justify="left",
            text_color=("gray40", "gray60"),
        ).pack(padx=16, pady=(6, 4), anchor="w")

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

        ctk.CTkButton(win, text="Reset layout to defaults", command=reset_layout).pack(
            padx=16, pady=8, anchor="w"
        )

        var = ctk.BooleanVar(value=bool(data.get("diagnostics_enabled")))
        ctk.CTkCheckBox(win, text="Anonymous diagnostics export", variable=var).pack(
            padx=16, pady=(8, 4), anchor="w"
        )

        def save() -> None:
            data["diagnostics_enabled"] = bool(var.get())
            data["remember_window"] = bool(remember_var.get())
            data["appearance_mode"] = mode_var.get()
            app_prefs.save_prefs(data)
            self._prefs = data
            try:
                ctk.set_appearance_mode(mode_var.get())
                # Refresh sash colors for light/dark
                bg = self._sash_bg()
                self._hpaned.configure(bg=bg)
                self._vpaned.configure(bg=bg)
            except Exception:
                pass
            win.destroy()

        ctk.CTkButton(win, text="Save", command=save, width=100).pack(pady=16)

    def _about(self) -> None:
        data = app_prefs.load_prefs()
        win = ctk.CTkToplevel(self)
        win.title("About")
        win.geometry("520x380")
        ctk.CTkLabel(
            win,
            text=(
                f"{__app_name__} {__version__}\n"
                "MIT · free forever · offline only\n\n"
                "Visual editor: preview, timeline, In/Out, export progress.\n"
                "Video/audio engine: ffmpeg · Images: Pillow · Preview: OpenCV"
            ),
            justify="left",
        ).pack(padx=12, pady=12)

        def copy_diag() -> None:
            if not data.get("diagnostics_enabled"):
                messagebox.showinfo(__app_name__, "Enable diagnostics in Settings first.")
                return
            self.clipboard_clear()
            self.clipboard_append(build_report())
            messagebox.showinfo(__app_name__, "Diagnostics copied.")

        ctk.CTkButton(win, text="Copy diagnostics", command=copy_diag).pack(pady=8)
        ctk.CTkButton(win, text="Close", command=win.destroy).pack(pady=4)


def main() -> None:
    try:
        app = ClipworkApp()
        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        try:
            from clipwork.diagnostics import crash_log_path

            path = crash_log_path()
        except Exception:
            path = Path.cwd() / "clipwork_crash.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                encoding="utf-8",
            )
        except OSError:
            path = Path.cwd() / "clipwork_crash.log"
            try:
                path.write_text(str(exc), encoding="utf-8")
            except OSError:
                pass
        try:
            messagebox.showerror(__app_name__, f"Failed to start:\n{exc}\n\nLog:\n{path}")
        except Exception:
            print(exc, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
