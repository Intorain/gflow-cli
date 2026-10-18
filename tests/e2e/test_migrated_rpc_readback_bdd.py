"""Real Flow readback, zero generation quota. Only the response-body failure is injected.

The browser submits real zero-credit writes and a second real tab verifies the
persisted result. No generation control is invoked. One empty project is retained
with a gflow-e2e-rpc prefix because Flow exposes no delete-project CLI route.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from playwright.async_api import Response
from pytest_bdd import given, scenarios, then, when

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.transports import migrated_composer as mc

scenarios("../features/migrated_rpc_readback.feature")


@pytest.fixture
def world() -> dict[str, Any]:
    return {}


@given("a real signed-in Flow browser")
def profile(world: dict[str, Any], e2e_profile_dir: Path) -> None:
    world["profile"] = e2e_profile_dir


@when("a rename and an upload commit but their response bodies cannot be read")
def run(world: dict[str, Any]) -> None:
    image = Path(os.environ.get("GFLOW_CLI_E2E_RPC_IMAGE", ""))
    if not image.is_file():
        pytest.skip("Set GFLOW_CLI_E2E_RPC_IMAGE to an owned reference image")

    async def drive() -> None:
        async with FlowApiClient(profile_dir=world["profile"]) as client:
            page = client._page  # noqa: SLF001
            assert page is not None
            counts = {mc.CREATE_PROJECT_RPC: 0, mc.RENAME_PROJECT_RPC: 0, mc.UPLOAD_RPC: 0}
            injected: set[str] = set()
            original_text = Response.text

            async def unreadable(response: Response) -> str:
                rid = mc._rpcid(response.url)  # noqa: SLF001
                if rid in counts and rid not in injected:
                    # Consume the real body first: Flow already completed the operation.
                    await original_text(response)
                    injected.add(rid)
                    raise RuntimeError("Network.getResponseBody: No data found for resource")
                return await original_text(response)

            def requested(request: Any) -> None:
                rid = mc._rpcid(request.url)  # noqa: SLF001
                if rid in counts:
                    counts[rid] += 1

            page.on("request", requested)
            wanted = "gflow-e2e-rpc-renamed-" + uuid.uuid4().hex[:8]
            with patch.object(Response, "text", unreadable):
                created = await mc.create_project(page, "gflow-e2e-rpc-" + uuid.uuid4().hex[:8])
                await mc.rename_project(page, created.project_id, wanted)
                media_id, name = await mc.MigratedComposer().upload(page, created.project_id, image)
            title, tiles = await mc._project_snapshot(page, created.project_id, upload_name=name)  # noqa: SLF001
            world.update(
                title=title,
                wanted=wanted,
                media=media_id,
                name=name,
                tiles=tiles,
                counts=counts,
                injected=injected,
            )

    asyncio.run(drive())


@then("each write is recovered from persisted project state without resubmission")
def verify(world: dict[str, Any]) -> None:
    assert world["title"] == world["wanted"]
    assert [t["id"] for t in world["tiles"] if t["name"] == world["name"]] == [world["media"]]
    assert world["counts"] == {mc.CREATE_PROJECT_RPC: 1, mc.RENAME_PROJECT_RPC: 2, mc.UPLOAD_RPC: 1}
    assert world["injected"] == {mc.CREATE_PROJECT_RPC, mc.RENAME_PROJECT_RPC, mc.UPLOAD_RPC}
