"""#914: an upscale on the same warm FlowApiClient after a migrated image run.

The image run parks pool page 0 on about:blank; the route used to be read off that
page as labs, so the upscale minted reCAPTCHA there and failed with `RecaptchaError`
(measured 2026-10-23). One image of quota, the 2K upscale is $0. The pause covers
Flow listing a just-made tile late (a separate timing issue, measured the same day).

Needs GFLOW_CLI_E2E_PROFILE (served flow.google.com) and GFLOW_CLI_E2E_PROJECT.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.image import Aspect, GenerateImageRequest, Model
from gflow_cli.api.image_upscale import TargetResolution

pytestmark = pytest.mark.e2e

_TILE_LISTING_PAUSE_S = 20.0


@pytest.mark.asyncio
@pytest.mark.e2e_image
async def test_e2e_upscale_after_an_image_run_on_one_warm_client(
    e2e_profile_dir: Path, tmp_path: Path
) -> None:
    project = os.environ.get("GFLOW_CLI_E2E_PROJECT", "").strip()
    if not project:
        pytest.skip("GFLOW_CLI_E2E_PROJECT must name an existing Flow project id")
    req = GenerateImageRequest(
        prompt="a small blue teapot on a shelf", aspect=Aspect.SQUARE, model=Model.NARWHAL
    )
    async with FlowApiClient(profile_dir=e2e_profile_dir, out_dir=tmp_path) as client:
        image = await client.generate_image(project_id=project, req=req)
        if client.served_route(prefer_migrated=False) != "migrated":
            pytest.skip("profile is not on the migrated host")
        await asyncio.sleep(_TILE_LISTING_PAUSE_S)
        path = await client.upsample_image(
            media_id=image.media_name,
            project_id=project,
            target_resolution=TargetResolution.RES_2K,
            out_path=tmp_path / "up.png",
        )

    assert Path(str(path)).stat().st_size > 50_000
