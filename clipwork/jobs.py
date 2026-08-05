"""Job runner and local job log (basenames only)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from clipwork import __version__
from clipwork.media_ops import take_warnings


@dataclass
class JobResult:
    op: str
    paths: list[Path]
    ok: bool
    duration_s: float
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    value: Any = None


def job_log_path() -> Path:
    from clipwork.prefs import user_data_dir

    return user_data_dir() / "clipwork_jobs.log"


def _basenames(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        try:
            out.append(Path(raw).name or raw)
        except Exception:  # noqa: BLE001
            out.append(str(raw))
    return out


def log_job(
    op: str,
    *,
    inputs: list[str],
    outputs: list[str],
    ok: bool,
    duration_s: float,
    error: str | None = None,
    warnings: list[str] | None = None,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": __version__,
        "op": op,
        "ok": ok,
        "duration_s": round(duration_s, 3),
        "inputs": _basenames(inputs),
        "outputs": _basenames(outputs),
        "warnings": warnings or [],
        "error": error,
    }
    try:
        with open(job_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_job(
    op: str,
    fn: Callable[[], Any],
    *,
    inputs: list[Path | str] | None = None,
) -> JobResult:
    take_warnings()
    t0 = time.perf_counter()
    in_paths = [str(p) for p in (inputs or [])]
    try:
        result = fn()
        ms = time.perf_counter() - t0
        paths: list[Path] = []
        if isinstance(result, Path):
            paths = [result]
        elif isinstance(result, (list, tuple)):
            paths = [Path(p) for p in result]
        warns = take_warnings()
        log_job(
            op,
            inputs=in_paths,
            outputs=[str(p) for p in paths],
            ok=True,
            duration_s=ms,
            warnings=warns,
        )
        return JobResult(op=op, paths=paths, ok=True, duration_s=ms, warnings=warns, value=result)
    except Exception as exc:  # noqa: BLE001
        ms = time.perf_counter() - t0
        warns = take_warnings()
        log_job(
            op,
            inputs=in_paths,
            outputs=[],
            ok=False,
            duration_s=ms,
            error=f"{type(exc).__name__}: {exc}",
            warnings=warns,
        )
        return JobResult(
            op=op,
            paths=[],
            ok=False,
            duration_s=ms,
            warnings=warns,
            error=f"{type(exc).__name__}: {exc}",
        )
