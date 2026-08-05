"""Local user prefs (offline only — never uploaded)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def user_data_dir() -> Path:
    """Writable app data (installed apps must not write under Program Files)."""
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            base = root / "Clipwork"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Clipwork"
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            base = Path(xdg) / "Clipwork" if xdg else Path.home() / ".local" / "share" / "Clipwork"
    else:
        base = Path(__file__).resolve().parent.parent
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def prefs_path() -> Path:
    return user_data_dir() / "clipwork_prefs.json"


def load_prefs() -> dict[str, Any]:
    data: dict[str, Any] = {
        "diagnostics_enabled": False,
        "first_run_completed": False,
        "last_output_dir": "",
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
    data["last_output_dir"] = str(data.get("last_output_dir") or "")
    return data


def save_prefs(data: dict[str, Any]) -> None:
    path = prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass
