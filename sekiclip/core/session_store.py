"""Recent files + session JSON (offline only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sekiclip.core import prefs as app_prefs

RECENT_MAX = 12
SESSION_VERSION = 1

# UI label → internal action key
EDIT_ACTION_CHOICES: list[tuple[str, str]] = [
    ("Full cut", "render_cut"),
    ("Fade only", "fade"),
    ("Crop", "crop"),
    ("Volume", "volume"),
    ("Speed", "speed"),
    ("GIF / WebP", "gif"),
    ("Flip", "flip"),
    ("Target size", "target_size"),
    ("Burn subs", "burn_subs"),
    ("Logo", "logo"),
]
EDIT_LABEL_TO_KEY = {lab: key for lab, key in EDIT_ACTION_CHOICES}
EDIT_KEY_TO_LABEL = {key: lab for lab, key in EDIT_ACTION_CHOICES}
EDIT_ACTION_LABELS = [lab for lab, _ in EDIT_ACTION_CHOICES]

# Export quality presets (video key, audio key) — all free
EXPORT_PRESETS: list[tuple[str, str, str]] = [
    ("Share 1080p", "1080p", "192k"),
    ("Share 720p", "720p", "128k"),
    ("Archive", "original", "320k"),
    ("Small file", "480p", "128k"),
    ("High audio", "original", "256k"),
]
EXPORT_PRESET_LABELS = [p[0] for p in EXPORT_PRESETS]
EXPORT_PRESET_MAP = {lab: (vk, ak) for lab, vk, ak in EXPORT_PRESETS}


def get_recent_files() -> list[Path]:
    data = app_prefs.load_prefs()
    raw = data.get("recent_files") or []
    out: list[Path] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            p = Path(str(item))
            if p.is_file():
                out.append(p)
        except OSError:
            continue
    return out[:RECENT_MAX]


def push_recent_file(path: Path | str) -> None:
    path = Path(path)
    if not path.is_file():
        return
    data = app_prefs.load_prefs()
    key = str(path.resolve())
    prev = [str(x) for x in (data.get("recent_files") or []) if str(x)]
    prev = [p for p in prev if p.lower() != key.lower()]
    prev.insert(0, key)
    data["recent_files"] = prev[:RECENT_MAX]
    app_prefs.save_prefs(data)


def build_session_dict(
    *,
    media_path: Path | str | None,
    in_point: float,
    out_point: float | None,
    position: float,
    look: dict[str, Any],
    tool: str,
) -> dict[str, Any]:
    """Serializable session (paths as strings)."""
    srt = look.get("srt_path")
    logo = look.get("logo_path")
    music = look.get("music_path")
    return {
        "version": SESSION_VERSION,
        "media": str(media_path) if media_path else "",
        "in_point": float(in_point),
        "out_point": float(out_point) if out_point is not None else None,
        "position": float(position),
        "tool": str(tool or "Edit"),
        "look": {
            "edit_action": look.get("edit_action") or "render_cut",
            "video_quality": look.get("video_quality") or "1080p",
            "audio_quality": look.get("audio_quality") or "256k",
            "fade_video": bool(look.get("fade_video", True)),
            "fade_audio": bool(look.get("fade_audio", True)),
            "v_fade_in": str(look.get("v_fade_in") or "0.5"),
            "v_fade_out": str(look.get("v_fade_out") or "0.5"),
            "a_fade_in": str(look.get("a_fade_in") or "0.5"),
            "a_fade_out": str(look.get("a_fade_out") or "0.5"),
            "mute": bool(look.get("mute")),
            "volume": str(look.get("volume") or "1.0"),
            "speed": str(look.get("speed") or "1.0"),
            "use_crop": bool(look.get("use_crop")),
            "use_logo": bool(look.get("use_logo")),
            "use_subs": bool(look.get("use_subs")),
            "logo_pos": str(look.get("logo_pos") or "top-right"),
            "logo_scale": str(look.get("logo_scale") or "0.15"),
            "crop_margin": str(look.get("crop_margin") or "40"),
            "crop_rect": list(look.get("crop_rect") or (0.1, 0.1, 0.9, 0.9)),
            "gif_fmt": str(look.get("gif_fmt") or "gif"),
            "max_mb": str(look.get("max_mb") or "25"),
            "srt_path": str(srt) if srt else "",
            "logo_path": str(logo) if logo else "",
            "color_look": str(look.get("color_look") or "none"),
            "color_strength": str(look.get("color_strength") or "1.0"),
            "vfx": str(look.get("vfx") or "none"),
            "vfx_strength": str(look.get("vfx_strength") or "1.0"),
            "title": str(look.get("title") or ""),
            "title_sub": str(look.get("title_sub") or ""),
            "title_position": str(look.get("title_position") or "center"),
            "end_card": str(look.get("end_card") or ""),
            "end_card_hold": str(look.get("end_card_hold") or "3.0"),
            "music_path": str(music) if music else "",
            "music_volume": str(look.get("music_volume") or "0.35"),
            "music_fade_in": str(look.get("music_fade_in") or "1.0"),
            "music_fade_out": str(look.get("music_fade_out") or "1.5"),
            "music_duck": bool(look.get("music_duck")),
            "transition": str(look.get("transition") or "crossfade"),
            "transition_dur": str(look.get("transition_dur") or "0.6"),
        },
    }


def save_session_file(path: Path | str, payload: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_session_file(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid session file")
    return raw
