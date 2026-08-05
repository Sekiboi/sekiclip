"""Launch Clipwork from the project root: python run.py"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _crash_log_path() -> Path:
    try:
        from clipwork.diagnostics import crash_log_path

        return crash_log_path()
    except Exception:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent / "clipwork_crash.log"
        return Path(__file__).resolve().parent / "clipwork_crash.log"


def main() -> None:
    try:
        from clipwork.app import main as app_main

        app_main()
    except Exception as exc:
        path = _crash_log_path()
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            path = Path.cwd() / "clipwork_crash.log"
            path.write_text(text, encoding="utf-8")
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Clipwork", f"Failed to start:\n{exc}\n\nLog:\n{path}")
            root.destroy()
        except Exception:
            print(f"Clipwork failed: {exc}\nLog: {path}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
