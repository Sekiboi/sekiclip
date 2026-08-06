"""Clipwork GUI — offline media editor with visual preview. Free forever."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

from PIL import Image

from clipwork import __app_name__, __version__
from clipwork import jobs
from clipwork import media_ops as ops
from clipwork import prefs as app_prefs
from clipwork.diagnostics import build_report
from clipwork.media_preview import (
    MediaKind,
    MediaSession,
    format_time,
    run_ffmpeg_with_progress,
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

APP_USER_MODEL_ID = "Sekiboi.Clipwork"
MIN_W, MIN_H = 1100, 720
# Fixed preview stage (16:9). Frames letterbox into this for stable framing.
PREVIEW_W, PREVIEW_H = 960, 540
PREVIEW_MAX = (PREVIEW_W, PREVIEW_H)

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
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{__app_name__} {__version__}")
        self.minsize(MIN_W, MIN_H)
        self.geometry("1280x860")

        self._files: list[Path] = []
        self._selected_idx: int = -1
        self._busy = False
        self._session = MediaSession()
        self._session.on_frame = self._on_session_frame
        self._session.on_position = self._on_session_position
        self._session.on_status = self._on_session_status
        self._preview_photo = None  # keep ref (CTkImage or PhotoImage)
        self._scrub_dragging = False
        self._updating_scrub = False

        self._build()
        self._set_icons()
        self.after(200, self._maybe_first_run)
        self._refresh_ffmpeg_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
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
    def _build(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(10, 4))
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

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=12, pady=4)

        # Left: file list
        left = ctk.CTkFrame(main, width=220)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="Media", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.file_list = ctk.CTkScrollableFrame(left, width=200)
        self.file_list.pack(fill="both", expand=True, padx=6, pady=4)
        self._file_buttons: list[ctk.CTkButton] = []

        lb = ctk.CTkFrame(left, fg_color="transparent")
        lb.pack(fill="x", padx=6, pady=6)
        ctk.CTkButton(lb, text="Add…", width=70, command=self._add_files).pack(side="left", padx=2)
        ctk.CTkButton(lb, text="Clear", width=60, command=self._clear_files).pack(side="left", padx=2)

        # Center: preview + timeline
        center = ctk.CTkFrame(main)
        center.pack(side="left", fill="both", expand=True, padx=(0, 8))

        self.preview_frame = ctk.CTkFrame(
            center, width=PREVIEW_W, height=PREVIEW_H, fg_color=("#1a1a1e", "#141418")
        )
        self.preview_frame.pack(padx=10, pady=(10, 6))
        self.preview_frame.pack_propagate(False)
        self.preview_label = ctk.CTkLabel(
            self.preview_frame,
            text="Add a video, audio, or image file to preview\n"
            "Play includes sound once audio preview is ready",
            fg_color="transparent",
            text_color=("gray50", "gray60"),
            justify="center",
        )
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
        # Optional native tk label fallback when CTkImage is unhappy
        self._tk_preview = None
        if _HAS_DND:
            try:
                self.preview_frame.drop_target_register(DND_FILES)
                self.preview_frame.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        self.info_line = ctk.CTkLabel(center, text="", anchor="w", text_color=("gray30", "gray70"))
        self.info_line.pack(fill="x", padx=12)

        # Transport
        transport = ctk.CTkFrame(center, fg_color="transparent")
        transport.pack(fill="x", padx=10, pady=4)
        self.btn_play = ctk.CTkButton(transport, text="Play", width=70, command=self._toggle_play)
        self.btn_play.pack(side="left", padx=2)
        ctk.CTkButton(transport, text="Stop", width=60, command=self._stop).pack(side="left", padx=2)
        self.time_label = ctk.CTkLabel(transport, text="00:00.00 / 00:00.00", width=140)
        self.time_label.pack(side="left", padx=8)

        ctk.CTkButton(transport, text="[ In", width=50, command=self._mark_in).pack(side="left", padx=2)
        ctk.CTkButton(transport, text="Out ]", width=50, command=self._mark_out).pack(
            side="left", padx=2
        )
        ctk.CTkButton(transport, text="Clear I/O", width=70, command=self._clear_io).pack(
            side="left", padx=2
        )
        self.io_label = ctk.CTkLabel(transport, text="In 00:00 → Out end", text_color=("gray30", "gray70"))
        self.io_label.pack(side="left", padx=8)

        # Scrubber
        scrub_fr = ctk.CTkFrame(center, fg_color="transparent")
        scrub_fr.pack(fill="x", padx=12, pady=(2, 4))
        ctk.CTkLabel(scrub_fr, text="Timeline").pack(anchor="w")
        self.scrub = ctk.CTkSlider(
            scrub_fr, from_=0, to=1, number_of_steps=1000, command=self._on_scrub
        )
        self.scrub.set(0)
        self.scrub.pack(fill="x", pady=4)
        self.scrub.bind("<ButtonPress-1>", lambda _e: setattr(self, "_scrub_dragging", True))
        self.scrub.bind("<ButtonRelease-1>", self._on_scrub_release)

        # Range indicators (text)
        self.range_label = ctk.CTkLabel(
            center,
            text="Drag the timeline to scrub. Set In/Out for the region you will export.",
            text_color=("gray40", "gray60"),
            anchor="w",
        )
        self.range_label.pack(fill="x", padx=12, pady=(0, 6))

        # Job progress
        prog_fr = ctk.CTkFrame(center, fg_color="transparent")
        prog_fr.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(prog_fr, text="Export progress").pack(anchor="w")
        self.progress = ctk.CTkProgressBar(prog_fr)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=2)
        self.progress_label = ctk.CTkLabel(prog_fr, text="Idle", anchor="w")
        self.progress_label.pack(fill="x")

        # Right: tools
        right = ctk.CTkFrame(main, width=300)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="Tools", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 4)
        )
        self.tool = ctk.CTkSegmentedButton(
            right,
            values=["Convert", "Compress", "Trim", "Edit", "Audio", "Image", "More"],
            command=self._on_tool_change,
        )
        self.tool.set("Trim")
        self.tool.pack(fill="x", padx=10, pady=4)

        self.tool_frame = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.tool_frame.pack(fill="both", expand=True, padx=4, pady=4)
        self._panels: dict[str, ctk.CTkFrame] = {}
        self._build_tool_panels()

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
        ctk.CTkLabel(
            right,
            text="Single file: Save As dialog.\n"
            "Batch: pick an output folder.\n"
            "Trim/GIF use timeline In/Out.",
            wraplength=270,
            justify="left",
            text_color=("gray40", "gray60"),
        ).pack(padx=10, pady=(0, 8))

        # Bottom log
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(0, 8))
        row = ctk.CTkFrame(bottom, fg_color="transparent")
        row.pack(fill="x")
        self.status = ctk.CTkLabel(row, text="Ready", anchor="w")
        self.status.pack(side="left")
        ctk.CTkButton(row, text="About", width=70, command=self._about).pack(side="right", padx=4)
        ctk.CTkButton(row, text="Settings", width=80, command=self._settings).pack(side="right")
        self.log = ctk.CTkTextbox(bottom, height=90)
        self.log.pack(fill="x", pady=(4, 0))
        self.log.insert("1.0", "Open a file to preview. Scrub the timeline, set In/Out, then Export.\n")
        self.log.configure(state="disabled")

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
        self.var_edit_action = ctk.StringVar(value="crop")
        self.var_crop_margin = ctk.StringVar(value="40")
        self.var_volume = ctk.StringVar(value="1.0")
        self.var_mute = ctk.BooleanVar(value=False)
        self.var_speed = ctk.StringVar(value="1.5")
        self.var_gif_fmt = ctk.StringVar(value="gif")
        self.var_fade_in = ctk.StringVar(value="0.5")
        self.var_fade_out = ctk.StringVar(value="0.5")
        self.var_max_mb = ctk.StringVar(value="25")
        self.var_logo_pos = ctk.StringVar(value="top-right")
        self.var_logo_scale = ctk.StringVar(value="0.15")
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
            text="Uses timeline In/Out marks.\nRe-encode recommended for accuracy.",
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
        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_edit_action,
            values=[
                "crop",
                "volume",
                "speed",
                "gif",
                "fade",
                "flip",
                "target_size",
                "burn_subs",
                "logo",
            ],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Crop margin (px)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_crop_margin).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="Volume (1.0 = normal)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_volume).pack(fill="x", pady=2)
        ctk.CTkCheckBox(fr, text="Mute", variable=self.var_mute).pack(anchor="w", pady=2)
        ctk.CTkLabel(fr, text="Speed").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_speed, values=list(ops.SPEED_PRESETS)
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(fr, text="GIF format / fade in-out (s) / max MB").pack(anchor="w")
        ctk.CTkOptionMenu(fr, variable=self.var_gif_fmt, values=["gif", "webp"]).pack(
            fill="x", pady=2
        )
        ctk.CTkEntry(fr, textvariable=self.var_fade_in).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_fade_out).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_max_mb).pack(fill="x", pady=2)
        ctk.CTkButton(fr, text="Choose subtitle .srt…", command=self._pick_srt).pack(
            fill="x", pady=2
        )
        ctk.CTkButton(fr, text="Choose logo image…", command=self._pick_logo).pack(
            fill="x", pady=2
        )
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_logo_pos,
            values=["top-right", "top-left", "bottom-right", "bottom-left", "center"],
        ).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_logo_scale).pack(fill="x", pady=2)
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

        self._on_tool_change("Trim")

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
        self.edit_files_label.configure(text=" · ".join(parts) if parts else "No .srt / logo chosen")

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
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

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
        self._set_status("Ready - scrub timeline, set In/Out, export")
        self._updating_scrub = True
        self.scrub.configure(to=max(duration, 0.001))
        self.scrub.set(0)
        self._updating_scrub = False
        self._update_time_labels(0, duration)
        self._update_io_label()
        self.btn_play.configure(text="Play")
        # First paint on the UI thread (safe for CTk / Tk photo images)
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

    def _show_frame(self, img: Image.Image, t: float) -> None:
        """Paint preview on the main thread only (letterboxed stage)."""
        try:
            fitted = _fit_image(img, (PREVIEW_W, PREVIEW_H))
            w, h = PREVIEW_W, PREVIEW_H
            try:
                light = fitted
                dark = fitted.copy()
                ctk_img = ctk.CTkImage(light_image=light, dark_image=dark, size=(w, h))
                self._preview_photo = ctk_img
                if self._tk_preview is not None:
                    self._tk_preview.place_forget()
                self.preview_label.configure(image=ctk_img, text="")
                self.preview_label.place(relx=0.5, rely=0.5, anchor="center")
                return
            except Exception as ctk_exc:
                try:
                    import tkinter as tk
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
            self.scrub.set(t)
        finally:
            self._updating_scrub = False
        dur = self._session.duration
        self._update_time_labels(t, dur)
        if self._session.playing:
            self.btn_play.configure(text="Pause")

    def _on_scrub(self, value: float) -> None:
        if self._updating_scrub:
            return
        t = float(value)
        self._session.stop()
        self.btn_play.configure(text="Play")
        self._session.seek(t)
        self._update_time_labels(t, self._session.duration)

    def _on_scrub_release(self, _event=None) -> None:  # type: ignore[no-untyped-def]
        self._scrub_dragging = False
        self._session.seek(float(self.scrub.get()))

    def _update_time_labels(self, pos: float, dur: float) -> None:
        self.time_label.configure(text=f"{format_time(pos)} / {format_time(dur)}")

    def _update_io_label(self) -> None:
        inn = format_time(self._session.in_point)
        out = (
            format_time(self._session.out_point)
            if self._session.out_point is not None
            else "end"
        )
        self.io_label.configure(text=f"In {inn} → Out {out}")
        self.range_label.configure(
            text=f"Export region: {inn} → {out}  (duration {format_time(max(0, (self._session.out_or_end or 0) - self._session.in_point))})"
        )

    def _toggle_play(self) -> None:
        if not self._session.info:
            return
        if self._session.playing:
            self._session.stop()
            self.btn_play.configure(text="Play")
        else:
            self._session.play()
            self.btn_play.configure(text="Pause")

    def _stop(self) -> None:
        self._session.stop()
        self.btn_play.configure(text="Play")
        self._session.seek(self._session.in_point)

    def _mark_in(self) -> None:
        self._session.set_in()
        self._update_io_label()
        self._log(f"In → {format_time(self._session.in_point)}")

    def _mark_out(self) -> None:
        self._session.set_out()
        self._update_io_label()
        self._log(f"Out → {format_time(self._session.out_or_end)}")

    def _clear_io(self) -> None:
        self._session.clear_in_out()
        self._update_io_label()
        self._log("In/Out cleared")

    def _goto_mark(self, which: str) -> None:
        if which == "in":
            self._session.seek(self._session.in_point)
        else:
            self._session.seek(self._session.out_or_end or 0)

    # ── export ──────────────────────────────────────────────
    def _current_path(self) -> Path | None:
        if 0 <= self._selected_idx < len(self._files):
            return self._files[self._selected_idx]
        return None

    def _set_progress(self, frac: float, label: str) -> None:
        self.progress.set(max(0.0, min(1.0, frac)))
        self.progress_label.configure(text=label)

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
            return initial_dir, f"{stem}_trim{src.suffix or '.mp4'}", video_ft + audio_ft

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
            if act in ("crop", "speed", "fade", "flip", "volume", "burn_subs", "logo"):
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
        """Show Save As dialog. Returns None if cancelled."""
        initial_dir, name, filetypes = self._export_defaults(tool, src)
        # Default extension from suggested name
        def_ext = Path(name).suffix or ".mp4"
        path_str = filedialog.asksaveasfilename(
            parent=self,
            title="Save exported file as…",
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
                self._set_status("Export cancelled")
                return
            dest: Path | None = dest_folder
        else:
            dest = self._ask_save_path(tool, src)
            if dest is None:
                self._set_status("Export cancelled")
                return

        self._busy = True
        self._session.stop()
        self.btn_play.configure(text="Play")
        label = dest.name if dest else "…"
        self._set_status(f"Exporting to {label}…")
        self._set_progress(0.02, "Starting…")

        def work() -> None:
            try:
                if batch:
                    lines = self._export_batch(tool, list(self._files), dest)  # type: ignore[arg-type]
                else:
                    lines = self._export_worker(tool, src, dest)  # type: ignore[arg-type]
                self.after(0, lambda: self._export_done(True, lines))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._export_done(False, [str(exc)]))

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
        act = self.var_edit_action.get()
        margin = int(self.var_crop_margin.get() or 0)
        vol = float(self.var_volume.get() or 1.0)
        mute = bool(self.var_mute.get())
        speed = float(self.var_speed.get() or 1.0)
        fi = float(self.var_fade_in.get() or 0.5)
        fo = float(self.var_fade_out.get() or 0.5)
        max_mb = float(self.var_max_mb.get() or 25)
        start = self._session.in_point
        end = self._session.out_or_end
        gif_fmt = self.var_gif_fmt.get() or "gif"

        def run(src: Path, dest: Path) -> Path:
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
                return ops.fade_media(src, dest, fade_in=fi, fade_out=fo)
            if act == "flip":
                return ops.flip_video(src, dest, horizontal=True)
            if act == "target_size":
                return ops.target_size_video(src, dest, max_mb=max_mb)
            if act == "burn_subs":
                if not self._srt_path:
                    raise RuntimeError("Choose a .srt subtitle file first")
                return ops.burn_subtitles(src, self._srt_path, dest)
            if act == "logo":
                if not self._logo_path:
                    raise RuntimeError("Choose a logo image first")
                return ops.logo_overlay(
                    src,
                    self._logo_path,
                    dest,
                    position=self.var_logo_pos.get() or "top-right",
                    scale=float(self.var_logo_scale.get() or 0.15),
                )
            raise RuntimeError(f"Unknown edit action: {act}")

        return f"edit_{act}", act, run

    def _export_batch(self, tool: str, files: list[Path], out_dir: Path) -> list[str]:
        op_name, tag, run_one = self._batch_runner_for_tool(tool)
        total = len(files)

        def on_prog(i: int, n: int, name: str) -> None:
            self.after(
                0,
                lambda: self._set_progress(
                    i / max(n, 1), f"Batch {i}/{n}: {name}"
                ),
            )

        results = ops.batch_to_folder(
            files,
            out_dir,
            op_name=op_name,
            run_one=run_one,
            name_tag=tag,
            on_progress=on_prog,
        )
        lines: list[str] = []
        ok_n = 0
        for r in results:
            if r["ok"]:
                ok_n += 1
                lines.append(str(r["dest"]))
            else:
                lines.append(f"FAIL {Path(r['src']).name}: {r['error']}")
        lines.insert(0, f"Batch done: {ok_n}/{total} ok → {out_dir}")
        if ok_n == 0:
            raise RuntimeError("All batch jobs failed")
        return lines

    def _export_worker(self, tool: str, src: Path | None, dest: Path) -> list[str]:
        results: list[str] = []

        def prog(frac: float, label: str) -> None:
            self.after(0, lambda: self._set_progress(frac, label))

        # User picked this path (Save dialog already confirms overwrite on Windows).
        # Remove existing file so ops unique_path() does not invent _1 suffixes.
        if dest.exists():
            try:
                dest.unlink()
            except OSError as exc:
                raise RuntimeError(f"Cannot overwrite {dest.name}: {exc}") from exc

        if tool == "More" and self.var_more.get() == "concat":
            vids = [p for p in self._files if p.suffix.lower() in VIDEO_EXTS]
            jr = jobs.run_job(
                "concat",
                lambda: ops.concat_videos(vids, dest, reencode=True),
                inputs=vids,
            )
            if not jr.ok:
                raise RuntimeError(jr.error)
            prog(1.0, "Done")
            return [str(jr.paths[0] if jr.paths else dest)]

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
            if reenc and ops.find_ffmpeg():
                args = ["-ss", str(start), "-i", str(src)]
                if dur:
                    args += ["-t", str(dur)]
                if suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"):
                    args += ["-c:a", "libmp3lame", str(out_path)]
                else:
                    args += [
                        "-c:v",
                        "libx264",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        str(out_path),
                    ]
                hint = float(dur or self._session.duration or 1)
                try:
                    run_ffmpeg_with_progress(args, duration_hint=hint, on_progress=prog)
                    jr = jobs.JobResult("trim", [out_path], True, 0.0)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(str(exc)) from exc
            else:
                jr = jobs.run_job(
                    "trim",
                    lambda: ops.trim_media(
                        src, out_path, start=start, end=end if end else None, reencode=reenc
                    ),
                    inputs=[src],
                )
                prog(1.0, "Done")
            if not jr.ok and not out_path.is_file():
                raise RuntimeError(jr.error or "trim failed")
            results.append(str(out_path if out_path.is_file() else jr.paths[0]))
            return results

        def go(name: str, fn):  # type: ignore[no-untyped-def]
            jr = jobs.run_job(name, fn, inputs=[src])
            if not jr.ok:
                raise RuntimeError(jr.error or name)
            prog(1.0, "Done")
            return [str(p) for p in jr.paths] if jr.paths else [str(dest)]

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
            act = self.var_edit_action.get()
            if act == "crop":
                return go(
                    "crop",
                    lambda: ops.crop_video(
                        src, dest, margin=int(self.var_crop_margin.get() or 0)
                    ),
                )
            if act == "volume":
                return go(
                    "volume",
                    lambda: ops.adjust_volume(
                        src,
                        dest,
                        volume=float(self.var_volume.get() or 1.0),
                        mute=bool(self.var_mute.get()),
                    ),
                )
            if act == "speed":
                return go(
                    "speed",
                    lambda: ops.change_speed(
                        src, dest, speed=float(self.var_speed.get() or 1.0)
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
                        fmt=self.var_gif_fmt.get() or "gif",
                    ),
                )
            if act == "fade":
                return go(
                    "fade",
                    lambda: ops.fade_media(
                        src,
                        dest,
                        fade_in=float(self.var_fade_in.get() or 0.5),
                        fade_out=float(self.var_fade_out.get() or 0.5),
                    ),
                )
            if act == "flip":
                return go("flip", lambda: ops.flip_video(src, dest, horizontal=True))
            if act == "target_size":
                return go(
                    "target_size",
                    lambda: ops.target_size_video(
                        src, dest, max_mb=float(self.var_max_mb.get() or 25)
                    ),
                )
            if act == "burn_subs":
                if not self._srt_path:
                    raise RuntimeError("Choose a .srt subtitle file first")
                return go(
                    "burn_subs",
                    lambda: ops.burn_subtitles(src, self._srt_path, dest),  # type: ignore[arg-type]
                )
            if act == "logo":
                if not self._logo_path:
                    raise RuntimeError("Choose a logo image first")
                return go(
                    "logo",
                    lambda: ops.logo_overlay(
                        src,
                        self._logo_path,  # type: ignore[arg-type]
                        dest,
                        position=self.var_logo_pos.get() or "top-right",
                        scale=float(self.var_logo_scale.get() or 0.15),
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

    def _export_done(self, ok: bool, lines: list[str]) -> None:
        self._busy = False
        self._set_status("Done" if ok else "Export failed")
        if ok:
            self._set_progress(1.0, "Done")
        for line in lines:
            self._log(("OK " if ok else "ERR ") + line)
        if ok and lines:
            messagebox.showinfo(__app_name__, f"Saved:\n{lines[0]}")

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
                "• Preview video frames, audio waveforms, and images\n"
                "• Scrub the timeline and set In/Out for trims\n"
                "• Watch export progress as work runs\n"
                "• Nothing is uploaded\n"
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
        win.geometry("400x180")
        var = ctk.BooleanVar(value=bool(data.get("diagnostics_enabled")))
        ctk.CTkCheckBox(win, text="Anonymous diagnostics export", variable=var).pack(
            padx=16, pady=16, anchor="w"
        )

        def save() -> None:
            data["diagnostics_enabled"] = bool(var.get())
            app_prefs.save_prefs(data)
            win.destroy()

        ctk.CTkButton(win, text="Save", command=save).pack(pady=8)

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
