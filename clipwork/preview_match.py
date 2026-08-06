"""Cut timeline + look helpers so preview and export share one definition.

Best-practice model (NLE-style):
  - Source time  = position in the original file
  - Cut time     = t_source − In  (0 at In, length = Out − In) on the *source* axis
  - Output time  = cut_time / speed  (what the exported file runs as)
  - UI fade N    = N seconds on the *output* cut (last N seconds before Out)
  - Wall clock   = master for preview playhead; decoder follows it
  - One Look dict feeds both live preview and render_cut

Offline only — no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── speed / audio tempo (shared with export) ─────────────────


def atempo_chain(speed: float) -> list[str]:
    """ffmpeg atempo stack (each stage limited to 0.5–2.0)."""
    sp = max(0.25, min(4.0, float(speed)))
    if abs(sp - 1.0) <= 1e-3:
        return []
    parts: list[str] = []
    remaining = sp
    while remaining > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return parts


# ── cut timeline ─────────────────────────────────────────────


@dataclass(frozen=True)
class CutTimeline:
    """In→Out cut with optional speed. All fades are in *output* seconds."""

    in_point: float
    out_point: float
    speed: float = 1.0

    def __post_init__(self) -> None:
        inn = max(0.0, float(self.in_point))
        out = max(inn + 0.05, float(self.out_point))
        sp = max(0.25, min(4.0, float(self.speed)))
        object.__setattr__(self, "in_point", inn)
        object.__setattr__(self, "out_point", out)
        object.__setattr__(self, "speed", sp)

    @property
    def source_duration(self) -> float:
        """Length of the cut on the source timeline (Out − In)."""
        return self.out_point - self.in_point

    @property
    def output_duration(self) -> float:
        """Length of the exported clip after speed."""
        return self.source_duration / self.speed

    def contains_source(self, t_source: float) -> bool:
        return self.in_point - 1e-3 <= t_source <= self.out_point + 1e-3

    def source_to_output(self, t_source: float) -> float:
        """Map file time → output-cut time (0 at In, length = output_duration)."""
        return (float(t_source) - self.in_point) / self.speed

    def output_to_source(self, t_out: float) -> float:
        return self.in_point + float(t_out) * self.speed


def fit_fades(sel_dur: float, fade_in: float, fade_out: float) -> tuple[float, float]:
    """Honor UI fade seconds; only shrink if in+out would exceed the cut."""
    sel_dur = max(0.05, float(sel_dur))
    vfi = max(0.0, float(fade_in))
    vfo = max(0.0, float(fade_out))
    total = vfi + vfo
    if total > sel_dur * 0.98 and total > 1e-6:
        scale = (sel_dur * 0.98) / total
        vfi *= scale
        vfo *= scale
    return vfi, vfo


def fade_strength_on_output(
    t_output: float,
    output_duration: float,
    fade_in: float,
    fade_out: float,
) -> float:
    """0 = full picture/audio, 1 = black/silent on the *output* cut timeline.

    Fade-out starts at (output_duration − N) and finishes at output_duration.
    """
    out_dur = max(0.05, float(output_duration))
    t = float(t_output)
    vfi, vfo = fit_fades(out_dur, fade_in, fade_out)
    strength = 0.0
    if vfi > 1e-6 and t < vfi:
        strength = max(strength, 1.0 - max(0.0, min(1.0, t / vfi)))
    if vfo > 1e-6 and t > out_dur - vfo:
        into = t - (out_dur - vfo)
        strength = max(strength, max(0.0, min(1.0, into / vfo)))
    return strength


def fade_strength(
    t_rel: float,
    sel_dur: float,
    fade_in: float,
    fade_out: float,
) -> float:
    """Backward-compatible: t_rel/sel_dur treated as *output* cut time/length."""
    return fade_strength_on_output(t_rel, sel_dur, fade_in, fade_out)


def video_fade_strength_at_source(
    cut: CutTimeline,
    t_source: float,
    fade_in: float,
    fade_out: float,
) -> float:
    """Preview helper: playhead is source time; fades are UI output seconds."""
    if not cut.contains_source(t_source):
        return 0.0  # outside cut: caller may dim separately
    return fade_strength_on_output(
        cut.source_to_output(t_source),
        cut.output_duration,
        fade_in,
        fade_out,
    )


def build_audio_filter(
    *,
    start: float,
    end: float,
    cut: CutTimeline,
    volume: float = 1.0,
    mute: bool = False,
    audio_fade_in: float = 0.0,
    audio_fade_out: float = 0.0,
) -> str:
    """ffmpeg -af chain matching render_cut audio: atrim→asetpts→vol→atempo→afade.

    ``start``/``end`` are source times for this play window.
    Fade N is on the full cut's *output* timeline (same as export).
    """
    start = max(0.0, float(start))
    end = max(start + 0.05, float(end))
    speed = cut.speed
    src_len = end - start
    out_len = src_len / speed

    parts: list[str] = [
        f"atrim=start={start:.4f}:end={end:.4f}",
        "asetpts=PTS-STARTPTS",
    ]
    vol = 0.0 if mute else max(0.0, min(4.0, float(volume)))
    if abs(vol - 1.0) > 1e-3 or mute:
        parts.append(f"volume={vol:.4f}")
    parts.extend(atempo_chain(speed))

    out_sel = cut.output_duration
    afi, afo = fit_fades(out_sel, audio_fade_in, audio_fade_out)
    # Output-time offset of this window from cut start
    out_off = max(0.0, (start - cut.in_point) / speed) if start >= cut.in_point - 1e-6 else 0.0

    if afi > 0 and start <= cut.in_point + 0.05:
        d = min(afi, out_len * 0.98)
        if d > 0.02:
            parts.append(f"afade=t=in:st=0:d={d:.4f}:curve=tri")

    if afo > 0 and end >= cut.out_point - 0.05:
        d = min(afo, out_len * 0.98)
        # Export: fade starts at out_sel - d on full cut output clock
        st = (out_sel - d) - out_off
        if d > 0.02:
            if st >= out_len - 0.02:
                pass
            elif st <= 0:
                remaining = d + st
                if remaining > 0.02:
                    parts.append(f"afade=t=out:st=0:d={remaining:.4f}:curve=tri")
            else:
                parts.append(f"afade=t=out:st={st:.4f}:d={d:.4f}:curve=tri")

    return ",".join(parts)


def export_fade_filter_pairs(
    out_dur: float,
    video_fade_in: float,
    video_fade_out: float,
    audio_fade_in: float,
    audio_fade_out: float,
) -> tuple[list[str], list[str]]:
    """Return (video_fade_filters, audio_fade_filters) for render_cut on output clock."""
    vfi, vfo = fit_fades(out_dur, video_fade_in, video_fade_out)
    afi, afo = fit_fades(out_dur, audio_fade_in, audio_fade_out)
    v_bits: list[str] = []
    a_bits: list[str] = []
    if vfi > 0:
        v_bits.append(f"fade=t=in:st=0:d={vfi:.4f}")
    if vfo > 0:
        v_bits.append(f"fade=t=out:st={max(0.0, out_dur - vfo):.4f}:d={vfo:.4f}")
    if afi > 0:
        a_bits.append(f"afade=t=in:st=0:d={afi:.4f}:curve=tri")
    if afo > 0:
        a_bits.append(f"afade=t=out:st={max(0.0, out_dur - afo):.4f}:d={afo:.4f}:curve=tri")
    return v_bits, a_bits


# ── subtitles (preview burn-in) ──────────────────────────────


def parse_srt(path: Path | str) -> list[tuple[float, float, str]]:
    """Parse basic SRT → list of (start_s, end_s, text)."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", raw.strip())
    cues: list[tuple[float, float, str]] = []
    time_re = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
    )

    def to_sec(h: str, m: str, s: str, ms: str) -> float:
        ms = (ms + "000")[:3]
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        idx = 0
        if re.match(r"^\d+$", lines[0]):
            idx = 1
        if idx >= len(lines):
            continue
        m = time_re.search(lines[idx])
        if not m:
            continue
        start = to_sec(*m.groups()[0:4])
        end = to_sec(*m.groups()[4:8])
        text = "\n".join(lines[idx + 1 :]).replace("<br>", "\n")
        text = re.sub(r"<[^>]+>", "", text)
        if text.strip():
            cues.append((start, end, text.strip()))
    return cues


def active_subs(cues: list[tuple[float, float, str]], t: float) -> str:
    parts: list[str] = []
    for start, end, text in cues:
        if start <= t <= end:
            parts.append(text)
    return "\n".join(parts)


_srt_cache: dict[str, tuple[float, list[tuple[float, float, str]]]] = {}


def load_srt_cached(path: Path | str | None) -> list[tuple[float, float, str]]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    key = str(p.resolve())
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    hit = _srt_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    cues = parse_srt(p)
    _srt_cache[key] = (mtime, cues)
    return cues
