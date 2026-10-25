"""MCP Tasks extension implementation (SEP-2663) for gflow-cli."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    CancelTaskRequestParams,
    CancelTaskResult,
    GetTaskPayloadRequestParams,
    GetTaskRequestParams,
    GetTaskResult,
    TaskStatus,
    TextContent,
)

from gflow_cli.config import get_settings
from gflow_cli.data.store import DataStore
from gflow_cli.worker.queue import QueueRepository, QueueTask

log = structlog.get_logger()

# ``wait=false`` jobs running in this server's loop, by task id (#1097). The strong
# reference is what keeps asyncio from collecting a task mid-run.
DETACHED: dict[str, asyncio.Task[None]] = {}

_CANCEL_WAIT_S = 10.0


def detach(task_id: str, job: Coroutine[Any, Any, None], settle: Callable[[], None]) -> None:
    """Run ``job`` in the background and track it until it finishes.

    ``settle`` runs when the job is cancelled, and must be idempotent: a cancel that
    lands before the job's first step skips the job's own handlers entirely.
    """
    task = asyncio.create_task(job)
    DETACHED[task_id] = task

    def done(t: asyncio.Task[None]) -> None:
        DETACHED.pop(task_id, None)
        if t.cancelled():
            settle()

    task.add_done_callback(done)


async def drain_detached() -> None:
    """Cancel every running job and wait for each to record how far it got.

    Both servers call this before ``asyncio.run`` tears down. Its blanket cancel would
    also cancel Playwright's connection, so a job's browser could not close; and on HTTP,
    uvicorn re-raises the stop signal and kills the process before that point anyway
    (spike 2026-10-25-mcp-detached-task-shutdown).
    """
    tasks = list(DETACHED.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=_CANCEL_WAIT_S)


def _mcp_status(task: QueueTask) -> TaskStatus:
    if task.status == "completed":
        return "completed"
    if task.status in ("failed", "indeterminate"):
        return "cancelled" if (task.error or {}).get("reason") == "cancelled" else "failed"
    return "working"


def _status_message(task: QueueTask) -> str | None:
    if task.status == "indeterminate":
        return "Stopped after submit; a credit may have been spent. Check the Flow project."
    return None


class TasksExtension(Extension):
    """Extension subclass handling SEP-2663 tasks/get, tasks/result and tasks/cancel."""

    identifier = "io.modelcontextprotocol/tasks"

    def __init__(self, data_store: DataStore | None = None) -> None:
        self._data_store = data_store

    def set_data_store(self, data_store: DataStore) -> None:
        self._data_store = data_store

    def _get_store(self) -> DataStore:
        if self._data_store is None:
            self._data_store = DataStore.open(get_settings().resolved_db_path())
        return self._data_store

    def _get_task(self, task_id: str) -> QueueTask:
        queue_task = QueueRepository(self._get_store()).get_task(task_id)
        if queue_task is None:
            raise MCPError(INVALID_PARAMS, f"Task not found: {task_id}")
        return queue_task

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding(
                method="tasks/get",
                params_type=GetTaskRequestParams,
                handler=self._handle_get_task,
            ),
            MethodBinding(
                method="tasks/result",
                params_type=GetTaskPayloadRequestParams,
                handler=self._handle_task_result,
            ),
            MethodBinding(
                method="tasks/cancel",
                params_type=CancelTaskRequestParams,
                handler=self._handle_cancel_task,
            ),
        ]

    async def _handle_get_task(self, context: Any, params: GetTaskRequestParams) -> GetTaskResult:
        queue_task = self._get_task(params.task_id)
        return GetTaskResult(
            task_id=queue_task.task_id,
            status=_mcp_status(queue_task),
            status_message=_status_message(queue_task),
            created_at=queue_task.created_at or "",
            last_updated_at=queue_task.updated_at or "",
            ttl=3600,
        )

    async def _handle_task_result(
        self, context: Any, params: GetTaskPayloadRequestParams
    ) -> CallToolResult:
        """The finished job's tool reply: the same payload ``wait=true`` returns."""
        from gflow_cli.mcp.tools import task_payload  # tools imports this module

        queue_task = self._get_task(params.task_id)
        if _mcp_status(queue_task) == "working":
            text = f"Task {params.task_id} is still running; poll tasks/get until it finishes."
            return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        payload = task_payload(queue_task.profile_name, params.task_id, self._get_store().path)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, indent=2))],
            structured_content=payload,
            is_error=payload["status"] != "completed",
        )

    async def _handle_cancel_task(
        self, context: Any, params: CancelTaskRequestParams
    ) -> CancelTaskResult:
        task_id = params.task_id
        before = self._get_task(task_id)
        repo = QueueRepository(self._get_store())
        running = DETACHED.get(task_id)
        log.info("mcp.task.cancel_requested", task_id=task_id, running_here=running is not None)
        if before.status in ("completed", "failed", "indeterminate"):
            pass  # already finished: report it as it is, never as a cancel
        elif running is not None:
            # The job's own CancelledError handler records how far it got: a job past
            # submit becomes indeterminate (a credit may be spent), never failed.
            running.cancel()
            await asyncio.wait({running}, timeout=_CANCEL_WAIT_S)
        else:
            repo.update_task_status(
                task_id,
                "failed",
                error={
                    "title": "Cancelled",
                    "status": 499,
                    "detail": "Cancelled before it ran.",
                    "reason": "cancelled",
                },
                only_from=("pending",),
            )

        # Re-read: the job's own handler, not this method, wrote the final row.
        queue_task = self._get_task(task_id)
        return CancelTaskResult(
            task_id=queue_task.task_id,
            status=_mcp_status(queue_task),
            status_message=_status_message(queue_task),
            created_at=queue_task.created_at or "",
            last_updated_at=queue_task.updated_at or "",
            ttl=3600,
        )
