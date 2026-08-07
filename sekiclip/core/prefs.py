"""Local user prefs (offline only — never uploaded)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _portable_root() -> Path | None:
    """If portable mode: folder next to the executable / project with prefs."""
    # Env wins
    env = os.environ.get("SEKICLIP_PORTABLE") or os.environ.get("SEKICLIP_DATA")
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            return None
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
    else:
        # sekiclip/core/prefs.py → project root
        exe_dir = Path(__file__).resolve().parent.parent.parent
    # Marker file enables portable prefs beside the app
    if (exe_dir / "sekiclip_portable.txt").is_file() or (exe_dir / "portable").is_file():
        data = exe_dir / "data"
        try:
            data.mkdir(parents=True, exist_ok=True)
            return data
        except OSError:
            return exe_dir
    return None


def user_data_dir() -> Path:
    """Writable app data (installed apps must not write under Program Files).

    Portable mode: set SEKICLIP_PORTABLE=path, or place ``sekiclip_portable.txt``
    next to the exe / project root (uses ``./data``).
    """
    portable = _portable_root()
    if portable is not None:
        return portable
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            base = root / "Sekiclip"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Sekiclip"
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            base = Path(xdg) / "Sekiclip" if xdg else Path.home() / ".local" / "share" / "Sekiclip"
    else:
        # Dev: store prefs at project root (sekiclip/core/prefs.py → repo root)
        base = Path(__file__).resolve().parent.parent.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def is_portable_mode() -> bool:
    return _portable_root() is not None


def prefs_path() -> Path:
    return user_data_dir() / "sekiclip_prefs.json"


# Defaults for layout / window (desktop media-tool UX)
DEFAULT_GEOMETRY = "1280x860"
DEFAULT_APPEARANCE = "System"  # System | Light | Dark
DEFAULT_LEFT_W = 220
DEFAULT_RIGHT_W = 300
DEFAULT_LOG_H = 110


def load_prefs() -> dict[str, Any]:
    data: dict[str, Any] = {
        "diagnostics_enabled": False,
        "first_run_completed": False,
        "show_tips_next_start": False,
        "update_check_enabled": False,
        "update_check_url": "",
        "last_output_dir": "",
        "recent_files": [],
        "geometry": DEFAULT_GEOMETRY,
        "zoomed": False,
        "appearance_mode": DEFAULT_APPEARANCE,
        "remember_window": True,
        "left_pane_w": DEFAULT_LEFT_W,
        "right_pane_w": DEFAULT_RIGHT_W,
        "log_pane_h": DEFAULT_LOG_H,
    }
    path = prefs_path()
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    data["diagnostics_enabled"] = bool(data.get("diagnostics_enabled", False))
    data["first_run_completed"] = bool(data.get("first_run_completed", False))
    data["show_tips_next_start"] = bool(data.get("show_tips_next_start", False))
    data["update_check_enabled"] = bool(data.get("update_check_enabled", False))
    data["update_check_url"] = str(data.get("update_check_url") or "")
    data["last_output_dir"] = str(data.get("last_output_dir") or "")
    recent = data.get("recent_files") or []
    if not isinstance(recent, list):
        recent = []
    data["recent_files"] = [str(x) for x in recent if str(x)][:20]
    data["geometry"] = str(data.get("geometry") or DEFAULT_GEOMETRY)
    data["zoomed"] = bool(data.get("zoomed", False))
    mode = str(data.get("appearance_mode") or DEFAULT_APPEARANCE).title()
    if mode not in ("System", "Light", "Dark"):
        mode = DEFAULT_APPEARANCE
    data["appearance_mode"] = mode
    data["remember_window"] = bool(data.get("remember_window", True))
    try:
        data["left_pane_w"] = max(140, min(480, int(data.get("left_pane_w") or DEFAULT_LEFT_W)))
    except (TypeError, ValueError):
        data["left_pane_w"] = DEFAULT_LEFT_W
    try:
        data["right_pane_w"] = max(220, min(560, int(data.get("right_pane_w") or DEFAULT_RIGHT_W)))
    except (TypeError, ValueError):
        data["right_pane_w"] = DEFAULT_RIGHT_W
    try:
        data["log_pane_h"] = max(72, min(400, int(data.get("log_pane_h") or DEFAULT_LOG_H)))
    except (TypeError, ValueError):
        data["log_pane_h"] = DEFAULT_LOG_H
    return data


def save_prefs(data: dict[str, Any]) -> None:
    path = prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def reset_layout_prefs(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Clear window/pane layout keys back to defaults (keeps other settings)."""
    d = dict(data or load_prefs())
    d["geometry"] = DEFAULT_GEOMETRY
    d["zoomed"] = False
    d["left_pane_w"] = DEFAULT_LEFT_W
    d["right_pane_w"] = DEFAULT_RIGHT_W
    d["log_pane_h"] = DEFAULT_LOG_H
    return d
