# SPDX-License-Identifier: MIT
"""A ``wait=false`` generation actually runs, and can be read back and cancelled (#1097).

Before the fix, ``wait=false`` saved the job and returned ``pending``. No shipped
command ever ran it, ``tasks/get`` said ``working`` forever, and ``tasks/cancel`` wrote
``failed`` without stopping anything. These tests drive the real tool,
``FlowWorker.process_task`` and its ``CancelledError`` handler. Only ``FlowApiClient``
is faked, so no browser starts.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CancelTaskRequestParams, GetTaskPayloadRequestParams
from structlog.testing import capture_logs

from gflow_cli.data.store import DataStore
from gflow_cli.mcp import tasks_extension, tools
from gflow_cli.mcp.tasks_extension import TasksExtension
from gflow_cli.worker.queue import QueueRepository, make_checkpoint_document

from .test_tools_wired import _FakeFlowApiClient  # pyright: ignore[reportPrivateUsage]


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[DataStore]:
    store = DataStore.open(tmp_path / "gflow.db")
    store.conn.execute(
        "INSERT INTO profiles(name, profile_dir, first_seen_at) "
        "VALUES ('default', '/profiles/default', '2026-10-25T00:00:00Z')"
    )
    store.conn.commit()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def wired(db: DataStore, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """Route the tool and the worker at ``db``; ``client`` is the fake Flow client."""
    client = _FakeFlowApiClient()
    with (
        patch("gflow_cli.mcp.tools._resolve_and_validate_profile", return_value="default"),
        patch("gflow_cli.worker.daemon.FlowApiClient", return_value=client),
        patch(
            "gflow_cli.mcp.tools.get_settings",
            return_value=MagicMock(
                resolved_db_path=lambda: db.path,
                profile_subdir=lambda _: tmp_path / "profile_default",
                timeout_seconds=30,
            ),
        ),
        patch(
            "gflow_cli.worker.daemon.get_settings",
            return_value=MagicMock(
                profile_subdir=lambda _: tmp_path / "profile_default",
                headless=True,
                transport=None,
                output_dir=tmp_path / "out",
            ),
        ),
    ):
        yield {"client": client}
    tasks_extension.DETACHED.clear()


def _status(db: DataStore, task_id: str) -> str:
    task = QueueRepository(db).get_task(task_id)
    assert task is not None
    return task.status


async def _finish(task_id: str) -> None:
    task = tasks_extension.DETACHED.get(task_id)
    if task is not None:
        await asyncio.wait({task}, timeout=5)


def _hang_after_submit(db: DataStore) -> AsyncMock:
    """A generate call that records a submit, then never returns."""

    async def hang(*_: Any, **__: Any) -> Any:
        repo = QueueRepository(db)
        (task_id,) = [t for t in tasks_extension.DETACHED]
        repo.write_checkpoint(
            task_id,
            make_checkpoint_document(
                claimant="mcp:default",
                phase="submit_attempted",
                may_have_spent=True,
                project_id="proj-abc",
            ),
        )
        await asyncio.Event().wait()

    return AsyncMock(side_effect=hang)


async def test_a_wait_false_job_runs_to_completion(db: DataStore, wired: dict[str, Any]) -> None:
    reply = await tools.gflow_generate_image(prompt="a lighthouse", wait=False)

    assert reply["status"] == "pending"
    await _finish(reply["task_id"])
    assert _status(db, reply["task_id"]) == "completed"
    assert reply["task_id"] not in tasks_extension.DETACHED


async def test_an_old_pending_row_is_never_claimed(db: DataStore, wired: dict[str, Any]) -> None:
    """Rows queued by the broken releases must not start billing after an upgrade."""
    QueueRepository(db).enqueue_task(
        task_id="old-row", profile_name="default", task_type="t2i", payload={"prompt": "old"}
    )

    reply = await tools.gflow_generate_image(prompt="new", wait=False)
    await _finish(reply["task_id"])

    assert _status(db, "old-row") == "pending"


@pytest.mark.parametrize("entry", ["run_stdio", "run_http", "run_sse"])
def test_no_entry_point_recovers_processing_rows(entry: str) -> None:
    """A second stdio server must not fail the first one's live job (predict STOP)."""
    from gflow_cli.mcp import server

    assert "recover_processing" not in inspect.getsource(getattr(server, entry))


async def test_a_detached_job_and_a_waiting_call_share_the_profile(
    db: DataStore, wired: dict[str, Any]
) -> None:
    active = peak = 0

    image = wired["client"].generate_image.return_value
    calls = 0

    async def held(*_: Any, **__: Any) -> Any:
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        # A media id seen twice trips the attribution guard, so each job gets its own.
        return replace(image, media_name=f"media-{calls}")

    async def held_batch(*a: Any, **k: Any) -> Any:
        return [await held(*a, **k)]

    wired["client"].generate_images_batch.side_effect = held_batch
    wired["client"].generate_image.side_effect = held

    queued = await tools.gflow_generate_image(prompt="a", wait=False)
    waited = await tools.gflow_generate_image(prompt="b")
    await _finish(queued["task_id"])

    assert peak == 1
    assert _status(db, queued["task_id"]) == "completed"
    assert waited["status"] == "completed", waited


async def test_a_detached_crash_is_logged_and_fails_the_row(
    db: DataStore, wired: dict[str, Any]
) -> None:
    with (
        patch("gflow_cli.mcp.tools.FlowWorker", side_effect=RuntimeError("boom")),
        capture_logs() as logs,
    ):
        reply = await tools.gflow_generate_image(prompt="x", wait=False)
        await _finish(reply["task_id"])

    assert _status(db, reply["task_id"]) == "failed"
    assert reply["task_id"] not in tasks_extension.DETACHED
    events = [e for e in logs if e["event"] == "mcp.task.detached_failed"]
    assert events and events[0]["error_class"] == "RuntimeError"
    assert "payload" not in events[0]


async def test_cancel_after_submit_leaves_the_row_indeterminate(
    db: DataStore, wired: dict[str, Any]
) -> None:
    """A credit may already be spent, so the row must never read as a clean failure."""
    wired["client"].generate_images_batch = _hang_after_submit(db)
    wired["client"].generate_image = _hang_after_submit(db)
    reply = await tools.gflow_generate_image(prompt="x", wait=False)
    await asyncio.sleep(0.2)

    result = await TasksExtension(data_store=db)._handle_cancel_task(  # pyright: ignore[reportPrivateUsage]
        None, CancelTaskRequestParams(task_id=reply["task_id"])
    )

    assert _status(db, reply["task_id"]) == "indeterminate"
    assert result.status == "cancelled"
    assert reply["task_id"] not in tasks_extension.DETACHED


async def test_cancel_of_a_row_nobody_runs_fails_only_a_pending_row(db: DataStore) -> None:
    repo = QueueRepository(db)
    repo.enqueue_task(task_id="p", profile_name="default", task_type="t2i", payload={})
    repo.enqueue_task(task_id="c", profile_name="default", task_type="t2i", payload={})
    repo.update_task_status("c", "completed")
    ext = TasksExtension(data_store=db)

    pending = await ext._handle_cancel_task(None, CancelTaskRequestParams(task_id="p"))  # pyright: ignore[reportPrivateUsage]
    done = await ext._handle_cancel_task(None, CancelTaskRequestParams(task_id="c"))  # pyright: ignore[reportPrivateUsage]

    assert (_status(db, "p"), pending.status) == ("failed", "cancelled")
    assert (_status(db, "c"), done.status) == ("completed", "completed")


async def test_tasks_result_returns_what_wait_true_returns(
    db: DataStore, wired: dict[str, Any]
) -> None:
    reply = await tools.gflow_generate_image(prompt="a lighthouse", wait=False)
    await _finish(reply["task_id"])

    result = await TasksExtension(data_store=db)._handle_task_result(  # pyright: ignore[reportPrivateUsage]
        None, GetTaskPayloadRequestParams(task_id=reply["task_id"])
    )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["task_id"] == reply["task_id"]
    assert payload["files"]


async def test_tasks_result_of_an_unfinished_or_unknown_task_is_an_error(db: DataStore) -> None:
    QueueRepository(db).enqueue_task(
        task_id="busy", profile_name="default", task_type="t2i", payload={}
    )
    ext = TasksExtension(data_store=db)

    busy = await ext._handle_task_result(None, GetTaskPayloadRequestParams(task_id="busy"))  # pyright: ignore[reportPrivateUsage]
    assert busy.is_error
    with pytest.raises(KeyError, match="Task not found"):
        await ext._handle_task_result(None, GetTaskPayloadRequestParams(task_id="nope"))  # pyright: ignore[reportPrivateUsage]


async def test_draining_on_shutdown_classifies_a_running_job(
    db: DataStore, wired: dict[str, Any]
) -> None:
    """uvicorn re-raises the stop signal, so the HTTP lifespan must drain (spike 2026-10-25)."""
    wired["client"].generate_images_batch = _hang_after_submit(db)
    wired["client"].generate_image = _hang_after_submit(db)
    reply = await tools.gflow_generate_image(prompt="x", wait=False)
    await asyncio.sleep(0.2)

    await tasks_extension.drain_detached()

    assert _status(db, reply["task_id"]) == "indeterminate"


async def test_the_http_app_drains_detached_tasks_on_shutdown() -> None:
    from gflow_cli.mcp.server import build_app

    app = build_app(transport="http")
    with patch("gflow_cli.mcp.tasks_extension.drain_detached", new=AsyncMock()) as drain:
        async with app.router.lifespan_context(app):
            pass

    drain.assert_awaited_once()
