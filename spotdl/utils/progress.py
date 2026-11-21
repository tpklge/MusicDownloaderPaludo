"""Render terminal progress in batches without intermediate worker redraws."""

import math
from typing import Any, Optional

from rich.progress import BarColumn, Progress, Task
from rich.text import Text

__all__ = ["BatchedProgress", "StaticBarColumn", "draw_progress"]


class StaticBarColumn(BarColumn):
    """Use a static marker for service/discovery rows instead of pulsing bars."""

    def render(self, task: Task):
        """Draw a normal bar for known totals and a static service marker otherwise."""
        if task.total is None:
            return Text("—", style="dim")
        return super().render(task)


class BatchedProgress(Progress):
    """Only draw committed frames, at most four times per second when changed.

    Rich's add_task/reset call refresh synchronously. Defer those calls until
    draw() so twelve workers can change together in one frame. Idle frames only
    change when a displayed clock or countdown advances by a whole second.
    """

    def __init__(self, *columns, **kwargs: Any) -> None:
        kwargs["auto_refresh"] = False
        kwargs.setdefault("refresh_per_second", 4)
        super().__init__(*columns, **kwargs)
        self._frame_interval = 1 / self.live.refresh_per_second
        self._last_frame_check = -math.inf
        self._displayed_state: Optional[Any] = None

    def refresh(self) -> None:
        """Defer Rich's implicit refreshes until the coordinator commits a frame."""

    def draw(self, force: bool = False) -> None:
        """Render the final state of a batch, omitting unchanged frames."""
        if self.disable or not self.live.is_started:
            return
        now = self.get_time()
        if not force and now - self._last_frame_check < self._frame_interval:
            return
        self._last_frame_check = now
        size = self.console.size
        state = (
            size,
            [
                (
                    task.id,
                    task.description,
                    task.total,
                    task.completed,
                    task.visible,
                    dict(task.fields),
                    int((task.finished_time if task.finished else task.elapsed) or 0),
                    task.time_remaining,
                )
                for task in self.tasks
            ],
        )
        if not force and state == self._displayed_state:
            return

        # DEC synchronized output lets compatible terminals show a complete
        # frame without displaying Rich's erase/redraw steps in between.
        synchronized = (
            self.console.is_terminal
            and not self.console.is_dumb_terminal
            and not self.console.is_jupyter
            and not self.console.legacy_windows
        )
        stream = self.console.file
        if synchronized:
            stream.write("\033[?2026h")
        try:
            super().refresh()
        finally:
            if synchronized:
                stream.write("\033[?2026l")
                stream.flush()
        self._displayed_state = state


def draw_progress(progress: Progress, force: bool = False) -> None:
    """Commit batched UI frames while accepting existing Rich Progress callers."""
    if isinstance(progress, BatchedProgress):
        progress.draw(force=force)
    elif force:
        progress.refresh()
