"""Tool ids and labels — single registry for tabs and runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Main tool tabs (order = UI order)
TOOL_NAMES: tuple[str, ...] = (
    "Convert",
    "Compress",
    "Trim",
    "Edit",
    "Audio",
    "Image",
    "More",
)

# Optional developer experiments only. Set via env SEKICLIP_FLAGS=a,b
_DEV_FLAGS: set[str] = set()


def load_dev_flags_from_env() -> None:
    import os

    raw = os.environ.get("SEKICLIP_FLAGS") or ""
    _DEV_FLAGS.clear()
    for part in raw.split(","):
        p = part.strip()
        if p:
            _DEV_FLAGS.add(p)


def dev_flag(name: str) -> bool:
    """True only when set via SEKICLIP_FLAGS (developer experiments)."""
    return name in _DEV_FLAGS


@dataclass(frozen=True)
class ToolSpec:
    id: str
    label: str
    needs_ffmpeg: bool = True
    batchable: bool = True


TOOLS: dict[str, ToolSpec] = {
    "Convert": ToolSpec("Convert", "Convert", needs_ffmpeg=False, batchable=True),
    "Compress": ToolSpec("Compress", "Compress", needs_ffmpeg=False, batchable=True),
    "Trim": ToolSpec("Trim", "Trim", needs_ffmpeg=True, batchable=False),
    "Edit": ToolSpec("Edit", "Edit", needs_ffmpeg=True, batchable=True),
    "Audio": ToolSpec("Audio", "Audio", needs_ffmpeg=True, batchable=True),
    "Image": ToolSpec("Image", "Image", needs_ffmpeg=False, batchable=True),
    "More": ToolSpec("More", "More", needs_ffmpeg=True, batchable=False),
}


def tool_ids() -> list[str]:
    return list(TOOL_NAMES)


def get_tool(tool_id: str) -> ToolSpec | None:
    return TOOLS.get(tool_id)
