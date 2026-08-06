"""Batch run one op over many files into an output folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

def batch_to_folder(
    sources: list[Path | str],
    out_dir: Path | str,
    *,
    op_name: str,
    run_one: Callable[[Path, Path], Path],
    suffix: str | None = None,
    name_tag: str = "out",
    overwrite: bool = True,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Run run_one(src, dest) for each source.
    dest is out_dir / f"{stem}_{name_tag}{suffix or src.suffix}".
    When overwrite is True (default), re-runs replace same-named outputs.
    Returns list of {src, dest, ok, error}.
    """
    from clipwork.media_ops.ffmpeg_util import unique_path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    total = len(sources)
    for i, raw in enumerate(sources, 1):
        src = Path(raw)
        if not src.is_file():
            results.append(
                {"src": str(src), "dest": None, "ok": False, "error": "not a file"}
            )
            if on_progress:
                on_progress(i, total, src.name)
            continue
        ext = suffix if suffix is not None else (src.suffix or ".mp4")
        if not ext.startswith("."):
            ext = f".{ext}"
        candidate = out_dir / f"{src.stem}_{name_tag}{ext}"
        dest = candidate if overwrite else unique_path(candidate)
        try:
            if overwrite and dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            got = run_one(src, dest)
            results.append(
                {
                    "src": str(src),
                    "dest": str(got),
                    "ok": True,
                    "error": None,
                    "op": op_name,
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "src": str(src),
                    "dest": str(dest),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "op": op_name,
                }
            )
        if on_progress:
            on_progress(i, total, src.name)
    return results
