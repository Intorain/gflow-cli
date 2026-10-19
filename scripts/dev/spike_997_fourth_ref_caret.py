r"""#997: where is the caret when gflow types the 4th ``@``, and does the picker open? ($0)

The reporter (external, gflow 0.83.1, macOS) says that on flow.google.com the 4th and
later references never attach: before every ``@``, ``_mention_by_token`` /
``_mention_by_name`` click the CENTRE of the composer (``page.locator(COMPOSER).first
.click()``); with three chips the first line is full, the centre click lands the caret
directly behind chip 3 with no space, and the mention plugin does not open its picker
for an ``@`` typed there. Proposed fix: after the click, collapse a DOM Range to the end
of the contenteditable before typing ``@``.

This drives exactly that gesture, N mentions per run, in four ARMS, on a fresh editor
load per run (image mode bound first, as the in-place path does):

  click         production: composer click, then ``@``            (the control)
  range_end     click + reporter's DOM Range collapsed to end     (the proposed fix)
  end_key       click + ``End``                                   (reporter: does NOT help)
  ctrl_end      click + ``Control+End``                           (a keyboard alternative)

Per mention it records, BEFORE typing ``@``: the click point and ``elementFromPoint``
there; the selection (anchor node, offset, whether the node immediately before the caret
is a chip, chips before/after the caret, text after the caret, caret rect); chip line
tops (how many visual lines). AFTER ``@``: picker option count and the composer tail.
A picker that opened is committed with option i (ArrowDown i-1, Enter) and followed by a
space, as production does. A picker that did NOT open is closed with Escape and the run
stops there — Enter is never pressed without an open picker, because Enter in the
composer is the submit key.

COST: navigation, the settings pane, DOM reads and typing only. NOTHING IS SUBMITTED;
the composer is cleared at the end of every run and the page reloaded before the next,
which also drops any client-side request state the chips left behind.

Pre-registered reading (written before the first run, 2026-10-19):

  control arm, mention 4 picker fails R/R runs, and in those runs the caret sits
      directly behind a chip (not at document end)
      -> reproduced; the reporter's mechanism is observed, stable.
  control arm mention 4 fails in some runs only -> it flaps; the mechanism is real
      but layout/timing dependent (viewport width decides when line 1 fills).
  control arm, mentions 1..N all open the picker in 0/R failing runs
      -> DOES NOT REPRODUCE HERE; settles nothing (the reporter's viewport/OS may wrap
      at a different chip count). Read the chip line count: if all chips fit on one line
      the precondition (a wrap) was never met, and that is the finding.
  range_end arm opens the picker for every mention while control fails
      -> the proposed fix is supported by a controlled comparison.
  end_key arm fails like control -> the reporter's "End does not help" holds.
  The failing mention number need not be 4: it is wherever line 1 fills at THIS width.

    uv run python scripts/dev/spike_997_fourth_ref_caret.py \
        --profile denon82 --project <uuid> [--mentions 5] [--runs 3] [--arms click range_end]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gflow_cli.api.image import GenerateImageRequest  # noqa: E402
from gflow_cli.api.transports.migrated_composer import (  # noqa: E402
    COMPOSER,
    MENTION_CHIP,
    PICKER_OPTION,
    MigratedComposer,
)

from _spike_common import build_client, default_out_path, resolve_profile_dir, step  # noqa: E402, isort: skip

ARMS = ("click", "range_end", "end_key", "ctrl_end")

# The reporter's proposed fix, verbatim in effect.
_RANGE_END_JS = """(sel) => {
  const ce = document.querySelector(sel); if (!ce) return;
  ce.focus();
  const r = document.createRange(); r.selectNodeContents(ce); r.collapse(false);
  const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
}"""

_CARET_JS = """([sel, chipSel]) => {
  const ce = document.querySelector(sel);
  if (!ce) return {composer: false};
  const s = window.getSelection();
  const out = {
    composer: true,
    focused: document.activeElement === ce || ce.contains(document.activeElement),
    composer_rect: (() => { const b = ce.getBoundingClientRect();
      return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; })(),
    chips: ce.querySelectorAll(chipSel).length,
    chip_line_tops: [...new Set([...ce.querySelectorAll(chipSel)].map(
      c => Math.round(c.getBoundingClientRect().top)))],
    text_tail: JSON.stringify((ce.textContent || '').slice(-40)),
    html_tail: ce.innerHTML.slice(-500),
  };
  if (!s || !s.rangeCount) { out.selection = null; return out; }
  const r = s.getRangeAt(0);
  const a = s.anchorNode;
  out.selection = {
    in_composer: ce.contains(r.startContainer),
    collapsed: r.collapsed,
    anchor_node: a ? (a.nodeType === 3 ? '#text' : a.nodeName) : null,
    anchor_offset: s.anchorOffset,
    anchor_parent_class: a && a.parentElement ? String(a.parentElement.className).slice(0, 80) : null,
  };
  // The node immediately before the caret, and whether it is (inside) a chip.
  let prev = null;
  const c = r.startContainer, o = r.startOffset;
  if (c.nodeType === 3) {
    if (o > 0) prev = {kind: 'text', char: JSON.stringify(c.data.slice(o - 1, o))};
    else { let n = c; while (n && n !== ce && !n.previousSibling) n = n.parentNode;
           prev = n && n !== ce ? {kind: 'node', node: n.previousSibling} : null; }
  } else {
    const n = o > 0 ? c.childNodes[o - 1] : null;
    prev = n ? {kind: 'node', node: n} : null;
  }
  if (prev && prev.kind === 'node') {
    const n = prev.node;
    const el = n.nodeType === 1 ? n : n.parentElement;
    prev = {kind: n.nodeType === 3 ? 'text' : n.nodeName,
            is_chip: !!(el && (el.matches(chipSel) || el.querySelector(chipSel) || el.closest(chipSel))),
            char: n.nodeType === 3 ? JSON.stringify(n.data.slice(-1)) : null};
  }
  out.selection.before_caret = prev;
  const all = [...ce.querySelectorAll(chipSel)];
  out.selection.chips_before_caret = all.filter(ch => {
    const x = document.createRange(); x.setStartAfter(ch);
    return r.compareBoundaryPoints(Range.START_TO_START, x) >= 0; }).length;
  const tail = document.createRange(); tail.selectNodeContents(ce); tail.setStart(r.endContainer, r.endOffset);
  out.selection.text_after_caret = JSON.stringify(tail.toString().slice(0, 40));
  out.selection.chips_after_caret = all.length - out.selection.chips_before_caret;
  const cr = r.getBoundingClientRect();
  out.selection.caret_rect = [Math.round(cr.left), Math.round(cr.top)];
  return out;
}"""

_POINT_JS = """([x, y, chipSel]) => {
  const e = document.elementFromPoint(x, y);
  if (!e) return null;
  return {tag: e.tagName, cls: String(e.className).slice(0, 80),
          in_chip: !!e.closest(chipSel), contenteditable: e.closest('[contenteditable]') !== null};
}"""


async def _place_caret(page: Any, arm: str) -> dict[str, Any]:
    box = await page.locator(COMPOSER).first.bounding_box()
    pt = None
    if box:
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        pt = {
            "x": round(x),
            "y": round(y),
            "element": await page.evaluate(_POINT_JS, [x, y, MENTION_CHIP]),
        }
    await page.locator(COMPOSER).first.click(timeout=5000)  # production gesture
    after_click = await page.evaluate(_CARET_JS, [COMPOSER, MENTION_CHIP])
    if arm == "range_end":
        await page.evaluate(_RANGE_END_JS, COMPOSER)
        await page.wait_for_timeout(300)
    elif arm == "end_key":
        await page.keyboard.press("End")
        await page.wait_for_timeout(300)
    elif arm == "ctrl_end":
        await page.keyboard.press("Control+End")
        await page.wait_for_timeout(300)
    before_at = (
        after_click if arm == "click" else await page.evaluate(_CARET_JS, [COMPOSER, MENTION_CHIP])
    )
    return {"click_point": pt, "after_click": after_click, "before_at": before_at}


async def _run(
    page: Any, composer: MigratedComposer, project: str, arm: str, mentions: int
) -> dict[str, Any]:
    await page.reload(wait_until="domcontentloaded", timeout=45_000)
    kind = await composer.ensure_editor(page, project)
    await composer.apply_image_settings(page, GenerateImageRequest(prompt="spike-997"))
    await composer.clear_composer(page)
    run: dict[str, Any] = {
        "arm": arm,
        "composer_kind": kind,
        "viewport": page.viewport_size,
        "mentions": [],
    }
    for i in range(1, mentions + 1):
        rec: dict[str, Any] = {"i": i, **await _place_caret(page, arm)}
        await page.keyboard.type("@", delay=120)
        await page.wait_for_timeout(2200)
        opts = await page.locator(PICKER_OPTION).count()
        rec["picker_options"] = opts
        rec["after_at"] = await page.evaluate(_CARET_JS, [COMPOSER, MENTION_CHIP])
        if opts == 0:
            rec["outcome"] = "picker_did_not_open"
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
            run["mentions"].append(rec)
            break
        for _ in range(min(i - 1, opts - 1)):
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(250)
        await page.wait_for_timeout(400)
        await page.keyboard.press("Enter")  # picker is open: Enter commits the option
        await page.wait_for_timeout(2500)
        chips = await composer.read_chips(page)
        rec["chips_after_commit"] = len(chips)
        rec["chip_kinds"] = [c.get("reference_type") for c in chips]
        rec["outcome"] = "attached" if len(chips) == i else "commit_did_not_add_chip"
        await page.keyboard.type(" ", delay=80)  # production types a space after a chip
        run["mentions"].append(rec)
        if rec["outcome"] != "attached":
            break
    await composer.clear_composer(page)
    run["attached"] = sum(1 for m in run["mentions"] if m["outcome"] == "attached")
    return run


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--mentions", type=int, default=5)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    args = ap.parse_args()
    composer = MigratedComposer()
    out = default_out_path("spike_997_fourth_ref_caret")
    findings: dict[str, Any] = {"project": args.project, "runs": []}

    def save() -> None:
        out.write_text(json.dumps(findings, indent=2), encoding="utf-8")

    async with build_client(resolve_profile_dir(args.profile)) as client:
        page = client._page  # noqa: SLF001 - dev instrument
        assert page is not None
        await composer.ensure_editor(page, args.project)
        for r in range(1, args.runs + 1):
            for arm in args.arms:  # interleaved, so drift over time hits every arm alike
                try:
                    run = await _run(page, composer, args.project, arm, args.mentions)
                except Exception as exc:  # noqa: BLE001 - record and keep measuring
                    run = {"arm": arm, "error": f"{type(exc).__name__}: {exc}"[:300]}
                    try:
                        await composer.clear_composer(page)
                    except Exception:  # noqa: BLE001
                        pass
                run["run"] = r
                findings["runs"].append(run)
                save()
                fails = [m for m in run.get("mentions", []) if m["outcome"] != "attached"]
                f = fails[0] if fails else None
                step(
                    f"run{r} {arm}",
                    f"attached={run.get('attached')} "
                    + (
                        f"FAIL@{f['i']} before_caret={f['before_at'].get('selection', {}) and f['before_at']['selection'].get('before_caret')} "
                        f"after_caret={f['before_at'].get('selection', {}) and f['before_at']['selection'].get('text_after_caret')} "
                        f"lines={f['before_at'].get('chip_line_tops')}"
                        if f
                        else ""
                    )
                    + (run.get("error") or ""),
                )
    save()
    step("done", f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
