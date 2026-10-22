"""E2E: a lost submit reply body never re-submits a billed generation (#1010).

Binds ``tests/features/migrated_submit_body_loss.feature`` (``-m e2e_image``) and its video
twin ``migrated_video_submit_body_loss.feature`` (``-m e2e_video``, opt in with
``GFLOW_CLI_E2E_RUN_VIDEO=1``); see ``docs/E2E_TESTING.md`` § BDD-bound e2e.

**Why an e2e.** Offline, a fake page proves the composer types the error. Only a real
submit proves the CLICK is the billed act the client must not repeat: before the fix this
exact run left two images in the project (spike
``docs/superpowers/spikes/2026-10-21-migrated-submit-reply-body-loss.md``).

**How the loss is provoked.** ``Response.text`` raises the exact error #1009 captured for
the first ``ogiZ0b`` reply; every submit request is counted on the browser context.

**What the image run does not separate.** The transport already returns a non-retryable
error on the fixed tree, so the image run passes with or without the client's
``max_attempts=1``; that half rests on ``test_client_never_resubmits_a_page_owned_image_run``.

**Cost.** Image: one image of daily quota, zero credits. Video: one Veo lite clip at the
default length.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api._generated import Response
from pytest_bdd import given, scenarios, then, when

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.image import GenerateImageRequest
from gflow_cli.api.transports import migrated_composer as mc
from gflow_cli.api.video import GenerateVideoRequest, Mode, VideoModel
from gflow_cli.errors import TransportTimeoutError, is_retryable

scenarios("../features/migrated_submit_body_loss.feature")
scenarios("../features/migrated_video_submit_body_loss.feature")

LOST_BODY = (
    "Response.text: Protocol error (Network.getResponseBody): "
    "No data found for resource with given identifier"
)


@pytest.fixture
def world(e2e_profile_dir: Path) -> dict[str, Any]:
    return {"profile": e2e_profile_dir, "submits": 0, "lost": 0}


def _lose_first_body(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, rpcids: tuple[str, ...]
) -> None:
    real_text = Response.text

    async def text(self: Response) -> str:
        if mc.url_rpcid(self.url) in rpcids and not world["lost"]:
            world["lost"] += 1
            raise PlaywrightError(LOST_BODY)
        return await real_text(self)

    monkeypatch.setattr(Response, "text", text)


def _run_counting(world: dict[str, Any], rpcids: tuple[str, ...], generate: Any) -> None:
    def count(req: Any) -> None:
        # The URL carries the rpcid; `post_data` raises on a gzipped body inside the listener.
        if mc.url_rpcid(req.url) in rpcids:
            world["submits"] += 1

    async def run() -> None:
        async with FlowApiClient(profile_dir=world["profile"], headless=False) as client:
            assert client._context is not None  # noqa: SLF001
            client._context.on("request", count)  # noqa: SLF001
            try:
                await generate(client)
            except Exception as exc:  # noqa: BLE001 — the raised error is what Then checks
                world["error"] = exc

    asyncio.run(run())


def _assert_one_submit(world: dict[str, Any]) -> None:
    if not world["lost"]:
        pytest.fail(f"no submit reply was reached, so nothing was tested: {world.get('error')!r}")
    assert world["submits"] == 1, world["submits"]


@given("an image submit whose reply body Chrome cannot return")
def _armed(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _lose_first_body(world, monkeypatch, (mc.IMAGE_SUBMIT_RPC,))


@when("gflow generates the image on the migrated host")
def _generate(world: dict[str, Any]) -> None:
    _run_counting(
        world,
        (mc.IMAGE_SUBMIT_RPC,),
        lambda client: client.generate_image(
            req=GenerateImageRequest(prompt="a teal origami crane on a wooden table")
        ),
    )


@then("exactly one image submit reached Flow")
def _one_submit(world: dict[str, Any]) -> None:
    _assert_one_submit(world)


@given("a video submit whose reply body Chrome cannot return")
def _armed_video(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("GFLOW_CLI_E2E_RUN_VIDEO", "") != "1":
        pytest.skip("set GFLOW_CLI_E2E_RUN_VIDEO=1 to spend credits on the lost video reply e2e")
    _lose_first_body(world, monkeypatch, mc.SUBMIT_RPCS)


@when("gflow generates a lite clip on the migrated host")
def _generate_video(world: dict[str, Any], tmp_path: Path) -> None:
    _run_counting(
        world,
        mc.SUBMIT_RPCS,
        lambda client: client.generate_video(
            req=GenerateVideoRequest(
                prompt="a paper boat drifting on a calm pond, static camera",
                mode=Mode.T2V,
                # No duration: which lengths Flow offers is account-dependent (4 s is not
                # offered for lite on every account), and the default is what users get.
                model=VideoModel.VEO_3_1_LITE,
            ),
            out_dir=tmp_path,
        ),
    )


@then("exactly one video submit reached Flow")
def _one_video_submit(world: dict[str, Any]) -> None:
    _assert_one_submit(world)


@then("gflow reports a timeout that is not retryable and says to check the project")
def _reported(world: dict[str, Any]) -> None:
    err = world.get("error")
    assert isinstance(err, TransportTimeoutError), repr(err)
    assert not is_retryable(err)
    assert "unreadable" in err.detail
    assert "project" in err.remediation_hint
