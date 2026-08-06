"""Lightweight A/V edit ops (single ffmpeg invocations — low upkeep)."""

from __future__ import annotations

from pathlib import Path

from clipwork.media_ops.ffmpeg_util import (
    default_output,
    probe,
    run_ffmpeg,
    unique_path,
    warn,
)

SPEED_PRESETS = ("0.5", "0.75", "1.0", "1.25", "1.5", "2.0")


def _out(src: Path, dest: Path | str | None, suffix: str, tag: str) -> Path:
    if dest:
        p = Path(dest)
        return unique_path(p) if p.exists() else p
    return default_output(src, suffix, tag)


def crop_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    # margin crop in pixels (from each edge) if width/height not set
    margin: int = 0,
) -> Path:
    """Crop video. Either explicit x,y,w,h or equal margin from all edges."""
    src = Path(src)
    out = _out(src, dest, src.suffix or ".mp4", "crop")
    out.parent.mkdir(parents=True, exist_ok=True)

    if width is None or height is None:
        data = probe(src)
        vw = vh = 0
        for s in data.get("streams") or []:
            if s.get("codec_type") == "video":
                vw = int(s.get("width") or 0)
                vh = int(s.get("height") or 0)
                break
        if vw < 2 or vh < 2:
            raise RuntimeError("Could not read video size for crop")
        m = max(0, int(margin))
        x, y = m, m
        width = max(2, vw - 2 * m)
        height = max(2, vh - 2 * m)
        # even dimensions for yuv420
        width -= width % 2
        height -= height % 2

    w, h = int(width), int(height)
    w -= w % 2
    h -= h % 2
    if w < 2 or h < 2:
        raise ValueError("Crop size too small")

    vf = f"crop={w}:{h}:{int(x)}:{int(y)}"
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
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def adjust_volume(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    volume: float = 1.0,
    mute: bool = False,
) -> Path:
    """Change volume (linear multiplier) or mute. Works on video or audio."""
    src = Path(src)
    out = _out(src, dest, src.suffix or ".mp4", "mute" if mute else "vol")
    out.parent.mkdir(parents=True, exist_ok=True)

    if mute:
        # Keep video if present, silence audio
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-c:v",
                "copy",
                "-af",
                "volume=0",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(out),
            ]
        )
        return out

    vol = max(0.0, float(volume))
    # Detect if source is audio-only by extension heuristic; ffmpeg handles both
    ext = src.suffix.lower()
    if ext in {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-vn",
                "-af",
                f"volume={vol}",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(out),
            ]
        )
    else:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-c:v",
                "copy",
                "-af",
                f"volume={vol}",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(out),
            ]
        )
    return out


def change_speed(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    speed: float = 1.0,
) -> Path:
    """Change playback speed (0.5–2.0 recommended). Re-encodes."""
    src = Path(src)
    sp = float(speed)
    if sp <= 0:
        raise ValueError("speed must be > 0")
    if sp < 0.5 or sp > 2.0:
        warn("Speed outside 0.5–2.0 may sound poor (atempo chain limits).")

    out = _out(src, dest, src.suffix or ".mp4", f"speed{sp:g}")
    out.parent.mkdir(parents=True, exist_ok=True)

    # setpts for video: PTS/speed; atempo for audio (0.5–2.0 per filter)
    vfilter = f"setpts=PTS/{sp}"
    # Chain atempo if needed for extreme speeds
    remaining = sp
    a_parts: list[str] = []
    # atempo accepts 0.5–100 but quality best 0.5–2.0
    while remaining > 2.0 + 1e-6:
        a_parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        a_parts.append("atempo=0.5")
        remaining /= 0.5
    a_parts.append(f"atempo={remaining:.6f}")
    afilter = ",".join(a_parts)

    ext = src.suffix.lower()
    if ext in {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus"}:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-vn",
                "-af",
                afilter,
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(out),
            ]
        )
    else:
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-filter_complex",
                f"[0:v]{vfilter}[v];[0:a]{afilter}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(out),
            ]
        )
    return out


def export_gif(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    start: float = 0.0,
    duration: float | None = None,
    end: float | None = None,
    fps: int = 12,
    max_width: int = 480,
    fmt: str = "gif",
) -> Path:
    """Export a short clip as GIF or animated WebP from In/Out range."""
    src = Path(src)
    fmt = fmt.lower().lstrip(".")
    if fmt not in ("gif", "webp"):
        raise ValueError("fmt must be gif or webp")
    if duration is None and end is not None:
        duration = max(0.1, float(end) - float(start))
    if duration is None:
        duration = 3.0

    out = _out(src, dest, f".{fmt}", fmt)
    out.parent.mkdir(parents=True, exist_ok=True)

    # palettegen for better GIF quality
    if fmt == "gif":
        vf = (
            f"fps={int(fps)},scale={int(max_width)}:-1:flags=lanczos,"
            f"split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        )
        run_ffmpeg(
            [
                "-ss",
                str(start),
                "-t",
                str(duration),
                "-i",
                str(src),
                "-vf",
                vf,
                "-loop",
                "0",
                str(out),
            ]
        )
    else:
        vf = f"fps={int(fps)},scale={int(max_width)}:-1:flags=lanczos"
        run_ffmpeg(
            [
                "-ss",
                str(start),
                "-t",
                str(duration),
                "-i",
                str(src),
                "-vf",
                vf,
                "-c:v",
                "libwebp",
                "-lossless",
                "0",
                "-q:v",
                "70",
                "-loop",
                "0",
                "-an",
                str(out),
            ]
        )
    return out


def fade_media(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    fade_in: float = 0.5,
    fade_out: float = 0.5,
) -> Path:
    """Short fade in/out on video and/or audio."""
    src = Path(src)
    out = _out(src, dest, src.suffix or ".mp4", "fade")
    out.parent.mkdir(parents=True, exist_ok=True)
    fi = max(0.0, float(fade_in))
    fo = max(0.0, float(fade_out))

    data = probe(src)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Could not read duration for fade")

    has_video = any(s.get("codec_type") == "video" for s in data.get("streams") or [])
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])

    filters: list[str] = []
    maps: list[str] = []
    if has_video:
        # fade out starts at duration - fo
        st_out = max(0.0, duration - fo) if fo > 0 else duration
        parts = []
        if fi > 0:
            parts.append(f"fade=t=in:st=0:d={fi}")
        if fo > 0:
            parts.append(f"fade=t=out:st={st_out}:d={fo}")
        if parts:
            filters.append(f"[0:v]{','.join(parts)}[v]")
            maps.extend(["-map", "[v]"])
        else:
            maps.extend(["-map", "0:v"])
    if has_audio:
        st_out = max(0.0, duration - fo) if fo > 0 else duration
        parts = []
        if fi > 0:
            parts.append(f"afade=t=in:st=0:d={fi}")
        if fo > 0:
            parts.append(f"afade=t=out:st={st_out}:d={fo}")
        if parts:
            filters.append(f"[0:a]{','.join(parts)}[a]")
            maps.extend(["-map", "[a]"])
        else:
            maps.extend(["-map", "0:a"])

    args = ["-i", str(src)]
    if filters:
        args.extend(["-filter_complex", ";".join(filters)])
    args.extend(maps)
    if has_video:
        args.extend(["-c:v", "libx264", "-crf", "23"])
    if has_audio:
        args.extend(["-c:a", "aac", "-b:a", "128k"])
    args.extend(["-movflags", "+faststart", str(out)])
    run_ffmpeg(args)
    return out


def flip_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    horizontal: bool = True,
) -> Path:
    src = Path(src)
    tag = "flip_h" if horizontal else "flip_v"
    out = _out(src, dest, src.suffix or ".mp4", tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = "hflip" if horizontal else "vflip"
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
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def target_size_video(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    max_mb: float = 25.0,
) -> Path:
    """Rough single-pass bitrate target so output is near max_mb (not exact)."""
    src = Path(src)
    out = _out(src, dest, ".mp4", f"under{int(max_mb)}mb")
    out.parent.mkdir(parents=True, exist_ok=True)

    data = probe(src)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0.5:
        raise RuntimeError("Duration too short for size targeting")

    # total bits budget; leave ~10% for container/audio
    total_bits = max_mb * 1024 * 1024 * 8
    audio_bps = 128_000
    video_bps = max(200_000, int((total_bits * 0.9) / duration - audio_bps))
    # cap absurd values
    video_bps = min(video_bps, 12_000_000)

    warn(
        f"Size target ~{max_mb} MB uses bitrate ~{video_bps // 1000} kbps "
        "(approximate; not two-pass exact)."
    )
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(int(video_bps * 1.2)),
            "-bufsize",
            str(int(video_bps * 2)),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def burn_subtitles(
    src: Path | str,
    srt: Path | str,
    dest: Path | str | None = None,
) -> Path:
    """Burn a .srt subtitle file into the video (re-encode)."""
    src = Path(src)
    srt = Path(srt)
    if not srt.is_file():
        raise FileNotFoundError(srt)
    out = _out(src, dest, ".mp4", "subs")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Escape path for ffmpeg subtitles filter on Windows
    sub_path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles='{sub_path}'"
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
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def logo_overlay(
    src: Path | str,
    logo: Path | str,
    dest: Path | str | None = None,
    *,
    position: str = "top-right",
    scale: float = 0.15,
    opacity: float = 1.0,
) -> Path:
    """Overlay a logo image on video."""
    src = Path(src)
    logo = Path(logo)
    if not logo.is_file():
        raise FileNotFoundError(logo)
    out = _out(src, dest, ".mp4", "logo")
    out.parent.mkdir(parents=True, exist_ok=True)

    pos = position.lower().replace("_", "-")
    # overlay x/y
    if pos in ("top-left", "tl"):
        xy = "10:10"
    elif pos in ("top-right", "tr"):
        xy = "W-w-10:10"
    elif pos in ("bottom-left", "bl"):
        xy = "10:H-h-10"
    elif pos in ("bottom-right", "br"):
        xy = "W-w-10:H-h-10"
    else:
        xy = "(W-w)/2:(H-h)/2"

    sc = max(0.05, min(0.5, float(scale)))
    op = max(0.1, min(1.0, float(opacity)))
    # scale logo relative to main width
    fc = (
        f"[1:v]scale=iw*{sc}:-1,format=rgba,colorchannelmixer=aa={op}[lg];"
        f"[0:v][lg]overlay={xy}[v]"
    )
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-i",
            str(logo),
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-crf",
            "23",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out
