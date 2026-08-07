"""Lightweight A/V edit ops (single ffmpeg invocations — low upkeep)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from sekiclip.media_ops.ffmpeg_util import (
    output_path,
    probe,
    run_ffmpeg,
    warn,
)

SPEED_PRESETS = ("0.5", "0.75", "1.0", "1.25", "1.5", "2.0")


def _out(src: Path, dest: Path | str | None, suffix: str, tag: str) -> Path:
    return output_path(dest, src, suffix=suffix, tag=tag)


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
    video_fade_in: float | None = None,
    video_fade_out: float | None = None,
    audio_fade_in: float | None = None,
    audio_fade_out: float | None = None,
    start: float = 0.0,
    end: float | None = None,
    crf: int = 20,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    scale: str | None = None,
    prefer_gpu: bool = False,
    on_progress: Callable[[float, str], None] | None = None,
) -> Path:
    """Fade in/out for video and/or audio (independently).

    If start/end given, cuts to that range first (one encode).
    video_*/audio_* override the shared fade_in/fade_out when set.
    """
    src = Path(src)
    out = _out(src, dest, ".mp4", "fade")
    out.parent.mkdir(parents=True, exist_ok=True)

    vfi = max(0.0, float(video_fade_in if video_fade_in is not None else fade_in))
    vfo = max(0.0, float(video_fade_out if video_fade_out is not None else fade_out))
    afi = max(0.0, float(audio_fade_in if audio_fade_in is not None else fade_in))
    afo = max(0.0, float(audio_fade_out if audio_fade_out is not None else fade_out))

    # Delegate to one-pass renderer for consistency
    return render_cut(
        src,
        out,
        start=start,
        end=end,
        video_fade_in=vfi,
        video_fade_out=vfo,
        audio_fade_in=afi,
        audio_fade_out=afo,
        crf=crf,
        preset=preset,
        audio_bitrate=audio_bitrate,
        scale=scale,
        prefer_gpu=prefer_gpu,
        on_progress=on_progress,
    )


def render_cut(
    src: Path | str,
    dest: Path | str | None = None,
    *,
    start: float = 0.0,
    end: float | None = None,
    crop_x: int = 0,
    crop_y: int = 0,
    crop_w: int | None = None,
    crop_h: int | None = None,
    flip_h: bool = False,
    flip_v: bool = False,
    speed: float = 1.0,
    volume: float = 1.0,
    mute: bool = False,
    video_fade_in: float = 0.0,
    video_fade_out: float = 0.0,
    audio_fade_in: float = 0.0,
    audio_fade_out: float = 0.0,
    logo: Path | str | None = None,
    logo_position: str = "top-right",
    logo_scale: float = 0.15,
    logo_opacity: float = 0.9,
    srt: Path | str | None = None,
    crf: int = 20,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    scale: str | None = None,
    prefer_gpu: bool = False,
    # Film-making (roadmap phases A–B)
    color_look: str = "none",
    color_strength: float = 1.0,
    vfx: str = "none",
    vfx_strength: float = 1.0,
    title: str = "",
    title_sub: str = "",
    title_position: str = "center",
    end_card: str = "",
    end_card_hold: float = 3.0,
    music: Path | str | None = None,
    music_volume: float = 0.35,
    music_fade_in: float = 1.0,
    music_fade_out: float = 1.5,
    music_duck: bool = False,
    on_progress: Callable[[float, str], None] | None = None,
) -> Path:
    """One-pass edit: cut + fades + looks/VFX/titles/music bed (single encode).

    ``prefer_gpu`` tries hardware H.264 when available; always falls back to libx264.
    """
    from sekiclip.media_ops.film_fx import (
        color_look_filter,
        end_card_filter,
        title_filters,
        vfx_filter,
    )
    from sekiclip.preview.match import atempo_chain, export_fade_filter_pairs

    src = Path(src)
    out = _out(src, dest, ".mp4", "cut")
    out.parent.mkdir(parents=True, exist_ok=True)

    data = probe(src)
    full_dur = float((data.get("format") or {}).get("duration") or 0)
    has_video = any(s.get("codec_type") == "video" for s in data.get("streams") or [])
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams") or [])
    if not has_video and not has_audio:
        raise RuntimeError("No video or audio streams found")

    start = max(0.0, float(start))
    if end is None or end <= start:
        end = full_dur if full_dur > start else start + 0.1
    end = min(float(end), full_dur) if full_dur > 0 else float(end)
    sel_dur = max(0.05, end - start)
    sp = max(0.25, min(4.0, float(speed)))
    out_dur = sel_dur / sp

    # --- video filters (trim inside filter for multi-input safety) ---
    v_parts: list[str] = [
        f"trim=start={start}:end={end}",
        "setpts=PTS-STARTPTS",
    ]
    if crop_w is not None and crop_h is not None:
        w = int(crop_w) - int(crop_w) % 2
        h = int(crop_h) - int(crop_h) % 2
        if w >= 2 and h >= 2:
            v_parts.append(f"crop={w}:{h}:{int(crop_x)}:{int(crop_y)}")
    if flip_h:
        v_parts.append("hflip")
    if flip_v:
        v_parts.append("vflip")
    if abs(sp - 1.0) > 1e-3:
        v_parts.append(f"setpts=PTS/{sp}")
    if scale:
        # contain-fit into WxH box
        if ":" in str(scale) and "force" not in str(scale):
            sw, sh = str(scale).split(":", 1)
            v_parts.append(
                f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
                f"pad={sw}:{sh}:(ow-iw)/2:(oh-ih)/2"
            )
        else:
            v_parts.append(f"scale={scale}")
    v_parts.extend(color_look_filter(color_look, color_strength))
    v_parts.extend(vfx_filter(vfx, vfx_strength))
    if srt and has_video:
        srt_p = Path(srt)
        if srt_p.is_file():
            sub = str(srt_p.resolve()).replace("\\", "/").replace(":", "\\:")
            v_parts.append(f"subtitles='{sub}'")
    v_parts.extend(
        title_filters(title, subtitle=title_sub, position=title_position)
    )
    v_parts.extend(end_card_filter(end_card, hold=end_card_hold, out_dur=out_dur))

    # Fades on the *output* cut timeline (after speed). N seconds before Out.
    v_fade_bits, a_fade_bits = export_fade_filter_pairs(
        out_dur,
        float(video_fade_in),
        float(video_fade_out),
        float(audio_fade_in),
        float(audio_fade_out),
    )
    v_parts.extend(v_fade_bits)

    # --- main audio filters ---
    a_parts: list[str] = [
        f"atrim=start={start}:end={end}",
        "asetpts=PTS-STARTPTS",
    ]
    if mute:
        a_parts.append("volume=0")
    else:
        vol = max(0.0, float(volume))
        if abs(vol - 1.0) > 1e-3:
            a_parts.append(f"volume={vol}")
    a_parts.extend(atempo_chain(sp))
    a_parts.extend(a_fade_bits)
    if has_audio and out_dur > 0:
        a_parts.append(f"apad=whole_dur={out_dur:.4f}")

    logo_path = Path(logo) if logo else None
    use_logo = bool(has_video and logo_path and logo_path.is_file())
    music_path = Path(music) if music else None
    use_music = bool(music_path and music_path.is_file())

    fc_bits: list[str] = []
    maps: list[str] = []
    # Input indices: 0=src, then logo, then music
    next_in = 1
    logo_in = music_in = -1
    if use_logo:
        logo_in = next_in
        next_in += 1
    if use_music:
        music_in = next_in
        next_in += 1

    if has_video:
        if use_logo:
            sc = max(0.05, min(0.5, float(logo_scale)))
            op = max(0.1, min(1.0, float(logo_opacity)))
            pos = logo_position.lower().replace("_", "-")
            if pos in ("top-left", "tl"):
                xy = "10:10"
            elif pos in ("bottom-left", "bl"):
                xy = "10:H-h-10"
            elif pos in ("bottom-right", "br"):
                xy = "W-w-10:H-h-10"
            elif pos in ("center", "c"):
                xy = "(W-w)/2:(H-h)/2"
            else:
                xy = "W-w-10:10"
            fc_bits.append(f"[0:v]{','.join(v_parts)}[vbase]")
            fc_bits.append(
                f"[{logo_in}:v]scale=iw*{sc}:-1,format=rgba,colorchannelmixer=aa={op}[lg]"
            )
            fc_bits.append(f"[vbase][lg]overlay={xy}[vout]")
            maps.extend(["-map", "[vout]"])
        else:
            fc_bits.append(f"[0:v]{','.join(v_parts)}[vout]")
            maps.extend(["-map", "[vout]"])

    # Audio: main [+ music bed]
    if use_music:
        mv = max(0.0, min(2.0, float(music_volume)))
        mfi = max(0.0, float(music_fade_in))
        mfo = max(0.0, float(music_fade_out))
        m_parts = [
            f"atrim=0:{out_dur:.4f}",
            "asetpts=PTS-STARTPTS",
            f"volume={mv}",
            f"apad=whole_dur={out_dur:.4f}",
        ]
        if mfi > 0.01:
            m_parts.append(f"afade=t=in:st=0:d={mfi:.3f}")
        if mfo > 0.01 and out_dur > mfo:
            m_parts.append(f"afade=t=out:st={max(0.0, out_dur - mfo):.3f}:d={mfo:.3f}")
        if music_duck and has_audio and not mute:
            # sidechaincompress: bed ducks under main dialogue energy
            fc_bits.append(f"[0:a]{','.join(a_parts)}[amain]")
            fc_bits.append(f"[{music_in}:a]{','.join(m_parts)}[amus]")
            fc_bits.append(
                "[amus][amain]sidechaincompress=threshold=0.05:ratio=6:attack=50:release=300[aduck]"
            )
            fc_bits.append("[amain][aduck]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        elif has_audio and not mute:
            fc_bits.append(f"[0:a]{','.join(a_parts)}[amain]")
            fc_bits.append(f"[{music_in}:a]{','.join(m_parts)}[amus]")
            fc_bits.append(
                "[amain][amus]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
            )
        else:
            # Music only (muted main or no main audio)
            fc_bits.append(f"[{music_in}:a]{','.join(m_parts)}[aout]")
        maps.extend(["-map", "[aout]"])
    elif has_audio:
        fc_bits.append(f"[0:a]{','.join(a_parts)}[aout]")
        maps.extend(["-map", "[aout]"])

    args_base: list[str] = ["-i", str(src)]
    if use_logo:
        args_base.extend(["-i", str(logo_path)])
    if use_music:
        args_base.extend(["-i", str(music_path)])
    args_base.extend(["-filter_complex", ";".join(fc_bits)])
    args_base.extend(maps)

    want_audio = has_audio or use_music
    audio_args: list[str] = []
    if want_audio:
        audio_args.extend(["-c:a", "aac", "-b:a", str(audio_bitrate)])
    tail = ["-movflags", "+faststart", "-t", f"{out_dur:.4f}", str(out)]

    if has_video:
        from sekiclip.media_ops.encode_util import run_encode_with_gpu_fallback

        run_encode_with_gpu_fallback(
            args_base,
            crf=int(crf),
            preset=str(preset),
            audio_args=audio_args if want_audio else ["-an"],
            tail_args=tail,
            prefer_gpu=bool(prefer_gpu),
            out=out,
            on_progress=on_progress,
            duration_hint=out_dur if on_progress else None,
        )
    else:
        args = [*args_base, *audio_args, *tail]
        run_ffmpeg(
            args,
            on_progress=on_progress,
            duration_hint=out_dur if on_progress else None,
        )
    return out


def assemble_shots(
    clips: list[Path | str],
    dest: Path | str,
    *,
    transition: str = "crossfade",
    transition_dur: float = 0.6,
    crf: int = 20,
    preset: str = "fast",
    audio_bitrate: str = "192k",
) -> Path:
    """Join clips with xfade transitions (or hard cut). Offline only."""
    from sekiclip.media_ops.film_fx import transition_name
    from sekiclip.media_ops.video import concat_videos

    paths = [Path(c) for c in clips if Path(c).is_file()]
    if len(paths) < 1:
        raise ValueError("Need at least one clip")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        run_ffmpeg(
            [
                "-i",
                str(paths[0]),
                "-c:v",
                "libx264",
                "-crf",
                str(crf),
                "-preset",
                preset,
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
        return dest

    tname = transition_name(transition)
    td = max(0.05, float(transition_dur))
    if tname == "cut":
        return concat_videos(paths, dest)

    # xfade chain (video). Audio: acrossfade when both have audio.
    # Normalize each to 1080p first for xfade compatibility.
    tmp_dir = dest.parent / f".sekiclip_asm_{dest.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    norms: list[Path] = []
    durs: list[float] = []
    try:
        for i, p in enumerate(paths):
            n = tmp_dir / f"n{i:02d}.mp4"
            run_ffmpeg(
                [
                    "-i",
                    str(p),
                    "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1,format=yuv420p",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "veryfast",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    str(n),
                ]
            )
            norms.append(n)
            durs.append(float((probe(n).get("format") or {}).get("duration") or 1.0))

        # Build xfade filter
        fc: list[str] = []
        # offset accumulates: offset_i = sum(d[0..i]) - td*(i)
        offset = 0.0
        prev = "[0:v]"
        for i in range(1, len(norms)):
            offset = sum(durs[:i]) - td * i
            offset = max(0.0, offset)
            out_lab = f"[vx{i}]" if i < len(norms) - 1 else "[vout]"
            fc.append(
                f"{prev}[{i}:v]xfade=transition={tname}:duration={td:.3f}:offset={offset:.3f}{out_lab}"
            )
            prev = out_lab
            # acrossfade audio if possible
        # Audio chain with acrossfade
        aprev = "[0:a]"
        for i in range(1, len(norms)):
            aout = f"[ax{i}]" if i < len(norms) - 1 else "[aout]"
            fc.append(f"{aprev}[{i}:a]acrossfade=d={td:.3f}{aout}")
            aprev = aout

        args: list[str] = []
        for n in norms:
            args.extend(["-i", str(n)])
        args.extend(
            [
                "-filter_complex",
                ";".join(fc),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-crf",
                str(crf),
                "-preset",
                preset,
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
        try:
            run_ffmpeg(args)
        except Exception:
            # Fallback hard concat if xfade fails (e.g. missing audio)
            warn("xfade assemble failed — falling back to hard concat.")
            return concat_videos(norms, dest)
        return dest
    finally:
        try:
            for p in tmp_dir.glob("*"):
                p.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass


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
    two_pass: bool = True,
) -> Path:
    """Bitrate-targeted encode aiming for max_mb (approximate, not exact).

    When ``two_pass`` is True (default), runs x264 2-pass for steadier quality
    under the size budget. Still not a hard size guarantee.
    """
    src = Path(src)
    out = _out(src, dest, ".mp4", f"under{int(max_mb)}mb")
    out.parent.mkdir(parents=True, exist_ok=True)

    data = probe(src)
    duration = float((data.get("format") or {}).get("duration") or 0)
    if duration <= 0.5:
        raise RuntimeError("Duration too short for size targeting")

    total_bits = max_mb * 1024 * 1024 * 8
    audio_bps = 128_000
    video_bps = max(200_000, int((total_bits * 0.9) / duration - audio_bps))
    video_bps = min(video_bps, 12_000_000)

    warn(
        f"Size target ~{max_mb} MB → ~{video_bps // 1000} kbps video "
        f"({'2-pass' if two_pass else '1-pass'}; approximate)."
    )

    if two_pass:
        import tempfile

        passlog = Path(tempfile.mkdtemp(prefix="sekiclip_2pass_")) / "ffmpeg2pass"
        try:
            run_ffmpeg(
                [
                    "-y",
                    "-i",
                    str(src),
                    "-c:v",
                    "libx264",
                    "-b:v",
                    str(video_bps),
                    "-pass",
                    "1",
                    "-passlogfile",
                    str(passlog),
                    "-an",
                    "-f",
                    "null",
                    "NUL" if __import__("sys").platform == "win32" else "/dev/null",
                ]
            )
            run_ffmpeg(
                [
                    "-i",
                    str(src),
                    "-c:v",
                    "libx264",
                    "-b:v",
                    str(video_bps),
                    "-pass",
                    "2",
                    "-passlogfile",
                    str(passlog),
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(out),
                ]
            )
        finally:
            # clean log files
            parent = passlog.parent
            try:
                for p in parent.glob("ffmpeg2pass*"):
                    p.unlink(missing_ok=True)
                parent.rmdir()
            except OSError:
                pass
        return out

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
