"""Tests for media preview helpers (no GUI)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from sekiclip.media_ops import find_ffmpeg
from sekiclip.preview.session import MediaKind, MediaSession, classify, format_time, load_info

has_ffmpeg = bool(find_ffmpeg())


def test_format_time() -> None:
    assert format_time(0).startswith("00:00")
    assert "01:" in format_time(65) or format_time(65).startswith("01:05")


def test_classify(tmp_path: Path) -> None:
    assert classify(Path("a.mp4")) == MediaKind.VIDEO
    assert classify(Path("a.mp3")) == MediaKind.AUDIO
    assert classify(Path("a.png")) == MediaKind.IMAGE


def test_load_image(tmp_path: Path) -> None:
    p = tmp_path / "x.png"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(p)
    info = load_info(p)
    assert info.kind == MediaKind.IMAGE
    assert info.width == 64 and info.height == 48
    sess = MediaSession()
    sess.open(p)
    assert sess.info is not None
    sess.seek(0)
    sess.close()


@pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg required")
def test_session_video(tmp_path: Path) -> None:
    ff = find_ffmpeg()
    assert ff
    vid = tmp_path / "v.mp4"
    subprocess.run(
        [
            str(ff),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:d=0.6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(vid),
        ],
        check=True,
        capture_output=True,
    )
    sess = MediaSession()
    info = sess.open(vid)
    assert info.duration > 0.3
    frames: list[float] = []

    def on_frame(img, t):  # type: ignore[no-untyped-def]
        if img is not None:
            frames.append(t)

    sess.on_frame = on_frame
    sess.seek(0.1)
    sess.set_in(0.05)
    sess.set_out(0.4)
    assert sess.in_point == pytest.approx(0.05, abs=0.02)
    assert sess.out_point is not None
    assert len(frames) >= 1
    sess.close()
