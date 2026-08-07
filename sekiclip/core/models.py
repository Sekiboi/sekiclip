"""Shared typed models — Look + export job (preview and export share Look)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Look:
    """Canonical edit look for preview and export."""

    edit_action: str = "render_cut"
    video_quality: str = "1080p"
    audio_quality: str = "256k"
    fade_video: bool = True
    fade_audio: bool = True
    v_fade_in: str = "0.5"
    v_fade_out: str = "0.5"
    a_fade_in: str = "0.5"
    a_fade_out: str = "0.5"
    mute: bool = False
    volume: str = "1.0"
    speed: str = "1.0"
    use_crop: bool = False
    use_logo: bool = False
    use_subs: bool = False
    logo_pos: str = "top-right"
    logo_scale: str = "0.15"
    crop_margin: str = "40"
    crop_rect: tuple[float, float, float, float] = (0.1, 0.1, 0.9, 0.9)
    srt_path: Path | str | None = None
    logo_path: Path | str | None = None
    logo_ghost: bool = False
    gif_fmt: str = "gif"
    max_mb: str = "25"
    # Film-making (roadmap)
    color_look: str = "none"
    color_strength: str = "1.0"
    vfx: str = "none"
    vfx_strength: str = "1.0"
    title: str = ""
    title_sub: str = ""
    title_position: str = "center"
    end_card: str = ""
    end_card_hold: str = "3.0"
    music_path: Path | str | None = None
    music_volume: str = "0.35"
    music_fade_in: str = "1.0"
    music_fade_out: str = "1.5"
    music_duck: bool = False
    transition: str = "crossfade"
    transition_dur: str = "0.6"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.srt_path is not None:
            d["srt_path"] = str(self.srt_path)
        if self.logo_path is not None:
            d["logo_path"] = str(self.logo_path)
        if self.music_path is not None:
            d["music_path"] = str(self.music_path)
        d["crop_rect"] = tuple(self.crop_rect)
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Look:
        if not raw:
            return cls()
        cr = raw.get("crop_rect") or (0.1, 0.1, 0.9, 0.9)
        try:
            crop_rect = (float(cr[0]), float(cr[1]), float(cr[2]), float(cr[3]))
        except (TypeError, ValueError, IndexError):
            crop_rect = (0.1, 0.1, 0.9, 0.9)
        srt = raw.get("srt_path") or None
        logo = raw.get("logo_path") or None
        music = raw.get("music_path") or None
        return cls(
            edit_action=str(raw.get("edit_action") or "render_cut"),
            video_quality=str(raw.get("video_quality") or "1080p"),
            audio_quality=str(raw.get("audio_quality") or "256k"),
            fade_video=bool(raw.get("fade_video", True)),
            fade_audio=bool(raw.get("fade_audio", True)),
            v_fade_in=str(raw.get("v_fade_in") or "0.5"),
            v_fade_out=str(raw.get("v_fade_out") or "0.5"),
            a_fade_in=str(raw.get("a_fade_in") or "0.5"),
            a_fade_out=str(raw.get("a_fade_out") or "0.5"),
            mute=bool(raw.get("mute")),
            volume=str(raw.get("volume") or "1.0"),
            speed=str(raw.get("speed") or "1.0"),
            use_crop=bool(raw.get("use_crop")),
            use_logo=bool(raw.get("use_logo")),
            use_subs=bool(raw.get("use_subs")),
            logo_pos=str(raw.get("logo_pos") or "top-right"),
            logo_scale=str(raw.get("logo_scale") or "0.15"),
            crop_margin=str(raw.get("crop_margin") or "40"),
            crop_rect=crop_rect,
            srt_path=srt if srt else None,
            logo_path=logo if logo else None,
            logo_ghost=bool(raw.get("logo_ghost")),
            gif_fmt=str(raw.get("gif_fmt") or "gif"),
            max_mb=str(raw.get("max_mb") or "25"),
            color_look=str(raw.get("color_look") or "none"),
            color_strength=str(raw.get("color_strength") or "1.0"),
            vfx=str(raw.get("vfx") or "none"),
            vfx_strength=str(raw.get("vfx_strength") or "1.0"),
            title=str(raw.get("title") or ""),
            title_sub=str(raw.get("title_sub") or ""),
            title_position=str(raw.get("title_position") or "center"),
            end_card=str(raw.get("end_card") or ""),
            end_card_hold=str(raw.get("end_card_hold") or "3.0"),
            music_path=music if music else None,
            music_volume=str(raw.get("music_volume") or "0.35"),
            music_fade_in=str(raw.get("music_fade_in") or "1.0"),
            music_fade_out=str(raw.get("music_fade_out") or "1.5"),
            music_duck=bool(raw.get("music_duck")),
            transition=str(raw.get("transition") or "crossfade"),
            transition_dur=str(raw.get("transition_dur") or "0.6"),
        )


@dataclass
class ExportJob:
    """One export request (for logging / future queue)."""

    tool: str
    src: Path | None
    dest: Path | None
    batch: bool = False
    look: Look = field(default_factory=Look)
    prefer_gpu: bool = False
    reencode: bool = True

    def summary(self) -> str:
        src_n = self.src.name if self.src else "?"
        dest_n = self.dest.name if self.dest else ("batch" if self.batch else "?")
        return f"{self.tool}: {src_n} → {dest_n}"


# Structured codes for local job log (not shown in the main UI)
class JobErrorCode:
    CANCELLED = "E_CANCELLED"
    FFMPEG_MISSING = "E_FFMPEG_MISSING"
    FFMPEG_FAILED = "E_FFMPEG_FAILED"
    IO_ERROR = "E_IO"
    INVALID_INPUT = "E_INPUT"
    ENCODE_FAILED = "E_ENCODE"
    UNKNOWN = "E_UNKNOWN"


def classify_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("CancelledError",) or "cancel" in msg:
        return JobErrorCode.CANCELLED
    if "ffmpeg not found" in msg or "ffprobe not found" in msg:
        return JobErrorCode.FFMPEG_MISSING
    if name in ("FileNotFoundError", "PermissionError", "OSError"):
        return JobErrorCode.IO_ERROR
    if "ffmpeg failed" in msg or "encode" in msg:
        return JobErrorCode.ENCODE_FAILED
    if name in ("ValueError", "RuntimeError") and ("no " in msg or "invalid" in msg):
        return JobErrorCode.INVALID_INPUT
    if "ffmpeg" in msg:
        return JobErrorCode.FFMPEG_FAILED
    return JobErrorCode.UNKNOWN
