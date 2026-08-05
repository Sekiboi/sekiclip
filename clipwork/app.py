"""Clipwork GUI — offline media toolkit. Free forever."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

from clipwork import __app_name__, __version__
from clipwork import jobs
from clipwork import media_ops as ops
from clipwork import prefs as app_prefs
from clipwork.diagnostics import build_report

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
MIN_W, MIN_H = 960, 640

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = ops.IMAGE_EXTS


def _resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base.joinpath(*parts)
    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def _parse_drop(data: str) -> list[Path]:
    """Parse tkinterdnd2 file list string."""
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
        self.geometry("1000x700")
        self._files: list[Path] = []
        self._busy = False
        self._build()
        self._set_icons()
        self.after(200, self._maybe_first_run)
        self._refresh_ffmpeg_status()

    def _set_icons(self) -> None:
        try:
            ico = _resource_path("assets", "clipwork.ico")
            if ico.is_file():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    def _build(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(top, text=__app_name__, font=ctk.CTkFont(size=22, weight="bold")).pack(
            side="left"
        )
        ctk.CTkLabel(
            top,
            text="Offline · free forever · no uploads",
            text_color=("gray40", "gray60"),
        ).pack(side="left", padx=12)
        self.ff_label = ctk.CTkLabel(top, text="", text_color=("gray40", "gray60"))
        self.ff_label.pack(side="right")

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        left = ctk.CTkFrame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ctk.CTkLabel(left, text="Files", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=8, pady=4)
        self.listbox = ctk.CTkTextbox(left, height=280, activate_scrollbars=True)
        self.listbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.listbox.configure(state="disabled")
        if _HAS_DND:
            try:
                self.listbox.drop_target_register(DND_FILES)
                self.listbox.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.pack(fill="x", padx=8, pady=4)
        ctk.CTkButton(btns, text="Add files…", width=100, command=self._add_files).pack(
            side="left", padx=2
        )
        ctk.CTkButton(btns, text="Clear", width=70, command=self._clear_files).pack(
            side="left", padx=2
        )
        ctk.CTkButton(btns, text="Info", width=70, command=self._show_info).pack(side="left", padx=2)

        right = ctk.CTkFrame(body, width=360)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="Tools", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(8, 4)
        )

        self.tool = ctk.CTkSegmentedButton(
            right,
            values=["Convert", "Compress", "Trim", "Audio", "Image", "More"],
            command=self._on_tool_change,
        )
        self.tool.set("Convert")
        self.tool.pack(fill="x", padx=10, pady=4)

        self.tool_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.tool_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self._tool_widgets: dict[str, ctk.CTkBaseClass] = {}
        self._build_tool_panels()

        ctk.CTkButton(right, text="Run on selected / all", command=self._run).pack(
            fill="x", padx=10, pady=8
        )

        self.status = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status.pack(fill="x", padx=16, pady=(0, 4))
        self.log = ctk.CTkTextbox(self, height=120)
        self.log.pack(fill="x", padx=16, pady=(0, 12))
        self.log.insert("1.0", "Drop files or use Add files…\n")
        self.log.configure(state="disabled")

        menu = ctk.CTkFrame(self, fg_color="transparent", height=28)
        menu.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(menu, text="About / diagnostics", width=140, command=self._about).pack(
            side="left"
        )
        ctk.CTkButton(menu, text="Settings", width=90, command=self._settings).pack(
            side="left", padx=6
        )

    def _build_tool_panels(self) -> None:
        # Shared option vars
        self.var_fmt = ctk.StringVar(value="mp4")
        self.var_preset = ctk.StringVar(value="balanced")
        self.var_start = ctk.StringVar(value="0")
        self.var_end = ctk.StringVar(value="")
        self.var_reencode = ctk.BooleanVar(value=False)
        self.var_audio_fmt = ctk.StringVar(value="mp3")
        self.var_max_edge = ctk.StringVar(value="1920")
        self.var_quality = ctk.StringVar(value="75")
        self.var_degrees = ctk.StringVar(value="90")
        self.var_bitrate = ctk.StringVar(value="128k")
        self._panels: dict[str, ctk.CTkFrame] = {}
        for name in ("Convert", "Compress", "Trim", "Audio", "Image", "More"):
            fr = ctk.CTkFrame(self.tool_frame, fg_color="transparent")
            self._panels[name] = fr
        # Convert
        fr = self._panels["Convert"]
        ctk.CTkLabel(fr, text="Output format").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_fmt, values=["mp4", "webm", "mkv", "mov", "mp3", "wav", "png", "jpg", "webp"]
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Auto-detects video / audio / image from file type.").pack(anchor="w")
        # Compress
        fr = self._panels["Compress"]
        ctk.CTkLabel(fr, text="Video preset").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_preset, values=list(ops.COMPRESS_PRESETS.keys())
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Image quality (1–95)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_quality).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Image max edge (px)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Audio bitrate").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_bitrate).pack(fill="x", pady=4)
        # Trim
        fr = self._panels["Trim"]
        ctk.CTkLabel(fr, text="Start (seconds)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_start).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="End (seconds, optional)").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_end).pack(fill="x", pady=4)
        ctk.CTkCheckBox(fr, text="Re-encode (frame-accurate)", variable=self.var_reencode).pack(
            anchor="w", pady=4
        )
        # Audio
        fr = self._panels["Audio"]
        ctk.CTkLabel(fr, text="Extract / convert format").pack(anchor="w")
        ctk.CTkOptionMenu(
            fr, variable=self.var_audio_fmt, values=list(ops.AUDIO_FORMATS)
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="Run: extract audio from video, or convert/normalize/mono for audio files.\nUse action menu below.",
            wraplength=300,
            justify="left",
        ).pack(anchor="w", pady=4)
        self.var_audio_action = ctk.StringVar(value="extract")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_audio_action,
            values=["extract", "convert", "normalize", "mono", "compress"],
        ).pack(fill="x", pady=4)
        # Image
        fr = self._panels["Image"]
        ctk.CTkLabel(fr, text="Action").pack(anchor="w")
        self.var_image_action = ctk.StringVar(value="compress")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_image_action,
            values=["compress", "resize", "convert", "rotate", "flip", "strip_exif", "to_pdf"],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(fr, text="Max edge / rotate degrees").pack(anchor="w")
        ctk.CTkEntry(fr, textvariable=self.var_max_edge).pack(fill="x", pady=2)
        ctk.CTkEntry(fr, textvariable=self.var_degrees).pack(fill="x", pady=2)
        # More
        fr = self._panels["More"]
        self.var_more = ctk.StringVar(value="remux")
        ctk.CTkOptionMenu(
            fr,
            variable=self.var_more,
            values=["remux", "strip_audio", "frame", "rotate_video", "concat"],
        ).pack(fill="x", pady=4)
        ctk.CTkLabel(
            fr,
            text="remux: change container · frame: still at t=start · concat: all listed videos",
            wraplength=300,
            justify="left",
        ).pack(anchor="w")
        self._on_tool_change("Convert")

    def _on_tool_change(self, name: str) -> None:
        for n, fr in self._panels.items():
            if n == name:
                fr.pack(fill="both", expand=True)
            else:
                fr.pack_forget()

    def _refresh_ffmpeg_status(self) -> None:
        ff = ops.find_ffmpeg()
        self.ff_label.configure(text="ffmpeg: OK" if ff else "ffmpeg: MISSING")

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, msg: str) -> None:
        self.status.configure(text=msg)

    def _refresh_list(self) -> None:
        self.listbox.configure(state="normal")
        self.listbox.delete("1.0", "end")
        for i, p in enumerate(self._files, 1):
            self.listbox.insert("end", f"{i}. {p.name}\n")
        self.listbox.configure(state="disabled")

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add media files",
            filetypes=[
                ("Media", "*.mp4;*.mkv;*.webm;*.mov;*.avi;*.mp3;*.wav;*.flac;*.m4a;*.png;*.jpg;*.jpeg;*.webp"),
                ("All", "*.*"),
            ],
        )
        for p in paths:
            path = Path(p)
            if path.is_file() and path not in self._files:
                self._files.append(path)
        self._refresh_list()

    def _clear_files(self) -> None:
        self._files.clear()
        self._refresh_list()

    def _on_drop(self, event) -> None:  # type: ignore[no-untyped-def]
        for p in _parse_drop(event.data):
            if p not in self._files:
                self._files.append(p)
        self._refresh_list()

    def _show_info(self) -> None:
        if not self._files:
            messagebox.showinfo(__app_name__, "Add files first.")
            return
        lines = []
        for p in self._files[:20]:
            try:
                lines.append(ops.media_summary(p))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{p.name}: {exc}")
        messagebox.showinfo("Media info", "\n".join(lines))

    def _targets(self) -> list[Path]:
        return list(self._files)

    def _run(self) -> None:
        if self._busy:
            return
        files = self._targets()
        if not files:
            messagebox.showwarning(__app_name__, "Add at least one file.")
            return
        if not ops.find_ffmpeg() and self.tool.get() not in ("Image",):
            # Image tools don't need ffmpeg except we still allow image-only
            if self.tool.get() != "Image":
                messagebox.showerror(
                    __app_name__,
                    "ffmpeg not found. Install ffmpeg on PATH or place it in vendor/.",
                )
                return
        tool = self.tool.get()
        self._busy = True
        self._set_status("Working…")

        def work() -> None:
            try:
                results: list[str] = []
                if tool == "More" and self.var_more.get() == "concat":
                    vids = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
                    if len(vids) < 2:
                        raise RuntimeError("Concat needs at least two video files in the list.")
                    dest = vids[0].with_name(vids[0].stem + "_concat.mp4")
                    jr = jobs.run_job(
                        "concat",
                        lambda: ops.concat_videos(vids, dest, reencode=True),
                        inputs=vids,
                    )
                    if not jr.ok:
                        raise RuntimeError(jr.error or "concat failed")
                    results.append(str(jr.paths[0]))
                else:
                    for src in files:
                        jr = self._run_one(tool, src)
                        if jr.ok and jr.paths:
                            results.append(str(jr.paths[0]))
                            for w in jr.warnings:
                                results.append(f"  warn: {w}")
                        else:
                            results.append(f"FAIL {src.name}: {jr.error}")
                self.after(0, lambda: self._done(True, results))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._done(False, [str(exc)]))

        threading.Thread(target=work, daemon=True).start()

    def _run_one(self, tool: str, src: Path) -> jobs.JobResult:
        ext = src.suffix.lower()

        def go(name: str, fn):  # type: ignore[no-untyped-def]
            return jobs.run_job(name, fn, inputs=[src])

        if tool == "Convert":
            fmt = self.var_fmt.get()
            if ext in IMAGE_EXTS or fmt in ("png", "jpg", "jpeg", "webp"):
                if ext in IMAGE_EXTS:
                    return go("convert_image", lambda: ops.convert_image(src, fmt=fmt if fmt in ("png", "jpg", "webp") else "png"))
                return go("convert_video", lambda: ops.convert_video(src, fmt=fmt if fmt in ops.VIDEO_FORMATS else "mp4"))
            if ext in AUDIO_EXTS or fmt in ops.AUDIO_FORMATS:
                if ext in AUDIO_EXTS or fmt in ops.AUDIO_FORMATS:
                    f = fmt if fmt in ops.AUDIO_FORMATS else "mp3"
                    return go("convert_audio", lambda: ops.convert_audio(src, fmt=f))
            return go(
                "convert_video",
                lambda: ops.convert_video(src, fmt=fmt if fmt in ops.VIDEO_FORMATS else "mp4"),
            )

        if tool == "Compress":
            if ext in IMAGE_EXTS:
                q = int(self.var_quality.get() or 75)
                edge = int(self.var_max_edge.get() or 1920)
                return go("compress_image", lambda: ops.compress_image(src, quality=q, max_edge=edge))
            if ext in AUDIO_EXTS:
                return go(
                    "compress_audio",
                    lambda: ops.compress_audio(src, bitrate=self.var_bitrate.get() or "128k"),
                )
            return go(
                "compress_video",
                lambda: ops.compress_video(src, preset=self.var_preset.get() or "balanced"),
            )

        if tool == "Trim":
            end = self.var_end.get().strip() or None
            return go(
                "trim",
                lambda: ops.trim_media(
                    src,
                    start=self.var_start.get() or "0",
                    end=end,
                    reencode=bool(self.var_reencode.get()),
                ),
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
                return go("flip", lambda: ops.flip_image(src, horizontal=True))
            if act == "strip_exif":
                return go("strip_exif", lambda: ops.strip_exif(src))
            # to_pdf — single image becomes one-page pdf; multi handled if only one call per file
            dest = src.with_suffix(".pdf")
            return go("images_to_pdf", lambda: ops.images_to_pdf([src], dest))

        # More
        more = self.var_more.get()
        if more == "remux":
            return go("remux", lambda: ops.remux(src, fmt="mp4"))
        if more == "strip_audio":
            return go("strip_audio", lambda: ops.strip_audio(src))
        if more == "frame":
            return go(
                "frame",
                lambda: ops.grab_frame(src, time=self.var_start.get() or "0"),
            )
        if more == "rotate_video":
            return go(
                "rotate_video",
                lambda: ops.rotate_video(src, degrees=int(self.var_degrees.get() or 90)),
            )
        return jobs.JobResult(op=more, paths=[], ok=False, duration_s=0, error="Unknown action")

    def _done(self, ok: bool, lines: list[str]) -> None:
        self._busy = False
        self._set_status("Done" if ok else "Finished with errors")
        for line in lines:
            self._log(line)

    def _maybe_first_run(self) -> None:
        data = app_prefs.load_prefs()
        if data.get("first_run_completed"):
            return
        win = ctk.CTkToplevel(self)
        win.title(f"Welcome to {__app_name__}")
        win.geometry("480x320")
        win.transient(self)
        ctk.CTkLabel(
            win,
            text=f"{__app_name__} works fully offline.\nNothing is uploaded.\n\n"
            "Optional anonymous diagnostics (default off) only build a local\n"
            "report you can copy if something goes wrong.",
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
        win.geometry("400x200")
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
        win.geometry("520x400")
        ctk.CTkLabel(
            win,
            text=f"{__app_name__} {__version__}\nMIT · free forever · offline only\n"
            "Requires ffmpeg for video/audio.",
            justify="left",
        ).pack(padx=12, pady=12)

        def copy_diag() -> None:
            if not data.get("diagnostics_enabled"):
                messagebox.showinfo(
                    __app_name__,
                    "Enable anonymous diagnostics in Settings first.",
                )
                return
            text = build_report()
            self.clipboard_clear()
            self.clipboard_append(text)
            messagebox.showinfo(__app_name__, "Diagnostics copied to clipboard.")

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
            path.write_text(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                encoding="utf-8",
            )
        except OSError:
            path = Path.cwd() / "clipwork_crash.log"
            path.write_text(str(exc), encoding="utf-8")
        try:
            messagebox.showerror(__app_name__, f"Failed to start:\n{exc}\n\nLog:\n{path}")
        except Exception:
            print(exc, file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
