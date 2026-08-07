"""Typed models + tool registry."""

from __future__ import annotations

from sekiclip.core.models import ExportJob, JobErrorCode, Look, classify_error
from sekiclip.core.tools_registry import TOOL_NAMES, get_tool


def test_look_roundtrip() -> None:
    look = Look(edit_action="fade", video_quality="720p", volume="0.8")
    d = look.to_dict()
    again = Look.from_dict(d)
    assert again.edit_action == "fade"
    assert again.video_quality == "720p"
    assert again.volume == "0.8"


def test_classify_error() -> None:
    assert classify_error(RuntimeError("ffmpeg not found")) == JobErrorCode.FFMPEG_MISSING
    assert classify_error(FileNotFoundError("x")) == JobErrorCode.IO_ERROR


def test_tools_registry() -> None:
    assert "Edit" in TOOL_NAMES
    assert get_tool("Trim") is not None
    assert get_tool("Nope") is None


def test_export_job_summary() -> None:
    from pathlib import Path

    j = ExportJob(tool="Trim", src=Path("a.mp4"), dest=Path("b.mp4"))
    assert "Trim" in j.summary() and "a.mp4" in j.summary()
