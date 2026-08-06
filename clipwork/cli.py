"""Clipwork command-line interface — offline, free forever."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from clipwork import __app_name__, __version__
from clipwork import media_ops as ops


def _out(path: str | None) -> Path | None:
    return Path(path) if path else None


def cmd_info(args: argparse.Namespace) -> int:
    for p in args.files:
        path = Path(p)
        print(ops.media_summary(path))
        if args.json:
            import json

            print(json.dumps(ops.probe(path), indent=2)[:4000])
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    src = Path(args.input)
    kind = args.kind
    fmt = args.format
    out = _out(args.output)
    if kind == "auto":
        if src.suffix.lower() in ops.IMAGE_EXTS:
            kind = "image"
        elif src.suffix.lower() in {
            ".mp3",
            ".wav",
            ".flac",
            ".m4a",
            ".ogg",
            ".aac",
        }:
            kind = "audio"
        else:
            kind = "video"
    if kind == "video":
        result = ops.convert_video(src, out, fmt=fmt or "mp4")
    elif kind == "audio":
        result = ops.convert_audio(src, out, fmt=fmt or "mp3")
    else:
        result = ops.convert_image(src, out, fmt=fmt or "png")
    print(result)
    return 0


def cmd_compress(args: argparse.Namespace) -> int:
    src = Path(args.input)
    out = _out(args.output)
    if src.suffix.lower() in ops.IMAGE_EXTS:
        result = ops.compress_image(src, out, quality=args.quality, max_edge=args.max_edge)
    elif src.suffix.lower() in {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}:
        result = ops.compress_audio(src, out, bitrate=args.bitrate)
    else:
        result = ops.compress_video(src, out, preset=args.preset)
    print(result)
    for w in ops.take_warnings():
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_trim(args: argparse.Namespace) -> int:
    result = ops.trim_media(
        args.input,
        _out(args.output),
        start=args.start,
        end=args.end,
        duration=args.duration,
        reencode=args.reencode,
    )
    print(result)
    for w in ops.take_warnings():
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_extract_audio(args: argparse.Namespace) -> int:
    print(ops.extract_audio(args.input, _out(args.output), fmt=args.format))
    return 0


def cmd_remux(args: argparse.Namespace) -> int:
    print(ops.remux(args.input, _out(args.output), fmt=args.format))
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if src.suffix.lower() in ops.IMAGE_EXTS:
        print(ops.rotate_image(src, _out(args.output), degrees=args.degrees))
    else:
        print(ops.rotate_video(src, _out(args.output), degrees=args.degrees))
    return 0


def cmd_frame(args: argparse.Namespace) -> int:
    print(ops.grab_frame(args.input, _out(args.output), time=args.time))
    return 0


def cmd_strip_audio(args: argparse.Namespace) -> int:
    print(ops.strip_audio(args.input, _out(args.output)))
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    print(ops.normalize_audio(args.input, _out(args.output)))
    return 0


def cmd_mono(args: argparse.Namespace) -> int:
    print(ops.to_mono(args.input, _out(args.output), rate=args.rate))
    return 0


def cmd_resize(args: argparse.Namespace) -> int:
    print(
        ops.resize_image(
            args.input,
            _out(args.output),
            max_edge=args.max_edge,
            width=args.width,
            height=args.height,
        )
    )
    return 0


def cmd_strip_exif(args: argparse.Namespace) -> int:
    print(ops.strip_exif(args.input, _out(args.output)))
    return 0


def cmd_images_pdf(args: argparse.Namespace) -> int:
    print(ops.images_to_pdf(args.inputs, args.output))
    return 0


def cmd_concat(args: argparse.Namespace) -> int:
    print(ops.concat_videos(args.inputs, args.output, reencode=not args.copy))
    for w in ops.take_warnings():
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from clipwork.app import main as gui_main

    gui_main()
    return 0


def _out(args: argparse.Namespace):
    return Path(args.output) if getattr(args, "output", None) else None


def cmd_crop(args: argparse.Namespace) -> int:
    print(
        ops.crop_video(
            args.input,
            _out(args),
            x=args.x,
            y=args.y,
            width=args.width,
            height=args.height,
            margin=args.margin,
        )
    )
    return 0


def cmd_volume(args: argparse.Namespace) -> int:
    print(
        ops.adjust_volume(
            args.input, _out(args), volume=args.volume, mute=bool(args.mute)
        )
    )
    return 0


def cmd_speed(args: argparse.Namespace) -> int:
    print(ops.change_speed(args.input, _out(args), speed=args.speed))
    return 0


def cmd_gif(args: argparse.Namespace) -> int:
    print(
        ops.export_gif(
            args.input,
            _out(args),
            start=float(args.start),
            end=float(args.end) if args.end else None,
            duration=float(args.duration) if args.duration else None,
            fps=args.fps,
            max_width=args.max_width,
            fmt=args.format,
        )
    )
    return 0


def cmd_fade(args: argparse.Namespace) -> int:
    print(
        ops.fade_media(
            args.input, _out(args), fade_in=args.fade_in, fade_out=args.fade_out
        )
    )
    return 0


def cmd_flip(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if src.suffix.lower() in ops.IMAGE_EXTS:
        print(ops.flip_image(src, _out(args), horizontal=not args.vertical))
    else:
        print(ops.flip_video(src, _out(args), horizontal=not args.vertical))
    return 0


def cmd_target_size(args: argparse.Namespace) -> int:
    print(ops.target_size_video(args.input, _out(args), max_mb=args.max_mb))
    for w in ops.take_warnings():
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_burn_subs(args: argparse.Namespace) -> int:
    print(ops.burn_subtitles(args.input, args.srt, _out(args)))
    return 0


def cmd_logo(args: argparse.Namespace) -> int:
    print(
        ops.logo_overlay(
            args.input,
            args.logo,
            _out(args),
            position=args.position,
            scale=args.scale,
            opacity=args.opacity,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipwork",
        description=f"{__app_name__} {__version__} — offline media toolkit (free forever).",
    )
    p.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("info", help="Show media summary")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("convert", help="Convert video/audio/image")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("-f", "--format", default=None)
    s.add_argument("-k", "--kind", choices=("auto", "video", "audio", "image"), default="auto")
    s.set_defaults(func=cmd_convert)

    s = sub.add_parser("compress", help="Compress with presets")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--preset", default="balanced", choices=list(ops.COMPRESS_PRESETS))
    s.add_argument("--bitrate", default="128k")
    s.add_argument("--quality", type=int, default=75)
    s.add_argument("--max-edge", type=int, default=1920)
    s.set_defaults(func=cmd_compress)

    s = sub.add_parser("trim", help="Trim media")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--start", default="0")
    s.add_argument("--end", default=None)
    s.add_argument("--duration", default=None)
    s.add_argument("--reencode", action="store_true")
    s.set_defaults(func=cmd_trim)

    s = sub.add_parser("extract-audio", help="Extract audio from video")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("-f", "--format", default="mp3")
    s.set_defaults(func=cmd_extract_audio)

    s = sub.add_parser("remux", help="Change container (stream copy)")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("-f", "--format", default="mp4")
    s.set_defaults(func=cmd_remux)

    s = sub.add_parser("rotate", help="Rotate video or image")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--degrees", type=int, default=90)
    s.set_defaults(func=cmd_rotate)

    s = sub.add_parser("frame", help="Grab a still frame from video")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--time", default="0")
    s.set_defaults(func=cmd_frame)

    s = sub.add_parser("strip-audio", help="Remove audio track from video")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_strip_audio)

    s = sub.add_parser("normalize", help="Loudness-normalize audio")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_normalize)

    s = sub.add_parser("mono", help="Convert audio to mono")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--rate", type=int, default=44100)
    s.set_defaults(func=cmd_mono)

    s = sub.add_parser("resize", help="Resize image")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--max-edge", type=int, default=None)
    s.add_argument("--width", type=int, default=None)
    s.add_argument("--height", type=int, default=None)
    s.set_defaults(func=cmd_resize)

    s = sub.add_parser("strip-exif", help="Strip image metadata")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_strip_exif)

    s = sub.add_parser("images-pdf", help="Combine images into one PDF")
    s.add_argument("inputs", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_images_pdf)

    s = sub.add_parser("concat", help="Concatenate videos")
    s.add_argument("inputs", nargs="+")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--copy", action="store_true", help="Stream copy (requires matching codecs)")
    s.set_defaults(func=cmd_concat)

    s = sub.add_parser("crop", help="Crop video (margin or x/y/w/h)")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--margin", type=int, default=0)
    s.add_argument("--x", type=int, default=0)
    s.add_argument("--y", type=int, default=0)
    s.add_argument("--width", type=int, default=None)
    s.add_argument("--height", type=int, default=None)
    s.set_defaults(func=cmd_crop)

    s = sub.add_parser("volume", help="Adjust volume or mute")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--volume", type=float, default=1.0)
    s.add_argument("--mute", action="store_true")
    s.set_defaults(func=cmd_volume)

    s = sub.add_parser("speed", help="Change playback speed")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--speed", type=float, default=1.5)
    s.set_defaults(func=cmd_speed)

    s = sub.add_parser("gif", help="Export GIF/WebP clip")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--start", default="0")
    s.add_argument("--end", default=None)
    s.add_argument("--duration", default=None)
    s.add_argument("--fps", type=int, default=12)
    s.add_argument("--max-width", type=int, default=480)
    s.add_argument("-f", "--format", default="gif", choices=("gif", "webp"))
    s.set_defaults(func=cmd_gif)

    s = sub.add_parser("fade", help="Fade in/out")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--fade-in", type=float, default=0.5)
    s.add_argument("--fade-out", type=float, default=0.5)
    s.set_defaults(func=cmd_fade)

    s = sub.add_parser("flip", help="Flip video or image")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--vertical", action="store_true")
    s.set_defaults(func=cmd_flip)

    s = sub.add_parser("target-size", help="Approximate max file size (MB)")
    s.add_argument("input")
    s.add_argument("-o", "--output")
    s.add_argument("--max-mb", type=float, default=25.0)
    s.set_defaults(func=cmd_target_size)

    s = sub.add_parser("burn-subs", help="Burn SRT subtitles into video")
    s.add_argument("input")
    s.add_argument("srt")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_burn_subs)

    s = sub.add_parser("logo", help="Overlay logo image on video")
    s.add_argument("input")
    s.add_argument("logo")
    s.add_argument("-o", "--output")
    s.add_argument("--position", default="top-right")
    s.add_argument("--scale", type=float, default=0.15)
    s.add_argument("--opacity", type=float, default=1.0)
    s.set_defaults(func=cmd_logo)

    s = sub.add_parser("gui", help="Open the GUI")
    s.set_defaults(func=cmd_gui)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return int(args.func(args) or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
