"""Export quality labels and maps (shared by Trim/Edit UI)."""

from __future__ import annotations

VIDEO_QUALITY_KEYS = ("original", "4k", "1080p", "720p", "480p")
VIDEO_QUALITY_LABELS: dict[str, str] = {
    "original": "Original",
    "4k": "4K (2160p)",
    "1080p": "1080p — recommended",
    "720p": "720p",
    "480p": "480p",
}
# max_w None = keep source size. scale never upscales (min(max_w, iw)).
VIDEO_QUALITY_PARAMS: dict[str, tuple[int | None, int, str]] = {
    "original": (None, 16, "slow"),
    "4k": (3840, 17, "slow"),
    "1080p": (1920, 18, "medium"),
    "720p": (1280, 20, "medium"),
    "480p": (854, 23, "veryfast"),
}
VIDEO_QUALITY_MENU = [VIDEO_QUALITY_LABELS[k] for k in VIDEO_QUALITY_KEYS]
VIDEO_QUALITY_DEFAULT_KEY = "1080p"
VIDEO_QUALITY_DEFAULT_LABEL = VIDEO_QUALITY_LABELS[VIDEO_QUALITY_DEFAULT_KEY]

AUDIO_QUALITY_KEYS = ("320k", "256k", "192k", "128k")
AUDIO_QUALITY_LABELS: dict[str, str] = {
    "320k": "320 kbps",
    "256k": "256 kbps — recommended",
    "192k": "192 kbps",
    "128k": "128 kbps",
}
AUDIO_QUALITY_BITRATE: dict[str, str] = {
    "320k": "320k",
    "256k": "256k",
    "192k": "192k",
    "128k": "128k",
}
AUDIO_QUALITY_MENU = [AUDIO_QUALITY_LABELS[k] for k in AUDIO_QUALITY_KEYS]
AUDIO_QUALITY_DEFAULT_KEY = "256k"
AUDIO_QUALITY_DEFAULT_LABEL = AUDIO_QUALITY_LABELS[AUDIO_QUALITY_DEFAULT_KEY]

EXPORT_QUALITY_HELP = "Caps size (no upscale). Default: 1080p + 256 kbps."


def normalize_video_quality(value: str | None) -> str:
    """Map UI label / alias / raw key → video quality key."""
    raw = (value or "").strip().lower()
    if not raw:
        return VIDEO_QUALITY_DEFAULT_KEY
    if raw in VIDEO_QUALITY_PARAMS:
        return raw
    if raw in ("maximum", "max", "archive", "high"):
        return "original" if raw in ("maximum", "max", "archive") else "1080p"
    if raw in ("balanced", "medium", "normal", "everyday"):
        return "720p"
    if raw in ("fast", "draft", "quick", "speed"):
        return "480p"
    for key, label in VIDEO_QUALITY_LABELS.items():
        if raw == label.lower() or raw.startswith(key):
            return key
    if "original" in raw or "source" in raw:
        return "original"
    if "4k" in raw or "2160" in raw or "uhd" in raw:
        return "4k"
    if "1080" in raw or "full hd" in raw or "fhd" in raw:
        return "1080p"
    if "720" in raw or raw == "hd":
        return "720p"
    if "480" in raw or "sd" in raw:
        return "480p"
    if "recommended" in raw:
        return VIDEO_QUALITY_DEFAULT_KEY
    return VIDEO_QUALITY_DEFAULT_KEY


def normalize_audio_quality(value: str | None) -> str:
    """Map UI label / alias / raw key → audio quality key."""
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        return AUDIO_QUALITY_DEFAULT_KEY
    if raw in AUDIO_QUALITY_BITRATE:
        return raw
    for key in AUDIO_QUALITY_KEYS:
        digits = key.replace("k", "")
        if raw.startswith(digits) or key in raw:
            return key
    if raw in ("maximum", "max", "archive"):
        return "320k"
    if raw in ("high",):
        return "256k"
    if raw in ("balanced", "medium", "normal", "everyday"):
        return "192k"
    if raw in ("fast", "draft", "quick"):
        return "128k"
    if "recommended" in raw:
        return AUDIO_QUALITY_DEFAULT_KEY
    return AUDIO_QUALITY_DEFAULT_KEY


def video_scale_filter(max_w: int | None) -> str | None:
    """ffmpeg scale expr: cap width, keep aspect, never upscale, even height."""
    if max_w is None or max_w <= 0:
        return None
    return f"min({int(max_w)}\\,iw):-2"
