"""Audio operations via ffmpeg."""

from __future__ import annotations

from pathlib import Path

from sekiclip.media_ops.ffmpeg_util import default_output, run_ffmpeg, unique_path

AUDIO_FORMATS = ("mp3", "wav", "flac", "m4a", "ogg", "aac")


def convert_audio(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fmt: str = "mp3",
) -> Path:
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    if fmt not in AUDIO_FORMATS:
        raise ValueError(f"Unsupported audio format: {fmt}")
    out = Path(dest) if dest else default_output(src, f".{fmt}", "convert")
    out.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "mp3":
        ac = ["-c:a", "libmp3lame", "-q:a", "2"]
    elif fmt in ("m4a", "aac"):
        ac = ["-c:a", "aac", "-b:a", "192k"]
    elif fmt == "wav":
        ac = ["-c:a", "pcm_s16le"]
    elif fmt == "flac":
        ac = ["-c:a", "flac"]
    else:
        ac = ["-c:a", "libvorbis", "-q:a", "5"]

    run_ffmpeg(["-i", str(src), "-vn", *ac, str(out)])
    return out


def compress_audio(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    bitrate: str = "128k",
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, ".mp3", "compress")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, str(out)]
    )
    return out


def normalize_audio(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    integrated_lufs: float = -16.0,
    true_peak: float = -1.5,
    lra: float = 11.0,
) -> Path:
    """EBU R128 loudnorm (single pass — approximate, not a mastering suite)."""
    src = Path(src)
    out = Path(dest) if dest else default_output(src, ".mp3", "norm")
    out.parent.mkdir(parents=True, exist_ok=True)
    i = float(integrated_lufs)
    tp = float(true_peak)
    lr = float(lra)
    af = f"loudnorm=I={i}:TP={tp}:LRA={lr}"
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-vn",
            "-af",
            af,
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out),
        ]
    )
    return out


def to_mono(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    rate: int = 44100,
) -> Path:
    src = Path(src)
    out = Path(dest) if dest else default_output(src, ".mp3", "mono")
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(rate),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out),
        ]
    )
    return out
