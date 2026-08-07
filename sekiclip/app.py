"""Sekiclip GUI entry — offline media editor. Free forever."""

from __future__ import annotations

import traceback
from pathlib import Path

from sekiclip.ui.main_window import SekiclipApp

__all__ = ["SekiclipApp", "main"]


def main() -> None:
    try:
        app = SekiclipApp()
        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        try:
            from sekiclip.core.diagnostics import crash_log_path

            path = crash_log_path()
        except Exception:
            path = Path.cwd() / "sekiclip_crash.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                encoding="utf-8",
            )
        except OSError:
            path = Path.cwd() / "sekiclip_crash.log"
            try:
                path.write_text(str(exc), encoding="utf-8")
            except OSError:
                pass
        raise


if __name__ == "__main__":
    main()
