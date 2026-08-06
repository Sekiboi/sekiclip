"""Helpers so live preview matches export (filters / timing). Offline only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def atempo_chain(speed: float) -> list[str]:
    """Same atempo stacking as export (ffmpeg atempo is limited to 0.5–2.0)."""
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


def parse_srt(path: Path | str) -> list[tuple[float, float, str]]:
    """Parse basic SRT → list of (start_s, end_s, text). Best-effort, no styling."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Normalize newlines
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
        # First line may be index
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
    """Return subtitle text active at timeline time t (source timeline)."""
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


def fade_strength(
    t_rel: float,
    sel_dur: float,
    fade_in: float,
    fade_out: float,
) -> float:
    """0 = full picture, 1 = black. Same clamp as export (each ≤ 49% of selection)."""
    sel_dur = max(0.05, float(sel_dur))
    max_each = max(0.05, sel_dur * 0.49)
    vfi = min(max(0.0, float(fade_in)), max_each) if fade_in > 0 else 0.0
    vfo = min(max(0.0, float(fade_out)), max_each) if fade_out > 0 else 0.0
    strength = 0.0
    if vfi > 0 and t_rel < vfi:
        strength = max(strength, 1.0 - max(0.0, min(1.0, t_rel / max(vfi, 1e-6))))
    if vfo > 0 and t_rel > sel_dur - vfo:
        into = t_rel - (sel_dur - vfo)
        strength = max(strength, max(0.0, min(1.0, into / max(vfo, 1e-6))))
    return strength
