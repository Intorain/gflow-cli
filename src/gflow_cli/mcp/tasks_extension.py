"""MCP Tasks extension implementation (SEP-2663) for gflow-cli."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

import structlog
from mcp.server.extension import Extension, MethodBinding
from mcp.types import (
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


def detach(task_id: str, job: Coroutine[Any, Any, None]) -> None:
    """Run ``job`` in the background and track it until it finishes."""
    task = asyncio.create_task(job)
    DETACHED[task_id] = task
    task.add_done_callback(lambda _t: DETACHED.pop(task_id, None))


async def drain_detached() -> None:
    """Cancel every running job and wait for each to record how far it got.

    The HTTP server must call this from its lifespan: uvicorn re-raises the stop signal
    after a graceful shutdown, which kills the process before ``asyncio.run`` would
    cancel anything (spike 2026-10-25-mcp-detached-task-shutdown).
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
        return "failed"
    return "working"


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
            raise KeyError(f"Task not found: {task_id}")
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
                error={"title": "Cancelled", "status": 499, "detail": "Cancelled before it ran."},
                only_from=("pending",),
            )

        # Re-read: the job's own handler, not this method, wrote the final row.
        queue_task = self._get_task(task_id)
        status = _mcp_status(queue_task)
        cancelled = status == "failed" and before.status not in ("failed", "indeterminate")
        return CancelTaskResult(
            task_id=queue_task.task_id,
            status="cancelled" if cancelled else status,
            status_message=(
                "Cancelled after submit; a credit may have been spent. Check the Flow project."
                if queue_task.status == "indeterminate"
                else None
            ),
            created_at=queue_task.created_at or "",
            last_updated_at=queue_task.updated_at or "",
            ttl=3600,
        )
