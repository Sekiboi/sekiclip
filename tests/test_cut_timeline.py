"""Cut timeline / fade best-practice math (preview = export)."""

from __future__ import annotations

from sekiclip.preview.match import (
    CutTimeline,
    build_audio_filter,
    export_fade_filter_pairs,
    fade_strength_on_output,
    fit_fades,
    video_fade_strength_at_source,
)


def test_fade_out_starts_exactly_n_seconds_before_end() -> None:
    # 30s output cut, 1s fade-out → starts at t=29
    assert fade_strength_on_output(28.9, 30.0, 0.0, 1.0) == 0.0
    assert abs(fade_strength_on_output(29.5, 30.0, 0.0, 1.0) - 0.5) < 0.02
    assert abs(fade_strength_on_output(30.0, 30.0, 0.0, 1.0) - 1.0) < 0.02


def test_fit_fades_never_expands() -> None:
    a, b = fit_fades(30.0, 0.0, 1.0)
    assert a == 0.0 and abs(b - 1.0) < 1e-9
    # Only shrink when both don't fit
    a, b = fit_fades(2.0, 1.5, 1.5)
    assert a + b <= 2.0 * 0.98 + 1e-6


def test_source_playhead_maps_through_speed() -> None:
    # speed 2×: 1s output fade-out = 2s of source before Out
    cut = CutTimeline(0.0, 20.0, speed=2.0)
    assert abs(cut.output_duration - 10.0) < 1e-9
    # At source t=19 (1s before out): output time = 9.5; fade-out last 1s of 10 → starts at 9
    # so t_out=9.5 is mid-fade
    s = video_fade_strength_at_source(cut, 19.0, 0.0, 1.0)
    assert 0.4 < s < 0.6
    # Clear well before that
    assert video_fade_strength_at_source(cut, 10.0, 0.0, 1.0) == 0.0


def test_audio_filter_exact_one_second() -> None:
    cut = CutTimeline(0.0, 30.0, speed=1.0)
    af = build_audio_filter(
        start=0.0,
        end=30.0,
        cut=cut,
        audio_fade_out=1.0,
    )
    assert "atrim=start=0.0000:end=30.0000" in af
    assert "afade=t=out:st=29.0000:d=1.0000" in af


def test_audio_filter_preseeked_no_absolute_atrim() -> None:
    """Preview fast-seek: no atrim (demux -ss/-t); fades still use cut Out."""
    cut = CutTimeline(10.0, 40.0, speed=1.0)
    af = build_audio_filter(
        start=10.0,
        end=40.0,
        cut=cut,
        audio_fade_in=0.5,
        audio_fade_out=1.0,
        input_preseeked=True,
    )
    assert "atrim=" not in af
    assert "asetpts=PTS-STARTPTS" in af
    assert "afade=t=in:st=0:d=0.5000" in af
    assert "afade=t=out:st=29.0000:d=1.0000" in af


def test_fade_uses_mid_file_in_out() -> None:
    """Fades belong to the user's In/Out, not file start."""
    cut = CutTimeline(60.0, 90.0, speed=1.0)  # 30s cut mid-file
    # Fade-out last 2s of cut → source 88–90
    assert video_fade_strength_at_source(cut, 87.0, 0.0, 2.0) == 0.0
    s = video_fade_strength_at_source(cut, 89.0, 0.0, 2.0)
    assert 0.4 < s < 0.6
    # Fade-in first 1s → source 60–61
    assert video_fade_strength_at_source(cut, 60.5, 1.0, 0.0) > 0.4
    assert video_fade_strength_at_source(cut, 62.0, 1.0, 0.0) == 0.0
    # Outside cut: no fade strength from helper
    assert video_fade_strength_at_source(cut, 50.0, 1.0, 2.0) == 0.0


def test_ensure_legal_marks_clamps() -> None:
    from pathlib import Path

    from sekiclip.preview.session import MediaInfo, MediaKind, MediaSession

    s = MediaSession()
    s.info = MediaInfo(Path("x"), MediaKind.VIDEO, 50.0, 64, 64, True, True, "x", 30.0)
    s.in_point = -1.0
    s.out_point = 99.0
    s.position = 80.0
    notes = s.ensure_legal_marks()
    assert s.in_point == 0.0
    assert s.out_point == 50.0
    assert s.position == 50.0  # clamped to duration
    assert notes


def test_export_fade_pairs_match_cut_end() -> None:
    v_bits, a_bits = export_fade_filter_pairs(10.0, 0.0, 1.0, 0.0, 1.0)
    assert any("st=9.0000" in x and "d=1.0000" in x for x in v_bits)
    assert any("st=9.0000" in x and "d=1.0000" in x for x in a_bits)
