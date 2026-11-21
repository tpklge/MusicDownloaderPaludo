"""Check that worker updates become bounded, complete terminal frames."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.progress import TextColumn

from spotdl.utils.progress import BatchedProgress, StaticBarColumn

BEGIN_FRAME = "\033[?2026h"
END_FRAME = "\033[?2026l"


def make_progress(now, terminal=True):
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=terminal,
        color_system=None,
        width=120,
        _environ={"TERM": "xterm-256color"},
    )
    progress = BatchedProgress(
        TextColumn("{task.description}"),
        StaticBarColumn(),
        console=console,
        get_time=lambda: now[0],
        redirect_stdout=False,
        redirect_stderr=False,
    )
    return progress, stream


def test_twelve_worker_resets_are_committed_as_one_frame():
    now = [0.0]
    progress, stream = make_progress(now)
    with progress:
        workers = [progress.add_task(f"W{index}") for index in range(12)]
        assert not progress.live.auto_refresh
        assert progress.live._refresh_thread is None
        assert BEGIN_FRAME not in stream.getvalue()
        progress.draw()
        assert stream.getvalue().count(BEGIN_FRAME) == 1
        for index, task in enumerate(workers):
            progress.reset(task, description=f"W{index}: nova música", completed=42)
        assert stream.getvalue().count(BEGIN_FRAME) == 1
        now[0] = 0.1
        progress.draw()
        assert stream.getvalue().count(BEGIN_FRAME) == 1
        now[0] = 0.25
        progress.draw()
        frame = stream.getvalue().split(BEGIN_FRAME)[-1]
        assert all(f"W{index}: nova música" in frame for index in range(12))
        assert stream.getvalue().count(BEGIN_FRAME) == 2
        assert stream.getvalue().count(END_FRAME) == 2


def test_repeated_and_subsecond_idle_frames_do_not_write_to_terminal():
    now = [0.0]
    progress, stream = make_progress(now)
    with progress:
        task = progress.add_task("Worker", completed=5)
        progress.draw()
        original = stream.getvalue()
        for moment in (0.1, 0.25, 0.5, 0.75):
            now[0] = moment
            progress.update(task, completed=5)
            progress.draw()
        assert stream.getvalue() == original
        now[0] = 1.0
        progress.draw()
        assert stream.getvalue().count(BEGIN_FRAME) == 2


def test_final_completion_can_render_before_next_frame_interval():
    now = [0.0]
    progress, stream = make_progress(now)
    with progress:
        task = progress.add_task("Worker", completed=5)
        progress.draw()
        progress.update(task, completed=100, description="Finalizado")
        progress.draw(force=True)
        assert "Finalizado" in stream.getvalue().split(BEGIN_FRAME)[-1]
        assert stream.getvalue().count(BEGIN_FRAME) == 2


def test_synchronized_output_is_closed_even_when_rendering_fails():
    now = [0.0]
    progress, stream = make_progress(now)
    with progress:
        progress.add_task("Worker")
        with patch.object(
            progress.live, "refresh", side_effect=RuntimeError("render failure")
        ):
            with pytest.raises(RuntimeError, match="render failure"):
                progress.draw()
        assert stream.getvalue().endswith(END_FRAME)


def test_nonterminal_output_does_not_emit_synchronized_terminal_codes():
    progress, stream = make_progress([0.0], terminal=False)
    with progress:
        progress.add_task("Worker")
        progress.draw(force=True)
    assert "Worker" in stream.getvalue()
    assert BEGIN_FRAME not in stream.getvalue()
    assert END_FRAME not in stream.getvalue()


def test_service_markers_do_not_animate_as_time_advances():
    now = [0.0]
    progress, _ = make_progress(now)
    task_id = progress.add_task("YouTube", total=None)
    task = progress.tasks[0]
    column = StaticBarColumn()
    first = column.render(task)
    now[0] = 10.0
    assert column.render(task) == first
    assert first.plain == "—"
    assert task.id == task_id
