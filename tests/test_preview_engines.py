"""Preview audio/frame engines (no GUI)."""

from __future__ import annotations

from sekiclip.preview.audio import PreviewAudioEngine, SAMPLE_RATE
from sekiclip.preview.frames import AsyncFrameCache, quantize_time


def test_quantize_time() -> None:
    assert quantize_time(1.02, 0.05) == 1.0
    assert quantize_time(1.04, 0.05) == 1.05


def test_audio_engine_available_flag() -> None:
    eng = PreviewAudioEngine()
    # May be True if sounddevice+ffmpeg installed in CI/dev
    assert isinstance(eng.available, bool)
    assert eng.metrics()["engine"] == "none"
    eng.set_gain(1.5, mute=False)
    assert eng.volume == 1.5
    eng.stop()
    eng.close()


def test_frame_cache_empty_path() -> None:
    c = AsyncFrameCache(max_size=8, use_hwaccel=False)
    assert c.get_cached(0.0) is None
    assert c.extract_sync(0.0) is None
    c.close()


def test_sample_rate_constant() -> None:
    assert SAMPLE_RATE == 48000
