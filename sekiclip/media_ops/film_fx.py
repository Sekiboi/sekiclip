"""Film-making helpers: color looks, light VFX, titles, music bed, transitions.

All offline ffmpeg filters. Export path is authoritative.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Look keys for UI / CLI
COLOR_LOOKS: dict[str, str] = {
    "none": "",
    "warm": "eq=saturation=1.08:gamma_r=1.06:gamma_b=0.94",
    "cool": "eq=saturation=1.05:gamma_b=1.08:gamma_r=0.95",
    "documentary": "eq=contrast=1.08:saturation=0.92:brightness=0.02",
    "night": "eq=brightness=-0.06:contrast=1.12:saturation=0.85:gamma_b=1.1",
    "soft_film": "eq=contrast=0.94:saturation=0.95:brightness=0.03,gblur=sigma=0.4",
    "high_contrast": "eq=contrast=1.22:saturation=1.05",
    "bw": "hue=s=0",
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
}

VFX_PRESETS: dict[str, str] = {
    "none": "",
    "vignette": "vignette=PI/4",
    "grain": "noise=alls=12:allf=t",
    "soft": "gblur=sigma=1.2",
    "sharpen": "unsharp=5:5:0.8:5:5:0.0",
    "bloom": "gblur=sigma=2,eq=brightness=0.04",
    "flash": "",  # applied as timed fade; see flash_at
}

TRANSITIONS: dict[str, str] = {
    "cut": "cut",
    "crossfade": "fade",
    "dissolve": "fade",
    "dip_black": "fadeblack",
    "dip_white": "fadewhite",
    "wipe_left": "wipeleft",
    "wipe_right": "wiperight",
    "slide_left": "slideleft",
    "slide_right": "slideright",
    "smooth_left": "smoothleft",
    "smooth_right": "smoothright",
    "circle_open": "circleopen",
    "circle_close": "circleclose",
    "pixelize": "pixelize",
    "distance": "distance",
    "hblur": "hblur",
    "fadegrays": "fadegrays",
    "squeezeh": "squeezeh",
    "squeezev": "squeezev",
}

PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "youtube_1080": {"scale": "1920:1080", "crf": 20, "audio_bitrate": "192k", "label": "YouTube 1080p"},
    "vertical_1080": {"scale": "1080:1920", "crf": 20, "audio_bitrate": "192k", "label": "Vertical 9:16"},
    "square_1080": {"scale": "1080:1080", "crf": 21, "audio_bitrate": "192k", "label": "Square"},
    "share_chat": {"scale": "1280:720", "crf": 26, "audio_bitrate": "128k", "label": "Share / chat"},
    "archive": {"scale": None, "crf": 18, "audio_bitrate": "256k", "label": "Archive quality"},
}


def escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext."""
    t = (
        str(text)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "%%")
        .replace("\n", "\\n")
    )
    return t


def color_look_filter(look: str, strength: float = 1.0) -> list[str]:
    """Return video filter fragments for a named color look."""
    key = (look or "none").strip().lower().replace(" ", "_").replace("-", "_")
    base = COLOR_LOOKS.get(key, "")
    if not base:
        return []
    s = max(0.0, min(1.0, float(strength)))
    if s < 0.02:
        return []
    # Strength: blend toward original via colorchannelmixer when partial
    if s >= 0.98:
        return [base]
    # Approximate strength with eq saturation/contrast scaled for simple looks
    if key == "bw":
        return [f"hue=s={1.0 - s:.3f}"]
    if key == "warm":
        return [f"eq=saturation={1.0 + 0.08 * s:.3f}:gamma_r={1.0 + 0.06 * s:.3f}:gamma_b={1.0 - 0.06 * s:.3f}"]
    if key == "cool":
        return [f"eq=saturation={1.0 + 0.05 * s:.3f}:gamma_b={1.0 + 0.08 * s:.3f}:gamma_r={1.0 - 0.05 * s:.3f}"]
    if key == "documentary":
        return [f"eq=contrast={1.0 + 0.08 * s:.3f}:saturation={1.0 - 0.08 * s:.3f}:brightness={0.02 * s:.3f}"]
    if key == "night":
        return [
            f"eq=brightness={-0.06 * s:.3f}:contrast={1.0 + 0.12 * s:.3f}:"
            f"saturation={1.0 - 0.15 * s:.3f}:gamma_b={1.0 + 0.1 * s:.3f}"
        ]
    if key == "soft_film":
        return [f"eq=contrast={1.0 - 0.06 * s:.3f}:saturation={1.0 - 0.05 * s:.3f}:brightness={0.03 * s:.3f}"]
    if key == "high_contrast":
        return [f"eq=contrast={1.0 + 0.22 * s:.3f}:saturation={1.0 + 0.05 * s:.3f}"]
    if key == "sepia":
        return [base] if s > 0.5 else []
    return [base]


def vfx_filter(vfx: str, strength: float = 1.0) -> list[str]:
    key = (vfx or "none").strip().lower().replace(" ", "_").replace("-", "_")
    s = max(0.0, min(1.0, float(strength)))
    if s < 0.02 or key in ("", "none"):
        return []
    if key == "vignette":
        return [f"vignette=PI/{max(3.0, 5.0 - 2.0 * s):.2f}"]
    if key == "grain":
        return [f"noise=alls={int(6 + 18 * s)}:allf=t"]
    if key == "soft":
        return [f"gblur=sigma={0.4 + 1.6 * s:.2f}"]
    if key == "sharpen":
        return [f"unsharp=5:5:{0.3 + 0.9 * s:.2f}:5:5:0.0"]
    if key == "bloom":
        return [f"gblur=sigma={1.0 + 2.0 * s:.2f}", f"eq=brightness={0.02 + 0.05 * s:.3f}"]
    base = VFX_PRESETS.get(key, "")
    return [base] if base else []


def title_filters(
    title: str,
    *,
    subtitle: str = "",
    position: str = "center",
    fontsize: int = 48,
    box: bool = True,
) -> list[str]:
    """drawtext filters for title (+ optional subtitle)."""
    parts: list[str] = []
    title = (title or "").strip()
    subtitle = (subtitle or "").strip()
    if not title and not subtitle:
        return parts
    pos = (position or "center").lower().replace("_", "-")
    if pos in ("lower-third", "lower", "lt"):
        y_main = "h*0.78"
        y_sub = "h*0.86"
        x = "(w-text_w)/2"
    elif pos in ("top", "top-center", "tc"):
        y_main = "h*0.10"
        y_sub = "h*0.18"
        x = "(w-text_w)/2"
    else:
        y_main = "(h-text_h)/2"
        y_sub = "(h-text_h)/2+th+12"
        x = "(w-text_w)/2"
    box_bit = ":box=1:boxcolor=black@0.45:boxborderw=12" if box else ""
    if title:
        t = escape_drawtext(title)
        parts.append(
            f"drawtext=text='{t}':fontsize={int(fontsize)}:fontcolor=white:"
            f"x={x}:y={y_main}{box_bit}"
        )
    if subtitle:
        t2 = escape_drawtext(subtitle)
        parts.append(
            f"drawtext=text='{t2}':fontsize={max(18, int(fontsize * 0.55))}:"
            f"fontcolor=white@0.92:x={x}:y={y_sub}{box_bit}"
        )
    return parts


def end_card_filter(
    text: str,
    *,
    hold: float = 3.0,
    out_dur: float = 0.0,
) -> list[str]:
    """Burn a simple end card text in the last ``hold`` seconds of the cut."""
    text = (text or "").strip()
    if not text or out_dur <= 0.5:
        return []
    hold = max(0.5, min(float(hold), out_dur * 0.5))
    start = max(0.0, out_dur - hold)
    t = escape_drawtext(text)
    # Dim plate + text
    return [
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.55:t=fill:enable='gte(t\\,{start:.3f})'",
        f"drawtext=text='{t}':fontsize=42:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
        f"box=1:boxcolor=black@0.35:boxborderw=16:enable='gte(t\\,{start:.3f})'",
    ]


def transition_name(key: str) -> str:
    k = (key or "cut").strip().lower().replace(" ", "_").replace("-", "_")
    return TRANSITIONS.get(k, "fade" if k not in ("cut", "none", "hard") else "cut")
