"""Export queue, path prompts, and ffmpeg job runners."""

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

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}
IMAGE_EXTS = ops.IMAGE_EXTS


class ExportMixin:
    def _export_again(self) -> None:
        if self._busy:
            return
        self._run()

    def _pack_export_quality_ui(
        self, parent: ctk.CTkFrame, *, when_reencoding: bool = False
    ) -> None:
        """Shared video (1080p…) + audio (kbps) quality controls for Trim and Edit."""
        title = "Export quality" if not when_reencoding else "Quality (re-encode)"
        ctk.CTkLabel(parent, text=title).pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(parent, text="Video").pack(anchor="w", pady=(2, 0))
        ctk.CTkOptionMenu(
            parent,
            variable=self.var_video_quality,
            values=list(VIDEO_QUALITY_MENU),
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(parent, text="Audio").pack(anchor="w", pady=(4, 0))
        ctk.CTkOptionMenu(
            parent,
            variable=self.var_audio_quality,
            values=list(AUDIO_QUALITY_MENU),
        ).pack(fill="x", pady=2)
        ctk.CTkLabel(
            parent,
            text=EXPORT_QUALITY_HELP,
            wraplength=260,
            justify="left",
            text_color=("gray40", "gray60"),
            font=ctk.CTkFont(size=11),
        ).pack(anchor="w", pady=(0, 4))

    def _video_quality_key(self) -> str:
        return _normalize_video_quality(self.var_video_quality.get())

    def _audio_quality_key(self) -> str:
        return _normalize_audio_quality(self.var_audio_quality.get())

    def _cut_quality_params(self) -> tuple[int, str, str, str | None]:
        """Map UI export quality → (crf, x264 preset, audio_bitrate, scale).

        Video uses familiar tiers (Original / 4K / 1080p / 720p / 480p).
        Audio uses kbps. Scale never upscales smaller sources.
        """
        vkey = self._video_quality_key()
        akey = self._audio_quality_key()
        max_w, crf, preset = VIDEO_QUALITY_PARAMS.get(
            vkey, VIDEO_QUALITY_PARAMS[VIDEO_QUALITY_DEFAULT_KEY]
        )
        audio_br = AUDIO_QUALITY_BITRATE.get(
            akey, AUDIO_QUALITY_BITRATE[AUDIO_QUALITY_DEFAULT_KEY]
        )
        scale = _video_scale_filter(max_w)
        return crf, preset, audio_br, scale

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

    def _film_kwargs(self, look: dict[str, Any] | None = None) -> dict[str, Any]:
        """Film-making kwargs for render_cut (color, VFX, titles, music, end card)."""
        a = look if look is not None else self._live_settings()

        def _f(key: str, default: float) -> float:
            try:
                return float(a.get(key) if a.get(key) not in (None, "") else default)
            except (TypeError, ValueError):
                return default

        music = a.get("music_path")
        music_path = Path(str(music)) if music else None
        if music_path and not music_path.is_file():
            music_path = None
        return {
            "color_look": str(a.get("color_look") or "none"),
            "color_strength": max(0.0, min(1.0, _f("color_strength", 1.0))),
            "vfx": str(a.get("vfx") or "none"),
            "vfx_strength": max(0.0, min(1.0, _f("vfx_strength", 1.0))),
            "title": str(a.get("title") or ""),
            "title_sub": str(a.get("title_sub") or ""),
            "title_position": str(a.get("title_position") or "center"),
            "end_card": str(a.get("end_card") or ""),
            "end_card_hold": max(0.5, _f("end_card_hold", 3.0)),
            "music": music_path,
            "music_volume": max(0.0, min(2.0, _f("music_volume", 0.35))),
            "music_fade_in": max(0.0, _f("music_fade_in", 1.0)),
            "music_fade_out": max(0.0, _f("music_fade_out", 1.5)),
            "music_duck": bool(a.get("music_duck")),
        }

    def _has_film_fx(self, look: dict[str, Any] | None = None) -> bool:
        a = look if look is not None else self._live_settings()
        cl = str(a.get("color_look") or "none").lower()
        vx = str(a.get("vfx") or "none").lower()
        if cl not in ("", "none") or vx not in ("", "none"):
            return True
        if str(a.get("title") or "").strip() or str(a.get("end_card") or "").strip():
            return True
        music = a.get("music_path")
        if music and Path(str(music)).is_file():
            return True
        return False

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

    def _cancel_export(self) -> None:
        request_cancel()
        self._set_status("Cancelling export…")
        self._log("Cancel requested")

    def _confirm_export_disk_space(
        self,
        *,
        sources: list[Path],
        dest: Path,
        batch: bool,
    ) -> bool:
        """Warn when free space looks tight. No artificial max input size.

        Re-encodes can need ~1–2× source size temporarily (staging + output).
        Stream-copy trims need far less; we use a conservative estimate so
        multi‑GB sources don't fail mid-write.
        """
        free = free_disk_bytes(dest)
        if free is None:
            return True
        total_src = 0
        for p in sources:
            try:
                if p is not None and Path(p).is_file():
                    total_src += int(Path(p).stat().st_size)
            except OSError:
                pass
        if total_src <= 0:
            return True
        # Staging + final for in-place, or full rewrite: budget ~2.2× sources
        need = int(total_src * (2.2 if batch else 1.6))
        # Only warn when free space is clearly short of that budget
        if free >= need:
            return True
        msg = (
            f"Low disk space on the export drive.\n\n"
            f"Free: {format_bytes(free)}\n"
            f"Sources: {format_bytes(total_src)}\n"
            f"Suggested free: {format_bytes(need)} "
            f"(re-encode / staging headroom)\n\n"
            f"Continue anyway?"
        )
        return bool(messagebox.askyesno(__app_name__, msg, icon="warning"))

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
            try:
                self.btn_export_again.configure(state="normal")
            except Exception:
                pass
        else:
            try:
                self.btn_open_folder.configure(state="disabled")
            except Exception:
                pass
            try:
                if hasattr(self, "btn_export_again"):
                    self.btn_export_again.configure(state="disabled")
            except Exception:
                pass
            # Drop path only when we are fully clearing export residue
            if not keep_open_folder:
                self._last_export_path = None
        self._set_progress(0.0, "Idle", to_log=False)
        self._set_status(status)
        self._set_queue_text(self._QUEUE_IDLE)

    # ── export ──────────────────────────────────────────────

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
                    "Sekiclip will encode safely to a temp file, then swap it in.\n"
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

        # Soft disk-space check: large re-encodes need headroom; no hard file-size cap.
        if dest is not None and not self._confirm_export_disk_space(
            sources=list(self._files) if batch else ([src] if src else []),
            dest=dest,
            batch=batch,
        ):
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
        self._set_transport_playing(False)
        label = dest.name if dest else "…"
        self._set_status(f"Exporting to {label}…")
        self._set_progress(0.02, "Preparing export…")
        self._log("—")
        self._log(f"Export started · tool={tool}" + (" · batch" if batch else ""))
        if src:
            try:
                self._log(f"  Source: {src.name} ({format_bytes(src.stat().st_size)})")
            except OSError:
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
                    return ops.normalize_audio(
                        src,
                        dest,
                        integrated_lufs=float(self.var_loud_i.get() or -16),
                        true_peak=float(self.var_loud_tp.get() or -1.5),
                    )
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
        crf, preset, audio_br, scale = self._cut_quality_params()
        logo_path = a.get("logo_path")
        srt_path = a.get("srt_path")
        use_logo = bool(a.get("use_logo") and logo_path)
        use_subs = bool(a.get("use_subs") and srt_path)
        logo_pos = str(a.get("logo_pos") or "top-right")
        logo_scale = float(a.get("logo_scale") or 0.15)
        film = self._film_kwargs(a)

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
                    audio_bitrate=audio_br,
                    scale=scale,
                    prefer_gpu=self._prefer_gpu(),
                    **film,
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
                    audio_bitrate=audio_br,
                    scale=scale,
                    prefer_gpu=self._prefer_gpu(),
                )
            if act == "flip":
                return ops.flip_video(src, dest, horizontal=True)
            if act == "target_size":
                return ops.target_size_video(src, dest, max_mb=max_mb, two_pass=True)
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
                self._set_transport_playing(False)
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
            look = self._export_look()
            vfi = look["video_fade_in"]
            vfo = look["video_fade_out"]
            afi = look["audio_fade_in"]
            afo = look["audio_fade_out"]
            film_live = self._live_settings()
            has_fx = (
                vfi > 0
                or vfo > 0
                or afi > 0
                or afo > 0
                or look.get("use_crop")
                or look.get("use_logo")
                or look.get("use_subs")
                or abs(float(look["speed"]) - 1.0) > 1e-3
                or look.get("mute")
                or abs(float(self.var_volume.get() or 1.0) - 1.0) > 1e-3
                or self._has_film_fx(film_live)
            ) and suffix.lower() not in (
                ".mp3",
                ".wav",
                ".flac",
                ".m4a",
                ".ogg",
                ".aac",
            )
            # Any Edit look requires re-encode so Trim matches the preview
            if has_fx:
                crf, preset, audio_br, scale = self._cut_quality_params()
                v_key = self._video_quality_key()
                a_key = self._audio_quality_key()
                note(
                    f"  Trim + looks (video fade {vfi:.2f}/{vfo:.2f}s · "
                    f"audio {afi:.2f}/{afo:.2f}s · speed {look['speed']:g}×)"
                )
                note(
                    f"  Cutting {format_time(start)} → {format_time(end or 0)}"
                    + (f" ({format_time(dur)})" if dur else "")
                )
                note(
                    f"  Export · video {v_key} · audio {a_key} "
                    f"({audio_br})"
                )
                prog(0.03, "Trim: encoding with looks…")
                cx = cy = 0
                cw = ch = None
                if look.get("use_crop"):
                    cx, cy, cw, ch = self._crop_pixels(src)
                logo = (
                    Path(str(look["logo_path"]))
                    if look.get("use_logo") and look.get("logo_path")
                    else None
                )
                srt = (
                    Path(str(look["srt_path"]))
                    if look.get("use_subs") and look.get("srt_path")
                    else None
                )
                try:
                    out = ops.render_cut(
                        src,
                        out_path,
                        start=start,
                        end=end if end else None,
                        crop_x=cx,
                        crop_y=cy,
                        crop_w=cw,
                        crop_h=ch,
                        speed=float(look["speed"]),
                        volume=float(self.var_volume.get() or 1.0),
                        mute=bool(look["mute"]),
                        video_fade_in=vfi,
                        video_fade_out=vfo,
                        audio_fade_in=afi,
                        audio_fade_out=afo,
                        logo=logo,
                        logo_position=str(look.get("logo_pos") or "top-right"),
                        logo_scale=float(look.get("logo_scale") or 0.15),
                        srt=srt,
                        crf=crf,
                        preset=preset,
                        audio_bitrate=audio_br,
                        scale=scale,
                        prefer_gpu=self._prefer_gpu(),
                        on_progress=prog,
                        **self._film_kwargs(film_live),
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
                crf, preset, audio_br, scale = self._cut_quality_params()
                v_key = self._video_quality_key()
                a_key = self._audio_quality_key()
                note(
                    f"  Starting encoder · video {v_key} · audio {a_key} "
                    f"({audio_br})…"
                )
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
                    if scale:
                        args += ["-vf", f"scale={scale}"]
                    args += [
                        "-c:v",
                        "libx264",
                        "-preset",
                        preset,
                        "-crf",
                        str(crf),
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        audio_br,
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
            look = self._export_look()
            act = str(look.get("edit_action") or "render_cut")
            if act in ("render_cut", "fade"):
                start = look["start"]
                end = look["end"]
                vfi = look["video_fade_in"]
                vfo = look["video_fade_out"]
                afi = look["audio_fade_in"]
                afo = look["audio_fade_out"]
                crf, preset, audio_br, scale = self._cut_quality_params()
                vol = float(self.var_volume.get() or 1.0)
                mute = bool(look["mute"])
                speed = float(look["speed"])
                cx, cy, cw, ch = (0, 0, None, None)
                logo = None
                srt = None
                if act == "render_cut":
                    if look.get("use_crop"):
                        cx, cy, cw, ch = self._crop_pixels(src)
                    if look.get("use_logo") and look.get("logo_path"):
                        logo = Path(str(look["logo_path"]))
                    if look.get("use_subs") and look.get("srt_path"):
                        srt = Path(str(look["srt_path"]))
                sel = f"{format_time(start)} → {format_time(end or 0)}"
                note(f"  One-pass {'render cut' if act == 'render_cut' else 'fade'}: {sel}")
                note(
                    f"  Fades video {vfi:.2f}/{vfo:.2f}s · audio {afi:.2f}/{afo:.2f}s"
                    f" · video {self._video_quality_key()}"
                    f" · audio {self._audio_quality_key()} ({audio_br})"
                )
                film_live = self._live_settings()
                film_kw = self._film_kwargs(film_live)
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
                    if look.get("flip_h"):
                        bits.append("flip")
                    cl = str(film_kw.get("color_look") or "none")
                    if cl not in ("", "none"):
                        bits.append(f"look {cl}")
                    vx = str(film_kw.get("vfx") or "none")
                    if vx not in ("", "none"):
                        bits.append(f"vfx {vx}")
                    if film_kw.get("title"):
                        bits.append("title")
                    if film_kw.get("end_card"):
                        bits.append("end card")
                    if film_kw.get("music"):
                        bits.append("music bed")
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
                            flip_h=bool(look.get("flip_h")),
                            speed=speed,
                            volume=vol,
                            mute=mute,
                            video_fade_in=vfi,
                            video_fade_out=vfo,
                            audio_fade_in=afi,
                            audio_fade_out=afo,
                            logo=logo,
                            logo_position=str(look.get("logo_pos") or "top-right"),
                            logo_scale=float(look.get("logo_scale") or 0.15),
                            logo_opacity=float(look.get("logo_opacity") or 0.9),
                            srt=srt,
                            crf=crf,
                            preset=preset,
                            audio_bitrate=audio_br,
                            scale=scale,
                            prefer_gpu=self._prefer_gpu(),
                            on_progress=prog,
                            **film_kw,
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
                            audio_bitrate=audio_br,
                            scale=scale,
                            prefer_gpu=self._prefer_gpu(),
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
                        src, dest, max_mb=float(a.get("max_mb") or 25), two_pass=True
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
                return go(
                    "normalize",
                    lambda: ops.normalize_audio(
                        src,
                        dest,
                        integrated_lufs=float(self.var_loud_i.get() or -16),
                        true_peak=float(self.var_loud_tp.get() or -1.5),
                    ),
                )
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
                messagebox.showinfo(
                    __app_name__,
                    f"Saved:\n{summary}\n\n"
                    "Open folder · Export again — use the buttons under progress.",
                )
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

    def _export_log(self, msg: str) -> None:
        """Thread-safe log line for export workers."""
        self.after(0, lambda m=msg: self._log(m))

    def _export_look(self) -> dict[str, Any]:
        """Canonical look for BOTH preview and export (In/Out + Edit controls)."""
        a = self._live_settings()
        vfi, vfo, afi, afo = self._fade_seconds()
        try:
            speed = max(0.25, min(4.0, float(a.get("speed") or 1.0)))
        except (TypeError, ValueError):
            speed = 1.0
        try:
            volume = max(0.0, float(a.get("volume") or 1.0))
        except (TypeError, ValueError):
            volume = 1.0
        mute = bool(a.get("mute"))
        act = str(a.get("edit_action") or "render_cut")
        return {
            "start": float(self._session.in_point),
            "end": float(self._session.out_or_end or 0) or None,
            "video_fade_in": vfi,
            "video_fade_out": vfo,
            "audio_fade_in": afi,
            "audio_fade_out": afo,
            "speed": speed,
            "volume": 0.0 if mute else volume,
            "mute": mute,
            "use_crop": bool(a.get("use_crop")) or self._crop_mode,
            "crop_rect": tuple(self._crop_rect),
            "use_logo": bool(a.get("use_logo")) and bool(a.get("logo_path")),
            "logo_path": a.get("logo_path"),
            "logo_pos": str(a.get("logo_pos") or "top-right"),
            "logo_scale": float(a.get("logo_scale") or 0.15),
            "logo_opacity": 0.9,
            "use_subs": bool(a.get("use_subs")) and bool(a.get("srt_path")),
            "srt_path": a.get("srt_path"),
            "flip_h": act == "flip",
            "edit_action": act,
            "video_quality": _normalize_video_quality(
                str(a.get("video_quality") or a.get("cut_quality") or VIDEO_QUALITY_DEFAULT_KEY)
            ),
            "audio_quality": _normalize_audio_quality(
                str(a.get("audio_quality") or a.get("cut_quality") or AUDIO_QUALITY_DEFAULT_KEY)
            ),
            "color_look": str(a.get("color_look") or "none"),
            "color_strength": str(a.get("color_strength") or "1.0"),
            "vfx": str(a.get("vfx") or "none"),
            "vfx_strength": str(a.get("vfx_strength") or "1.0"),
            "title": str(a.get("title") or ""),
            "title_sub": str(a.get("title_sub") or ""),
            "title_position": str(a.get("title_position") or "center"),
            "end_card": str(a.get("end_card") or ""),
            "end_card_hold": str(a.get("end_card_hold") or "3.0"),
            "music_path": a.get("music_path"),
            "music_volume": str(a.get("music_volume") or "0.35"),
            "music_fade_in": str(a.get("music_fade_in") or "1.0"),
            "music_fade_out": str(a.get("music_fade_out") or "1.5"),
            "music_duck": bool(a.get("music_duck")),
            "transition": str(a.get("transition") or "crossfade"),
            "transition_dur": str(a.get("transition_dur") or "0.6"),
        }

    def _apply_export_preset(self, choice: str | None = None) -> None:
        lab = (choice or self.var_export_preset.get() or "").strip()
        pair = session_store.EXPORT_PRESET_MAP.get(lab)
        if not pair:
            return
        vkey, akey = pair
        self.var_video_quality.set(
            VIDEO_QUALITY_LABELS.get(vkey, VIDEO_QUALITY_DEFAULT_LABEL)
        )
        self.var_audio_quality.set(
            AUDIO_QUALITY_LABELS.get(akey, AUDIO_QUALITY_DEFAULT_LABEL)
        )
        self._set_status(f"Quality: {lab}")
        self._log(f"Quick quality → {lab} ({vkey} / {akey})")

    def _prefer_gpu(self) -> bool:
        return bool(self.var_prefer_gpu.get()) if hasattr(self, "var_prefer_gpu") else False

