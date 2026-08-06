"""Clipwork GUI — offline media editor with visual preview. Free forever."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

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
PREVIEW_MAX = (720, 405)
# Cap UI paint rate during play (OpenCV seek is heavy).
_PREVIEW_MIN_INTERVAL_MS = 50

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
    out = img.copy()
    out.thumbnail(max_size, Image.Resampling.LANCZOS)
    return out


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
        self.geometry("1200x780")

        self._files: list[Path] = []
        self._selected_idx: int = -1
        self._busy = False
        self._session = MediaSession()
        self._session.on_frame = self._on_session_frame
        self._session.on_position = self._on_session_position
        self._preview_photo = None  # keep ref (CTkImage or PhotoImage)
        self._scrub_dragging = False
        self._updating_scrub = False
        self._last_paint_ms = 0.0
        self._paint_pending = False

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
            center, width=PREVIEW_MAX[0], height=PREVIEW_MAX[1], fg_color=("gray85", "gray18")
        )
        self.preview_frame.pack(padx=10, pady=(10, 6))
        self.preview_frame.pack_propagate(False)
        self.preview_label = ctk.CTkLabel(
            self.preview_frame,
            text="Add a video, audio, or image file to preview",
            fg_color="transparent",
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
            values=["Convert", "Compress", "Trim", "Audio", "Image", "More"],
            command=self._on_tool_change,
        )
        self.tool.set("Trim")
        self.tool.pack(fill="x", padx=10, pady=4)

        self.tool_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.tool_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self._panels: dict[str, ctk.CTkFrame] = {}
        self._build_tool_panels()

        ctk.CTkButton(
            right, text="Export / Run", height=36, command=self._run, font=ctk.CTkFont(weight="bold")
        ).pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(
            right,
            text="Trim uses In/Out from the timeline.\nPreview shows your scrub position.",
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

        for name in ("Convert", "Compress", "Trim", "Audio", "Image", "More"):
            self._panels[name] = ctk.CTkFrame(self.tool_frame, fg_color="transparent")

        fr = self._panels["Convert"]
        ctk.CTkLabel(fr, text="Output format").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_fmt,
            values=["mp4", "webm", "mkv", "mov", "mp3", "wav", "png", "jpg", "webp"],
        ).pack(fill="x", pady=4)

        fr = self._panels["Compress"]
        ctk.CTkLabel(fr, text="Video preset").pack(anchor="w")
        ctk.CTkOptionMenu(fr, variable=self.var_preset, values=list(ops.COMPRESS_PRESETS)).pack(
            fill="x", pady=4
        )
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
        ctk.CTkButton(fr, text="Go to In", command=lambda: self._goto_mark("in")).pack(fill="x", pady=2)
        ctk.CTkButton(fr, text="Go to Out", command=lambda: self._goto_mark("out")).pack(
            fill="x", pady=2
        )

        fr = self._panels["Audio"]
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_audio_action,
            values=["extract", "convert", "normalize", "mono", "compress"],
        ).pack(fill="x", pady=4)
        ctk.CTkOptionMenu(fr, variable=self.var_audio_fmt, values=list(ops.AUDIO_FORMATS)).pack(
            fill="x", pady=4
        )

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
            values=["remux", "strip_audio", "frame", "rotate_video", "concat"],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="frame uses current playhead.\nconcat uses all videos in the list.",
            wraplength=260,
            justify="left",
        ).pack(anchor="w")

        self._on_tool_change("Trim")

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
        # Copy immediately; source buffer may be reused by OpenCV.
        try:
            frame = img.copy()
        except Exception:
            return
        # Marshal to UI thread (Tk/CTk images must be created on main thread)
        self.after(0, lambda i=frame, tt=t: self._show_frame(i, tt))

    def _on_session_position(self, t: float) -> None:
        self.after(0, lambda: self._sync_scrub(t))

    def _show_frame(self, img: Image.Image, t: float) -> None:
        """Paint preview on the main thread only."""
        import time as _time

        now = _time.perf_counter() * 1000.0
        if self._session.playing and (now - self._last_paint_ms) < _PREVIEW_MIN_INTERVAL_MS:
            return
        self._last_paint_ms = now
        try:
            fitted = img.convert("RGB")
            fitted.thumbnail(PREVIEW_MAX, Image.Resampling.LANCZOS)
            w, h = int(fitted.size[0]), int(fitted.size[1])
            if w < 1 or h < 1:
                return

            # Prefer CTkImage (required by CTkLabel). Separate copies for light/dark.
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
                # Fallback: classic tk Label + PhotoImage (never pass PhotoImage to CTkLabel)
                try:
                    import tkinter as tk
                    from PIL import ImageTk

                    if self._tk_preview is None:
                        self._tk_preview = tk.Label(
                            self.preview_frame, bg="#2b2b2b", borderwidth=0
                        )
                    photo = ImageTk.PhotoImage(fitted, master=self)
                    self._preview_photo = photo
                    self._tk_preview.configure(image=photo)
                    self.preview_label.place_forget()
                    self._tk_preview.place(relx=0.5, rely=0.5, anchor="center")
                except Exception as tk_exc:
                    self._log(
                        f"Preview paint failed (CTk: {ctk_exc}; Tk: {tk_exc})"
                    )
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

    def _run(self) -> None:
        if self._busy:
            return
        src = self._current_path()
        tool = self.tool.get()
        if tool == "More" and self.var_more.get() == "concat":
            files = [p for p in self._files if p.suffix.lower() in VIDEO_EXTS]
            if len(files) < 2:
                messagebox.showwarning(__app_name__, "Add at least two videos for concat.")
                return
        elif not src:
            messagebox.showwarning(__app_name__, "Select a media file first.")
            return

        if tool != "Image" and not ops.find_ffmpeg():
            # Image-only tools can run without ffmpeg
            if tool not in ("Image",) and not (
                src and src.suffix.lower() in IMAGE_EXTS and tool in ("Convert", "Compress")
            ):
                messagebox.showerror(__app_name__, "ffmpeg not found (required for video/audio).")
                return

        self._busy = True
        self._session.stop()
        self.btn_play.configure(text="Play")
        self._set_status("Exporting…")
        self._set_progress(0.02, "Starting…")

        def work() -> None:
            try:
                lines = self._export_worker(tool, src)
                self.after(0, lambda: self._export_done(True, lines))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._export_done(False, [str(exc)]))

        threading.Thread(target=work, daemon=True).start()

    def _export_worker(self, tool: str, src: Path | None) -> list[str]:
        results: list[str] = []

        def prog(frac: float, label: str) -> None:
            self.after(0, lambda: self._set_progress(frac, label))

        if tool == "More" and self.var_more.get() == "concat":
            vids = [p for p in self._files if p.suffix.lower() in VIDEO_EXTS]
            dest = vids[0].with_name(vids[0].stem + "_concat.mp4")
            jr = jobs.run_job(
                "concat",
                lambda: ops.concat_videos(vids, dest, reencode=True),
                inputs=vids,
            )
            if not jr.ok:
                raise RuntimeError(jr.error)
            prog(1.0, "Done")
            return [str(jr.paths[0])]

        assert src is not None
        ext = src.suffix.lower()

        if tool == "Trim":
            start = self._session.in_point
            end = self._session.out_or_end
            dur = None
            if end and end > start:
                dur = end - start
            reenc = bool(self.var_reencode.get())
            from clipwork.media_ops.ffmpeg_util import default_output

            suffix = src.suffix or ".mp4"
            out_path = default_output(src, suffix, "trim")
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

        # Non-trim tools (use selected file)
        def go(name: str, fn):  # type: ignore[no-untyped-def]
            jr = jobs.run_job(name, fn, inputs=[src])
            if not jr.ok:
                raise RuntimeError(jr.error or name)
            prog(1.0, "Done")
            return [str(p) for p in jr.paths]

        if tool == "Convert":
            fmt = self.var_fmt.get()
            if ext in IMAGE_EXTS or fmt in ("png", "jpg", "webp"):
                f = fmt if fmt in ("png", "jpg", "webp") else "png"
                return go("convert_image", lambda: ops.convert_image(src, fmt=f))
            if ext in AUDIO_EXTS or fmt in ops.AUDIO_FORMATS:
                f = fmt if fmt in ops.AUDIO_FORMATS else "mp3"
                return go("convert_audio", lambda: ops.convert_audio(src, fmt=f))
            f = fmt if fmt in ops.VIDEO_FORMATS else "mp4"
            return go("convert_video", lambda: ops.convert_video(src, fmt=f))

        if tool == "Compress":
            if ext in IMAGE_EXTS:
                return go(
                    "compress_image",
                    lambda: ops.compress_image(
                        src,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    ),
                )
            if ext in AUDIO_EXTS:
                return go(
                    "compress_audio",
                    lambda: ops.compress_audio(src, bitrate=self.var_bitrate.get() or "128k"),
                )
            return go(
                "compress_video",
                lambda: ops.compress_video(src, preset=self.var_preset.get() or "balanced"),
            )

        if tool == "Audio":
            act = self.var_audio_action.get()
            fmt = self.var_audio_fmt.get() or "mp3"
            if act == "extract":
                return go("extract_audio", lambda: ops.extract_audio(src, fmt=fmt))
            if act == "convert":
                return go("convert_audio", lambda: ops.convert_audio(src, fmt=fmt))
            if act == "normalize":
                return go("normalize", lambda: ops.normalize_audio(src))
            if act == "mono":
                return go("mono", lambda: ops.to_mono(src))
            return go(
                "compress_audio",
                lambda: ops.compress_audio(src, bitrate=self.var_bitrate.get() or "128k"),
            )

        if tool == "Image":
            act = self.var_image_action.get()
            if act == "compress":
                return go(
                    "compress_image",
                    lambda: ops.compress_image(
                        src,
                        quality=int(self.var_quality.get() or 75),
                        max_edge=int(self.var_max_edge.get() or 1920),
                    ),
                )
            if act == "resize":
                return go(
                    "resize",
                    lambda: ops.resize_image(src, max_edge=int(self.var_max_edge.get() or 1920)),
                )
            if act == "convert":
                return go("convert_image", lambda: ops.convert_image(src, fmt="png"))
            if act == "rotate":
                return go(
                    "rotate_image",
                    lambda: ops.rotate_image(src, degrees=int(self.var_degrees.get() or 90)),
                )
            if act == "flip":
                return go("flip", lambda: ops.flip_image(src))
            if act == "strip_exif":
                return go("strip_exif", lambda: ops.strip_exif(src))
            return go("images_to_pdf", lambda: ops.images_to_pdf([src], src.with_suffix(".pdf")))

        more = self.var_more.get()
        if more == "remux":
            return go("remux", lambda: ops.remux(src, fmt="mp4"))
        if more == "strip_audio":
            return go("strip_audio", lambda: ops.strip_audio(src))
        if more == "frame":
            t = self._session.position
            return go("frame", lambda: ops.grab_frame(src, time=t))
        if more == "rotate_video":
            return go(
                "rotate_video",
                lambda: ops.rotate_video(src, degrees=int(self.var_degrees.get() or 90)),
            )
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
