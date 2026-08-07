"""Session store helpers (no GUI)."""

from __future__ import annotations

from pathlib import Path

from sekiclip.core import session_store


def test_edit_action_roundtrip() -> None:
    assert session_store.EDIT_LABEL_TO_KEY["Full cut"] == "render_cut"
    assert session_store.EDIT_KEY_TO_LABEL["render_cut"] == "Full cut"
    assert len(session_store.EDIT_ACTION_LABELS) >= 5


def test_export_presets() -> None:
    assert "Share 1080p" in session_store.EXPORT_PRESET_MAP
    v, a = session_store.EXPORT_PRESET_MAP["Archive"]
    assert v == "original" and a == "320k"


def test_session_json_roundtrip(tmp_path: Path) -> None:
    payload = session_store.build_session_dict(
        media_path=tmp_path / "a.mp4",
        in_point=1.5,
        out_point=9.0,
        position=2.0,
        look={
            "edit_action": "render_cut",
            "video_quality": "1080p",
            "audio_quality": "256k",
            "fade_video": True,
            "v_fade_in": "0.5",
            "v_fade_out": "0.5",
            "volume": "1.0",
            "speed": "1.0",
            "crop_rect": (0.1, 0.1, 0.9, 0.9),
        },
        tool="Edit",
    )
    p = tmp_path / "t.sekiclip.json"
    session_store.save_session_file(p, payload)
    loaded = session_store.load_session_file(p)
    assert loaded["in_point"] == 1.5
    assert loaded["look"]["video_quality"] == "1080p"
