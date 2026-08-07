"""Pytest setup: writable temp on Windows + shared ffmpeg availability."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Prefer a user-writable temp root (avoids PermissionError under protected dirs)
_BASE = Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
_PYTEST_ROOT = _BASE / "sekiclip_pytest"
try:
    _PYTEST_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    _PYTEST_ROOT = Path(tempfile.mkdtemp(prefix="sekiclip_pytest_"))


def pytest_configure(config: pytest.Config) -> None:
    """Point pytest basetemp at a writable location when possible."""
    try:
        basetemp = _PYTEST_ROOT / "basetemp"
        basetemp.mkdir(parents=True, exist_ok=True)
        # Only set if user did not pass --basetemp
        if not config.option.basetemp:
            config.option.basetemp = str(basetemp)
    except Exception:
        pass


@pytest.fixture()
def tmp_media(tmp_path: Path) -> Path:
    """Isolated directory for media fixtures (always under pytest tmp)."""
    d = tmp_path / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _has_ffmpeg() -> bool:
    try:
        from sekiclip.media_ops import find_ffmpeg, find_ffprobe

        return bool(find_ffmpeg() and find_ffprobe())
    except Exception:
        return False


@pytest.fixture(scope="session")
def ffmpeg_available() -> bool:
    return _has_ffmpeg()


requires_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
