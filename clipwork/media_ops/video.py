"""Video operations via ffmpeg."""

from __future__ import annotations

from pathlib import Path

from clipwork.media_ops.ffmpeg_util import (
    default_output,
    run_ffmpeg,
    unique_path,
    warn,
)

# Container / codec presets (keep small — low upkeep).
VIDEO_FORMATS = ("mp4", "webm", "mkv", "mov", "avi")
COMPRESS_PRESETS = {
    "chat": {"crf": "28", "scale": "1280:-2", "audio_k": "96k"},
    "balanced": {"crf": "23", "scale": None, "audio_k": "128k"},
    "quality": {"crf": "18", "scale": None, "audio_k": "192k"},
}


def convert_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fmt: str = "mp4",
) -> Path:
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    if fmt not in VIDEO_FORMATS:
        raise ValueError(f"Unsupported video format: {fmt}. Choose from {VIDEO_FORMATS}")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, f".{fmt}", "convert")
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "webm":
        args = [
            "-i",
            str(src),
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "32",
            "-c:a",
            "libopus",
            str(out),
        ]
    elif fmt == "mp4" or fmt == "mov":
        args = [
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(out),
        ]
    elif fmt == "mkv":
        args = [
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-c:a",
            "aac",
            str(out),
        ]
    else:  # avi
        args = [
            "-i",
            str(src),
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "libmp3lame",
            str(out),
        ]
    run_ffmpeg(args)
    return out


def compress_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    preset: str = "balanced",
) -> Path:
    src = Path(src)
    preset = preset.lower()
    if preset not in COMPRESS_PRESETS:
        raise ValueError(f"Unknown preset {preset}; use {tuple(COMPRESS_PRESETS)}")
    cfg = COMPRESS_PRESETS[preset]
    out = Path(dest) if dest else default_output(src, src.suffix or ".mp4", f"compress_{preset}")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ".mp4", f"compress_{preset}")
    out.parent.mkdir(parents=True, exist_ok=True)

    args = ["-i", str(src), "-c:v", "libx264", "-preset", "medium", "-crf", cfg["crf"]]
    if cfg["scale"]:
        args.extend(["-vf", f"scale={cfg['scale']}"])
    args.extend(["-c:a", "aac", "-b:a", cfg["audio_k"], "-movflags", "+faststart", str(out)])
    run_ffmpeg(args)
    return out


def trim_media(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    start: float | str = 0,
    end: float | str | None = None,
    duration: float | str | None = None,
    reencode: bool = False,
) -> Path:
    """Trim audio or video. Prefer stream copy unless reencode=True."""
    src = Path(src)
    suffix = src.suffix or ".mp4"
    out = Path(dest) if dest else default_output(src, suffix, "trim")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, suffix, "trim")
    out.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = ["-ss", str(start), "-i", str(src)]
    if duration is not None:
        args.extend(["-t", str(duration)])
    elif end is not None:
        # duration = end - start when both numeric
        try:
            dur = float(end) - float(start)
            if dur <= 0:
                raise ValueError("end must be greater than start")
            args.extend(["-t", str(dur)])
        except (TypeError, ValueError):
            args.extend(["-to", str(end)])
    if reencode:
        if suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"):
            args.extend(["-c:a", "aac" if suffix.lower() == ".m4a" else "libmp3lame", str(out)])
        else:
            args.extend(
                ["-c:v", "libx264", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(out)]
            )
    else:
        args.extend(["-c", "copy", str(out)])
        warn("Trim used stream copy (fast); cut may snap to keyframes. Use reencode for accuracy.")
    run_ffmpeg(args)
    return out


def extract_audio(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fmt: str = "mp3",
) -> Path:
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    out = Path(dest) if dest else default_output(src, f".{fmt}", "audio")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, f".{fmt}", "audio")
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "mp3":
        acodec = ["-c:a", "libmp3lame", "-q:a", "2"]
    elif fmt in ("m4a", "aac"):
        acodec = ["-c:a", "aac", "-b:a", "192k"]
    elif fmt == "wav":
        acodec = ["-c:a", "pcm_s16le"]
    elif fmt == "flac":
        acodec = ["-c:a", "flac"]
    elif fmt == "ogg":
        acodec = ["-c:a", "libvorbis", "-q:a", "5"]
    else:
        raise ValueError(f"Unsupported audio format: {fmt}")

    run_ffmpeg(["-i", str(src), "-vn", *acodec, str(out)])
    return out


def remux(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fmt: str = "mp4",
) -> Path:
    """Change container without re-encoding when possible."""
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    out = Path(dest) if dest else default_output(src, f".{fmt}", "remux")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, f".{fmt}", "remux")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(src), "-c", "copy", str(out)])
    return out


def rotate_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    degrees: int = 90,
) -> Path:
    src = Path(src)
    degrees = int(degrees) % 360
    if degrees not in (90, 180, 270):
        raise ValueError("degrees must be 90, 180, or 270")
    out = Path(dest) if dest else default_output(src, src.suffix or ".mp4", f"rot{degrees}")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ".mp4", f"rot{degrees}")
    out.parent.mkdir(parents=True, exist_ok=True)
    # transpose: 1=90CW, 2=90CCW, for 180 use two transpose or rotate filter
    if degrees == 90:
        vf = "transpose=1"
    elif degrees == 270:
        vf = "transpose=2"
    else:
        vf = "transpose=1,transpose=1"
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-c:a",
            "copy",
            str(out),
        ]
    )
    return out


def strip_audio(src: Path | str, dest: Path | str | None = None) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, src.suffix or ".mp4", "silent")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ".mp4", "silent")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(src), "-c:v", "copy", "-an", str(out)])
    return out


def grab_frame(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    time: float | str = 0,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, ".jpg", "frame")
    if dest:
        out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    else:
        out = default_output(src, ".jpg", "frame")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-ss", str(time), "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)]
    )
    return out


def concat_videos(
    sources: list[Path | str],
    dest: Path | str,
    *,
    reencode: bool = True,
) -> Path:
    """Concatenate videos. Default reencode for mismatched inputs (safer)."""
    paths = [Path(p) for p in sources]
    if len(paths) < 2:
        raise ValueError("Need at least two files to concatenate")
    out = unique_path(Path(dest)) if Path(dest).exists() else Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)

    if reencode:
        # filter_complex concat — works across differing params better with scale
        inputs: list[str] = []
        for p in paths:
            inputs.extend(["-i", str(p)])
        n = len(paths)
        # Scale/pad to first video size is complex; simple concat demuxer after reencode each is heavier.
        # Use concat filter with aformat/vformat normalize:
        fc_parts = []
        for i in range(n):
            fc_parts.append(
                f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}];"
                f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}];"
            )
        links = "".join(f"[v{i}][a{i}]" for i in range(n))
        fc = "".join(fc_parts) + f"{links}concat=n={n}:v=1:a=1[outv][outa]"
        run_ffmpeg(
            [
                *inputs,
                "-filter_complex",
                fc,
                "-map",
                "[outv]",
                "-map",
                "[outa]",
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-c:a",
                "aac",
                str(out),
            ]
        )
    else:
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            for p in paths:
                # concat demuxer needs escaped paths
                esc = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
                fh.write(f"file '{esc}'\n")
            list_path = fh.name
        try:
            run_ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", str(out)]
            )
        finally:
            Path(list_path).unlink(missing_ok=True)
        warn("Concat stream-copy requires identical codecs; use reencode if this fails.")
    return out
