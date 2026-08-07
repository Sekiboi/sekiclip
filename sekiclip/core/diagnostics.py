"""Anonymous local diagnostics export (never uploaded by the app)."""

from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from sekiclip import __version__
from sekiclip.media_ops import find_ffmpeg, find_ffprobe
from sekiclip.core.prefs import load_prefs, user_data_dir


def crash_log_path() -> Path:
    return user_data_dir() / "sekiclip_crash.log"


def job_log_tail(max_lines: int = 40) -> str:
    path = user_data_dir() / "sekiclip_jobs.log"
    if not path.is_file():
        return "(none)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]) if lines else "(none)"
    except OSError:
        return "(unreadable)"


def crash_log_tail(max_lines: int = 40) -> str:
    path = crash_log_path()
    if not path.is_file():
        return "(none)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max_lines:]) if lines else "(none)"
    except OSError:
        return "(unreadable)"


def build_report() -> str:
    prefs = load_prefs()
    ff = find_ffmpeg()
    fp = find_ffprobe()
    lines = [
        "### Sekiclip diagnostics (anonymous)",
        "",
        "This report is **anonymous**: no account, name, device ID, hostname,",
        "or full folder paths. Paths in log tails should be basenames only.",
        "",
        f"- **Version:** {__version__}",
        f"- **Frozen (exe):** {getattr(sys, 'frozen', False)}",
        f"- **Python:** {platform.python_version()} ({platform.python_implementation()})",
        f"- **OS:** {platform.system()} {platform.release()} ({platform.machine()})",
        f"- **ffmpeg:** {'found' if ff else 'not found'}",
        f"- **ffprobe:** {'found' if fp else 'not found'}",
        f"- **Diagnostics enabled (pref):** {prefs.get('diagnostics_enabled')}",
        f"- **Report generated (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "### Privacy",
        "",
        "- No media content is included.",
        "- Job log lines use **file basenames only**.",
        "- Nothing is sent by the app; you choose whether to paste this.",
        "",
        "### Recent job log (`sekiclip_jobs.log`)",
        "",
        "```",
        job_log_tail(),
        "```",
        "",
        "### Crash log (`sekiclip_crash.log`)",
        "",
        "```",
        crash_log_tail(),
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"
