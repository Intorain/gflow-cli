"""Project create / rename on flow.google.com (#864).

The fake models what `docs/superpowers/spikes/2026-09-17-project-create-on-flow-google-com.md`
measured: the projects page's `add` FAB fires `jHPbke` and answers `[id, [title]]`; the
project header's `flow-editable-text` input renames through `o8DA4`, answering `[title]`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gflow_cli.api.transports import migrated_composer as mc
from gflow_cli.errors import TransportTimeoutError, UiSelectorDriftError, WireFormatError

PID = "57c099ee-a5d6-4e2d-83c9-0346f9644b09"


def _frame(rpcid: str, payload: Any) -> str:
    frame = [["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]]
    return ")]}'\n\n10\n" + json.dumps(frame) + "\n"


def _batch(rpcid: str) -> str:
    return f"https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute?rpcids={rpcid}"


class _Response:
    def __init__(self, url: str, text: str) -> None:
        self.url, self._text = url, text

    async def text(self) -> str:
        if self._text is None:
            raise RuntimeError("response body unavailable")
        return self._text


class _Loc:
    def __init__(self, page: FakePage, sel: str) -> None:
        self.page, self.sel = page, sel

    @property
    def first(self) -> _Loc:
        return self

    async def wait_for(self, **_: Any) -> None:
        if not self.page.present.get(self.sel, False):
            raise mc.PlaywrightTimeoutError(f"waiting for {self.sel}")

    async def click(self, **_: Any) -> None:
        self.page.clicks.append(self.sel)
        await self.page.fire(self.page.on_click.get(self.sel))

    async def input_value(self) -> str:
        return self.page.current_title

    async def fill(self, value: str, **_: Any) -> None:
        self.page.filled = value

    async def press(self, key: str, **_: Any) -> None:
        if key == "Enter":
            await self.page.fire(self.page.on_enter)

    async def is_visible(self) -> bool:
        return False

    async def count(self) -> int:
        return 1 if self.page.present.get(self.sel, False) else 0


class FakePage:
    def __init__(self, *, url: str = "about:blank") -> None:
        self.url = url
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.filled: str | None = None
        self.current_title = ""
        self.present: dict[str, bool] = {mc.NEW_PROJECT_BUTTON: True, mc.PROJECT_TITLE_INPUT: True}
        self.on_click: dict[str, tuple[str, str] | None] = {}
        self.on_enter: tuple[str, str] | None = None
        self.navigate_on_create: str | None = None
        self._handlers: list[Any] = []
        self.keyboard = self

    async def wait_for_url(self, pattern: Any, **_: Any) -> None:
        assert pattern.search(self.url)

    async def goto(self, url: str, **_: Any) -> None:
        self.gotos.append(url)
        self.url = url

    def locator(self, sel: str) -> _Loc:
        return _Loc(self, sel)

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._handlers.append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        if event == "response" and handler in self._handlers:
            self._handlers.remove(handler)

    async def fire(self, reply: tuple[str, str] | None) -> None:
        if reply is None:
            return
        for h in list(self._handlers):
            await h(_Response(*reply))
        if self.navigate_on_create:
            self.url = self.navigate_on_create


def _page_creating(title: str = "Sep 17 - 17:02", pid: str = PID) -> FakePage:
    page = FakePage()
    page.on_click[mc.NEW_PROJECT_BUTTON] = (
        _batch(mc.CREATE_PROJECT_RPC),
        _frame("jHPbke", [pid, [title]]),
    )
    page.navigate_on_create = f"https://flow.google.com/project/{pid}"
    return page


# --- create -----------------------------------------------------------------------------


async def test_create_reads_id_and_title_from_the_rpc_reply() -> None:
    page = _page_creating(title="Sep 17 - 17:02")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["my film"]))

    info = await mc.create_project(page, "my film")  # type: ignore[arg-type]

    assert info.project_id == PID
    assert info.title == "my film"
    assert page.gotos[0] == "https://flow.google.com/"
    assert page.clicks == [mc.NEW_PROJECT_BUTTON]
    assert page.filled == "my film"  # the requested title was applied by rename


async def test_create_skips_rename_when_the_reply_already_carries_the_title() -> None:
    page = _page_creating(title="my film")

    info = await mc.create_project(page, "my film")  # type: ignore[arg-type]

    assert info.title == "my film"
    assert page.filled is None


async def test_create_without_the_fab_is_drift_naming_the_anchor() -> None:
    page = _page_creating()
    page.present[mc.NEW_PROJECT_BUTTON] = False

    with pytest.raises(UiSelectorDriftError, match="new-project"):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_with_no_reply_times_out() -> None:
    page = _page_creating()
    page.on_click[mc.NEW_PROJECT_BUTTON] = None

    with pytest.raises(TransportTimeoutError, match=mc.CREATE_PROJECT_RPC):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_reply_without_a_project_id_is_a_wire_format_error() -> None:
    page = _page_creating(pid="not-a-uuid")

    with pytest.raises(WireFormatError, match=mc.CREATE_PROJECT_RPC):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


async def test_create_ignores_other_rpcs_while_waiting() -> None:
    page = FakePage()
    page.on_click[mc.NEW_PROJECT_BUTTON] = (_batch("UpteDb"), _frame("UpteDb", [[[PID, ["old"]]]]))

    with pytest.raises(TransportTimeoutError):
        await mc.create_project(page, "x", timeout_s=0.1)  # type: ignore[arg-type]


# --- rename -----------------------------------------------------------------------------


async def test_rename_navigates_to_the_project_and_confirms_the_echo() -> None:
    page = FakePage()
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["new title"]))

    await mc.rename_project(page, PID, "new title")  # type: ignore[arg-type]

    assert page.gotos == [f"https://flow.google.com/project/{PID}"]
    assert page.filled == "new title"


async def test_rename_does_not_reload_a_project_page_already_open() -> None:
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["t"]))

    await mc.rename_project(page, PID, "t")  # type: ignore[arg-type]

    assert page.gotos == []


async def test_rename_echoing_another_title_is_a_wire_format_error() -> None:
    page = FakePage()
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame("o8DA4", ["something else"]))

    with pytest.raises(WireFormatError, match="something else"):
        await mc.rename_project(page, PID, "wanted", timeout_s=0.1)  # type: ignore[arg-type]


async def test_rename_without_the_title_input_is_drift() -> None:
    page = FakePage()
    page.present[mc.PROJECT_TITLE_INPUT] = False

    with pytest.raises(UiSelectorDriftError, match="title"):
        await mc.rename_project(page, PID, "t", timeout_s=0.1)  # type: ignore[arg-type]


def test_anchors_are_structural_not_labels() -> None:
    """AGENTS.md locale-invariance: no display text in these selectors."""
    assert "flow-projects-page" in mc.NEW_PROJECT_BUTTON
    assert "'add'" in mc.NEW_PROJECT_BUTTON or '"add"' in mc.NEW_PROJECT_BUTTON
    assert mc.PROJECT_TITLE_INPUT.startswith("flow-editable-text")


@pytest.mark.parametrize("rpcid", [mc.CREATE_PROJECT_RPC, mc.RENAME_PROJECT_RPC])
async def test_unreadable_reply_reply_is_not_misreported_as_a_timeout(rpcid: str) -> None:
    from gflow_cli.errors import is_retryable

    page = FakePage(url=f"https://flow.google.com/project/{PID}")

    async def action() -> None:
        await page.fire((_batch(rpcid), None))

    with pytest.raises(WireFormatError, match="body was unavailable") as exc:
        await mc._rpc_reply(page, rpcid, action, timeout_s=0.02)  # type: ignore[arg-type]
    assert exc.value.route == f"batchexecute:{rpcid}"
    assert not is_retryable(exc.value)
    assert "outcome" in exc.value.remediation_hint
    assert not page._handlers


async def test_rename_recovers_persisted_title_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), None)
    readback = AsyncMock(return_value=("wanted", []))
    monkeypatch.setattr(mc, "_project_snapshot", readback, raising=False)
    await mc.rename_project(page, PID, "wanted", timeout_s=0.02)  # type: ignore[arg-type]
    readback.assert_awaited_once_with(page, PID)
    assert page.filled == "wanted"


async def test_create_recovers_created_url_without_another_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    page = _page_creating()
    page.on_click[mc.NEW_PROJECT_BUTTON] = (_batch(mc.CREATE_PROJECT_RPC), None)
    readback = AsyncMock(return_value=("wanted", []))
    monkeypatch.setattr(mc, "_project_snapshot", readback, raising=False)
    got = await mc.create_project(page, "wanted", timeout_s=0.02)  # type: ignore[arg-type]
    assert got.project_id == PID and got.title == "wanted"
    assert page.clicks == [mc.NEW_PROJECT_BUTTON]
    readback.assert_awaited_once_with(page, PID)


async def test_rename_does_not_guess_success_when_readback_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), None)
    monkeypatch.setattr(mc, "_project_snapshot", AsyncMock(return_value=("old", [])), raising=False)
    with pytest.raises(WireFormatError, match="body was unavailable"):
        await mc.rename_project(page, PID, "wanted", timeout_s=0.02)  # type: ignore[arg-type]


async def test_rename_to_the_current_title_needs_no_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(mc, "_project_snapshot", AsyncMock(return_value=("already saved", [])))
    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.current_title = "already saved"
    await mc.rename_project(page, PID, "already saved", timeout_s=0.01)  # type: ignore[arg-type]
    assert page.filled is None and not page._handlers


async def test_local_title_alone_does_not_prove_rename_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    page = FakePage(url=f"https://flow.google.com/project/{PID}")
    page.current_title = "wanted"
    monkeypatch.setattr(mc, "_project_snapshot", AsyncMock(return_value=("old", [])))
    page.on_enter = (_batch(mc.RENAME_PROJECT_RPC), _frame(mc.RENAME_PROJECT_RPC, ["wanted"]))
    await mc.rename_project(page, PID, "wanted", timeout_s=0.01)  # type: ignore[arg-type]
    assert page.filled == "wanted"
