"""Export encode helpers — GPU when available, CPU fallback. Offline only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sekiclip.media_ops.ffmpeg_util import run_ffmpeg, warn


def video_encode_args(
    *,
    crf: int = 20,
    preset: str = "medium",
    prefer_gpu: bool = False,
) -> list[str]:
    """Return ``-c:v …`` args. GPU only when prefer_gpu; always valid for libx264 path.

    Callers that need true GPU try/fallback should use ``run_encode_with_gpu_fallback``.
    """
    return [
        "-c:v",
        "libx264",
        "-preset",
        str(preset),
        "-crf",
        str(int(crf)),
        "-pix_fmt",
        "yuv420p",
    ]


def gpu_encoder_candidates(crf: int) -> list[tuple[str, list[str]]]:
    """Ordered hardware encoder attempts (best-effort)."""
    q = str(int(max(0, min(51, crf))))
    return [
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", q, "-pix_fmt", "yuv420p"]),
        (
            "h264_qsv",
            ["-c:v", "h264_qsv", "-global_quality", q, "-pix_fmt", "nv12"],
        ),
        (
            "h264_amf",
            [
                "-c:v",
                "h264_amf",
                "-quality",
                "balanced",
                "-rc",
                "cqp",
                "-qp_i",
                q,
                "-pix_fmt",
                "yuv420p",
            ],
        ),
    ]


def run_encode_with_gpu_fallback(
    base_args_before_vcodec: list[str],
    *,
    crf: int,
    preset: str,
    audio_args: list[str],
    tail_args: list[str],
    prefer_gpu: bool,
    out: Path,
    on_progress: Any = None,
    duration_hint: float | None = None,
) -> str:
    """Try GPU encoders when requested; always fall back to libx264.

    ``base_args_before_vcodec`` is typically ``[-i, src, -filter_complex, …, -map, …]``.
    Returns encoder name used (``libx264`` or hardware name).
    """
    out = Path(out)
    if prefer_gpu:
        for name, vargs in gpu_encoder_candidates(crf):
            args = [*base_args_before_vcodec, *vargs, *audio_args, *tail_args]
            try:
                if out.is_file():
                    try:
                        out.unlink()
                    except OSError:
                        pass
                run_ffmpeg(
                    args,
                    on_progress=on_progress,
                    duration_hint=duration_hint,
                )
                if out.is_file() and out.stat().st_size > 64:
                    warn(f"Export used hardware encoder: {name}")
                    return name
            except Exception:
                if out.is_file():
                    try:
                        out.unlink()
                    except OSError:
                        pass
                continue
        warn("Hardware encode unavailable; using CPU libx264.")

    args = [
        *base_args_before_vcodec,
        *video_encode_args(crf=crf, preset=preset),
        *audio_args,
        *tail_args,
    ]
    run_ffmpeg(args, on_progress=on_progress, duration_hint=duration_hint)
    return "libx264"
