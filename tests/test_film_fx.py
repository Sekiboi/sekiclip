"""Film-making helpers + render_cut film path (ffmpeg required for integration)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sekiclip import media_ops as ops
from sekiclip.core.models import Look
from sekiclip.media_ops.film_fx import (
    COLOR_LOOKS,
    VFX_PRESETS,
    color_look_filter,
    end_card_filter,
    escape_drawtext,
    title_filters,
    transition_name,
    vfx_filter,
)

has_ffmpeg = bool(ops.find_ffmpeg())
pytestmark_ff = pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg required")


def test_color_look_filter_keys() -> None:
    assert color_look_filter("none") == []
    assert color_look_filter("warm")
    assert color_look_filter("bw", 0.5) == ["hue=s=0.500"]
    assert color_look_filter("warm", 0.0) == []
    for key in COLOR_LOOKS:
        # all keys resolve without error
        color_look_filter(key, 1.0)


def test_vfx_filter_keys() -> None:
    assert vfx_filter("none") == []
    assert vfx_filter("vignette")
    assert vfx_filter("grain", 0.5)
    for key in VFX_PRESETS:
        vfx_filter(key, 1.0)


def test_title_and_end_card() -> None:
    assert title_filters("") == []
    t = title_filters("Hello", subtitle="World", position="lower-third")
    assert len(t) == 2
    assert "Hello" in t[0]
    assert escape_drawtext("a:b") == "a\\:b"
    assert end_card_filter("", out_dur=5) == []
    assert end_card_filter("Thanks", hold=2.0, out_dur=5.0)


def test_transition_name() -> None:
    assert transition_name("crossfade") == "fade"
    assert transition_name("cut") == "cut"
    assert transition_name("dip_black") == "fadeblack"
    assert transition_name("wipe-left") == "wipeleft"


def test_look_film_roundtrip() -> None:
    look = Look(
        color_look="warm",
        vfx="vignette",
        title="Open",
        end_card="Thanks",
        music_path=Path("bed.mp3"),
        music_duck=True,
    )
    again = Look.from_dict(look.to_dict())
    assert again.color_look == "warm"
    assert again.vfx == "vignette"
    assert again.title == "Open"
    assert again.end_card == "Thanks"
    assert again.music_duck is True
    assert str(again.music_path).endswith("bed.mp3")


def _make_video(path: Path, seconds: float = 1.5, color: str = "blue") -> Path:
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
            f"color=c={color}:s=320x240:d={seconds}",
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


def _make_audio(path: Path, seconds: float = 2.0) -> Path:
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
            f"sine=frequency=220:duration={seconds}",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytestmark_ff
def test_render_cut_color_title_music(tmp_path: Path) -> None:
    vid = _make_video(tmp_path / "v.mp4", 2.0)
    music = _make_audio(tmp_path / "m.m4a", 2.0)
    out = ops.render_cut(
        vid,
        tmp_path / "film.mp4",
        start=0,
        end=1.5,
        color_look="warm",
        color_strength=0.8,
        vfx="vignette",
        vfx_strength=0.7,
        title="Sekiclip",
        title_sub="Demo",
        title_position="center",
        end_card="The End",
        end_card_hold=0.8,
        music=music,
        music_volume=0.3,
        music_fade_in=0.2,
        music_fade_out=0.3,
        video_fade_in=0.1,
        video_fade_out=0.1,
        crf=28,
        preset="ultrafast",
    )
    assert out.is_file() and out.stat().st_size > 1000


@pytestmark_ff
def test_assemble_shots_crossfade(tmp_path: Path) -> None:
    a = _make_video(tmp_path / "a.mp4", 1.2, "red")
    b = _make_video(tmp_path / "b.mp4", 1.2, "green")
    out = ops.assemble_shots(
        [a, b],
        tmp_path / "asm.mp4",
        transition="crossfade",
        transition_dur=0.3,
        crf=28,
        preset="ultrafast",
    )
    assert out.is_file() and out.stat().st_size > 1000
    cut = ops.assemble_shots(
        [a, b],
        tmp_path / "cut.mp4",
        transition="cut",
        crf=28,
        preset="ultrafast",
    )
    assert cut.is_file()
