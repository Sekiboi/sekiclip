"""Tests for P0–P2 edit ops (ffmpeg required)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from clipwork import media_ops as ops

has_ffmpeg = bool(ops.find_ffmpeg())
pytestmark = pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg required")


def _make_video(path: Path, seconds: float = 1.0) -> Path:
    ff = ops.find_ffmpeg()
    assert ff
    subprocess.run(
        [
            str(ff),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s=320x240:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_p0_crop_volume_speed_gif(tmp_path: Path) -> None:
    vid = _make_video(tmp_path / "v.mp4", 1.2)
    cropped = ops.crop_video(vid, tmp_path / "c.mp4", margin=10)
    assert cropped.is_file() and cropped.stat().st_size > 0
    muted = ops.adjust_volume(vid, tmp_path / "m.mp4", mute=True)
    assert muted.is_file()
    loud = ops.adjust_volume(vid, tmp_path / "l.mp4", volume=0.5)
    assert loud.is_file()
    sped = ops.change_speed(vid, tmp_path / "s.mp4", speed=1.5)
    assert sped.is_file()
    gif = ops.export_gif(vid, tmp_path / "g.gif", start=0, duration=0.5, fps=8, max_width=160)
    assert gif.is_file() and gif.suffix == ".gif"


def test_p1_fade_flip_target(tmp_path: Path) -> None:
    vid = _make_video(tmp_path / "v.mp4", 1.5)
    faded = ops.fade_media(vid, tmp_path / "f.mp4", fade_in=0.2, fade_out=0.2)
    assert faded.is_file()
    flipped = ops.flip_video(vid, tmp_path / "h.mp4", horizontal=True)
    assert flipped.is_file()
    sized = ops.target_size_video(vid, tmp_path / "t.mp4", max_mb=5)
    assert sized.is_file()


def test_p2_logo(tmp_path: Path) -> None:
    vid = _make_video(tmp_path / "v.mp4", 0.8)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (40, 40), (255, 0, 0, 200)).save(logo)
    out = ops.logo_overlay(vid, logo, tmp_path / "logoed.mp4", position="top-right", scale=0.2)
    assert out.is_file()


def test_batch_folder(tmp_path: Path) -> None:
    a = _make_video(tmp_path / "a.mp4", 0.5)
    b = _make_video(tmp_path / "b.mp4", 0.5)
    out_dir = tmp_path / "out"
    results = ops.batch_to_folder(
        [a, b],
        out_dir,
        op_name="compress",
        run_one=lambda s, d: ops.compress_video(s, d, preset="chat"),
        name_tag="chat",
    )
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert (out_dir).is_dir()


def test_share_presets_exist() -> None:
    for name in ("discord", "whatsapp", "email", "720p", "1080p", "fast_gpu"):
        assert name in ops.COMPRESS_PRESETS
