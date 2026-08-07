"""Tests for Sekiclip media ops (needs ffmpeg for A/V)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from sekiclip import media_ops as ops

ffmpeg = ops.find_ffmpeg()
ffprobe = ops.find_ffprobe()
has_ffmpeg = bool(ffmpeg and ffprobe)

pytestmark_av = pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg/ffprobe not available")


@pytest.fixture()
def tmp_media(tmp_path: Path) -> Path:
    return tmp_path


def _make_png(path: Path, color: tuple[int, int, int] = (30, 144, 255)) -> Path:
    Image.new("RGB", (120, 80), color).save(path)
    return path


def _make_video(path: Path, seconds: float = 1.0) -> Path:
    assert ffmpeg
    # solid color test pattern
    subprocess.run(
        [
            str(ffmpeg),
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


def _make_wav(path: Path, seconds: float = 0.5) -> Path:
    assert ffmpeg
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=880:duration={seconds}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_image_pipeline(tmp_media: Path) -> None:
    src = _make_png(tmp_media / "a.png")
    out = ops.compress_image(src, tmp_media / "a_c.jpg", quality=70, max_edge=64)
    assert out.is_file() and out.stat().st_size > 0
    r = ops.resize_image(src, tmp_media / "a_r.png", max_edge=40)
    assert r.is_file()
    rot = ops.rotate_image(src, tmp_media / "a_rot.png", degrees=90)
    assert rot.is_file()
    flip = ops.flip_image(src, tmp_media / "a_f.png")
    assert flip.is_file()
    nox = ops.strip_exif(src, tmp_media / "a_nx.png")
    assert nox.is_file()
    pdf = ops.images_to_pdf([src, src], tmp_media / "album.pdf")
    assert pdf.is_file() and pdf.suffix == ".pdf"
    conv = ops.convert_image(src, tmp_media / "a.webp", fmt="webp")
    assert conv.is_file()


def test_unique_path(tmp_media: Path) -> None:
    p = tmp_media / "x.txt"
    p.write_text("a", encoding="utf-8")
    u = ops.unique_path(p)
    assert u != p and not u.exists()


@pytestmark_av
def test_video_and_audio(tmp_media: Path) -> None:
    vid = _make_video(tmp_media / "t.mp4", 1.0)
    summary = ops.media_summary(vid)
    assert "t.mp4" in summary
    compressed = ops.compress_video(vid, tmp_media / "t_chat.mp4", preset="chat")
    assert compressed.is_file() and compressed.stat().st_size > 0
    trimmed = ops.trim_media(vid, tmp_media / "t_trim.mp4", start=0, duration=0.4, reencode=True)
    assert trimmed.is_file()
    audio = ops.extract_audio(vid, tmp_media / "t.mp3", fmt="mp3")
    assert audio.is_file()
    frame = ops.grab_frame(vid, tmp_media / "frame.jpg", time=0)
    assert frame.is_file()
    silent = ops.strip_audio(vid, tmp_media / "silent.mp4")
    assert silent.is_file()
    remuxed = ops.remux(vid, tmp_media / "t.mkv", fmt="mkv")
    assert remuxed.is_file()

    wav = _make_wav(tmp_media / "s.wav")
    mp3 = ops.convert_audio(wav, tmp_media / "s.mp3", fmt="mp3")
    assert mp3.is_file()
    norm = ops.normalize_audio(wav, tmp_media / "s_n.mp3")
    assert norm.is_file()
    mono = ops.to_mono(wav, tmp_media / "s_m.mp3")
    assert mono.is_file()


@pytestmark_av
def test_concat(tmp_media: Path) -> None:
    a = _make_video(tmp_media / "a.mp4", 0.5)
    b = _make_video(tmp_media / "b.mp4", 0.5)
    out = ops.concat_videos([a, b], tmp_media / "ab.mp4", reencode=True)
    assert out.is_file()
    data = ops.probe(out)
    dur = float((data.get("format") or {}).get("duration") or 0)
    assert dur >= 0.8


def test_cli_parser() -> None:
    from sekiclip.cli import build_parser

    p = build_parser()
    ns = p.parse_args(["info", "nope.mp4"])
    assert ns.command == "info"
    ns2 = p.parse_args(["compress", "a.mp4", "--preset", "chat"])
    assert ns2.preset == "chat"
