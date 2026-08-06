"""Cut timeline / fade best-practice math (preview = export)."""

from __future__ import annotations

from clipwork.preview_match import (
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


def test_export_fade_pairs_match_cut_end() -> None:
    v_bits, a_bits = export_fade_filter_pairs(10.0, 0.0, 1.0, 0.0, 1.0)
    assert any("st=9.0000" in x and "d=1.0000" in x for x in v_bits)
    assert any("st=9.0000" in x and "d=1.0000" in x for x in a_bits)
