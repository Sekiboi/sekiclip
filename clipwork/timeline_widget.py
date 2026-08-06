"""Visual range timeline: In / playhead / Out handles + selection fill + zoom."""

from __future__ import annotations

import tkinter as tk
from typing import Callable


class RangeTimeline(tk.Canvas):
    """
    Horizontal timeline with:
    - shaded In–Out selection
    - draggable In (green), Out (red), playhead (yellow)
    - click empty area to scrub playhead
    - zoom via set_view(start, end) in seconds
    """

    PAD = 10
    H = 56

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_change: Callable[[float, float, float], None] | None = None,
        on_seek: Callable[[float], None] | None = None,
        on_seek_end: Callable[[float], None] | None = None,
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("height", self.H)
        kwargs.setdefault("bg", "#1a1a1e")
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)  # type: ignore[arg-type]
        self.on_change = on_change
        self.on_seek = on_seek
        self.on_seek_end = on_seek_end
        self.duration = 1.0
        self.in_t = 0.0
        self.out_t = 1.0
        self.pos = 0.0
        # Visible window (zoom)
        self.view_start = 0.0
        self.view_end = 1.0
        self._drag: str | None = None  # "in" | "out" | "pos" | "range"
        self._drag_offset = 0.0
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double)

    def set_duration(self, duration: float) -> None:
        self.duration = max(0.001, float(duration))
        self.view_start = 0.0
        self.view_end = self.duration
        self.in_t = 0.0
        self.out_t = self.duration
        self.pos = 0.0
        self.redraw()

    def set_range(self, in_t: float, out_t: float, pos: float | None = None) -> None:
        self.in_t = max(0.0, min(float(in_t), self.duration))
        self.out_t = max(self.in_t + 0.01, min(float(out_t), self.duration))
        if pos is not None:
            self.pos = max(0.0, min(float(pos), self.duration))
        self.redraw()

    def set_position(self, pos: float) -> None:
        self.pos = max(0.0, min(float(pos), self.duration))
        self.redraw()

    def set_view(self, start: float, end: float) -> None:
        start = max(0.0, float(start))
        end = min(self.duration, max(start + 0.05, float(end)))
        self.view_start = start
        self.view_end = end
        self.redraw()

    def zoom(self, factor: float, center: float | None = None) -> None:
        """factor < 1 zooms in. center = time under cursor or playhead."""
        c = self.pos if center is None else float(center)
        span = self.view_end - self.view_start
        new_span = max(0.05, min(self.duration, span * factor))
        left = c - new_span * ((c - self.view_start) / span if span > 0 else 0.5)
        left = max(0.0, min(left, self.duration - new_span))
        self.view_start = left
        self.view_end = left + new_span
        self.redraw()

    def zoom_fit(self) -> None:
        self.view_start = 0.0
        self.view_end = self.duration
        self.redraw()

    def zoom_selection(self) -> None:
        pad = max(0.05, (self.out_t - self.in_t) * 0.1)
        self.view_start = max(0.0, self.in_t - pad)
        self.view_end = min(self.duration, self.out_t + pad)
        if self.view_end - self.view_start < 0.05:
            self.view_end = min(self.duration, self.view_start + 0.05)
        self.redraw()

    def _span(self) -> float:
        return max(0.001, self.view_end - self.view_start)

    def _x_to_t(self, x: float) -> float:
        w = max(1, self.winfo_width() - 2 * self.PAD)
        rel = (x - self.PAD) / w
        rel = max(0.0, min(1.0, rel))
        return self.view_start + rel * self._span()

    def _t_to_x(self, t: float) -> float:
        w = max(1, self.winfo_width() - 2 * self.PAD)
        rel = (t - self.view_start) / self._span()
        return self.PAD + rel * w

    def redraw(self) -> None:
        self.delete("all")
        w = max(self.winfo_width(), 100)
        h = self.H
        # Track
        self.create_rectangle(
            self.PAD, h // 2 - 6, w - self.PAD, h // 2 + 6, fill="#33333a", outline=""
        )
        # Selection
        x0 = self._t_to_x(self.in_t)
        x1 = self._t_to_x(self.out_t)
        if x1 > self.PAD and x0 < w - self.PAD:
            self.create_rectangle(
                max(self.PAD, x0),
                h // 2 - 6,
                min(w - self.PAD, x1),
                h // 2 + 6,
                fill="#2563eb",
                outline="",
            )
        # In handle (green)
        self._draw_handle(self.in_t, "#22c55e", "in")
        # Out handle (red)
        self._draw_handle(self.out_t, "#ef4444", "out")
        # Playhead (yellow)
        xp = self._t_to_x(self.pos)
        self.create_line(xp, 4, xp, h - 4, fill="#facc15", width=2, tags=("pos", "handle"))
        self.create_polygon(
            xp - 6,
            4,
            xp + 6,
            4,
            xp,
            14,
            fill="#facc15",
            outline="",
            tags=("pos", "handle"),
        )

    def _draw_handle(self, t: float, color: str, tag: str) -> None:
        x = self._t_to_x(t)
        h = self.H
        self.create_line(x, 8, x, h - 8, fill=color, width=3, tags=(tag, "handle"))
        self.create_rectangle(
            x - 5, h // 2 - 12, x + 5, h // 2 + 12, fill=color, outline="#111", tags=(tag, "handle")
        )

    def _hit(self, x: float) -> str | None:
        # Prefer handles near click
        for name, t in (("pos", self.pos), ("in", self.in_t), ("out", self.out_t)):
            if abs(self._t_to_x(t) - x) <= 10:
                return name
        # Inside selection = drag whole range
        if self._t_to_x(self.in_t) <= x <= self._t_to_x(self.out_t):
            return "range"
        return "pos"

    def _press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._drag = self._hit(event.x)
        t = self._x_to_t(event.x)
        if self._drag == "range":
            self._drag_offset = t - self.in_t
        elif self._drag == "pos" and abs(self._t_to_x(self.pos) - event.x) > 10:
            # Click empty-ish → seek
            self.pos = t
            self.redraw()
            if self.on_seek:
                self.on_seek(self.pos)
            if self.on_change:
                self.on_change(self.in_t, self.out_t, self.pos)

    def _motion(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if not self._drag:
            return
        t = self._x_to_t(event.x)
        if self._drag == "in":
            self.in_t = max(0.0, min(t, self.out_t - 0.01))
            if self.pos < self.in_t:
                self.pos = self.in_t
        elif self._drag == "out":
            self.out_t = min(self.duration, max(t, self.in_t + 0.01))
            if self.pos > self.out_t:
                self.pos = self.out_t
        elif self._drag == "pos":
            self.pos = max(0.0, min(t, self.duration))
        elif self._drag == "range":
            span = self.out_t - self.in_t
            new_in = t - self._drag_offset
            new_in = max(0.0, min(new_in, self.duration - span))
            self.in_t = new_in
            self.out_t = new_in + span
        self.redraw()
        if self.on_change:
            self.on_change(self.in_t, self.out_t, self.pos)
        if self._drag == "pos" and self.on_seek:
            self.on_seek(self.pos)

    def _release(self, _event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._drag in ("in", "out", "pos", "range"):
            if self.on_seek:
                self.on_seek(self.pos)
            if self.on_seek_end:
                self.on_seek_end(self.pos)
        self._drag = None

    def _double(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        # Double-click: set playhead and expand selection slightly around it
        t = self._x_to_t(event.x)
        self.pos = t
        self.redraw()
        if self.on_seek:
            self.on_seek(self.pos)
