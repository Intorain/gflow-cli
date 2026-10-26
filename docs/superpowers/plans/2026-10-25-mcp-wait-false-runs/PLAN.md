# MCP `wait=false` Runs Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature mcp-wait-false-runs` to find the next
> unchecked task. Implement one task at a time. Run `/gflow:check` before every commit.

**Goal:** `gflow_generate_image` / `gflow_generate_video` with `wait=false` return `pending` at once and the
job actually runs; the agent reads its result with `tasks/result` and can truly cancel it (#1097).

**Architecture:** Everything stays inside `src/gflow_cli/mcp/`. The inline `wait=true` body
(`tools.py:411-418` plus its read-back) is split into two helpers: `_execute_claimed(profile, task_id)`
and `_task_payload(profile, task_id)`. `wait=false` runs `_execute_claimed` as a detached `asyncio` task
in the server's own loop and keeps it in a module-level `{task_id: Task}` registry. `tasks/cancel` cancels
the live task, so the existing `CancelledError` handler (`worker/daemon.py:457-481`) can classify the row.
`tasks/result` returns `_task_payload`. No poll loop, no startup `recover_processing`, no `serve --profile`.

**Predict verdict:** CAUTION, 7/10 (option C). See `SCENARIO.md` beside this file.

**Risk register:**
| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Cancel after submit recorded as `failed`, so the credit spent is invisible | Cancel the asyncio task and let `mark_interrupted` write `indeterminate` (T5) |
| Critical | Old `pending` rows get billed | Claim by `task_id` only, never `claim_next_pending` (T3) |
| Critical | A sibling stdio server fails another server's live job | No startup recovery; a test asserts none runs (T2) |
| High | Detached exception lost | Done-callback logs `mcp.task.detached_failed`, no locals, no payload (T4) |
| High | Client exit orphans Chromium or leaves a row `processing` | Spike first (T1); bounded cancel-and-await on shutdown (T7) |
| High | Media of a completed job unreachable | `tasks/result` handler (T6) |

---

## File structure

### New files
```
scripts/dev/spike_mcp_detached_shutdown.py
  $0 measurement: what stdio EOF does to a detached task and its DB write
docs/superpowers/spikes/2026-10-25-mcp-detached-task-shutdown.md
  Spike findings
tests/mcp/test_wait_false_runs.py
  Offline scenarios 3-9, 13, 14
tests/e2e/features/mcp_wait_false.feature + tests/e2e/test_mcp_wait_false_bdd.py
  e2e scenarios 1 and 10 (@e2e @e2e_image)
```

### Modified files
```
src/gflow_cli/mcp/tools.py            helpers, detached registry, wait=false path, _WAIT_DESCRIPTION
src/gflow_cli/mcp/tasks_extension.py  tasks/cancel live-cancel; tasks/result
src/gflow_cli/worker/queue.py         conditional status update (only from an expected status)
src/gflow_cli/mcp/server.py           bounded cancel-and-await of detached tasks on shutdown
docs/MCP.md, website/docs mirror, KNOWN_ISSUES.md, CHANGELOG.md
```

---

## Task 1: Spike the shutdown behaviour ($0)

**What:** Measure what a real `gflow mcp run` does to a detached task when stdin closes, before
any design depends on it.

**Steps:**
- [x] Write `scripts/dev/spike_mcp_detached_shutdown.py`. It uses a throwaway `GFLOW_CLI_HOME`, a fake
      long coroutine (no browser) registered the way T4 will register a task, then closes stdin.
- [x] Record three things: is the task cancelled, does a DB write in its `except CancelledError`
      commit, and how long the server waits.
- [x] Do the same for `gflow serve` (HTTP) shutdown.
- [x] Write the spike note. Its outcome decides the T7 shape.

---

## Task 2: Red tests (no production code)

**Files:** `tests/mcp/test_wait_false_runs.py`

**Tests created (red):**
- [x] `wait=false` returns `pending`, and then the fake `process_task` runs for that `task_id` (sc. 1, offline)
- [x] An old `pending` row for the same profile is never claimed (sc. 5)
- [x] Server start calls no `recover_processing` (sc. 6)
- [x] `wait=false` then `wait=true` on one profile both complete, serialised by `_profile_lock` (sc. 7)
- [x] A detached exception leaves the row `failed`, logs `mcp.task.detached_failed`, and empties the registry (sc. 9)
- [x] Cancel of a still-`pending` row writes `failed` through the conditional update. Cancel of a post-submit
      row ends `indeterminate`. Cancel never overwrites `completed` (sc. 2, 3, 4)
- [x] `tasks/result`: unknown id gives an error; unfinished gives an error that says it is still working;
      completed gives the same payload as `wait=true` (sc. 13)
- [x] Under `--no-spend`, no detached task can be created (sc. 14) — covered by the existing `tests/mcp/test_no_spend.py` (spend tools are removed, so no call can detach)

---

## Task 3: Extract the shared helpers (refactor, behaviour unchanged)

- [x] `_execute_claimed(profile, task_id)`: lock, `claim_task`, `process_task`, `close`. Moved verbatim from `tools.py:411-418`
- [x] `_task_payload(profile, task_id)`: the read-back block (`tools.py:420+`), moved verbatim
- [x] `wait=true` calls both. The existing `tests/mcp/` suite is green with no test changes

## Task 4: Detached `wait=false`

- [x] After enqueue, `asyncio.create_task(_execute_claimed(...))`; store it in `_detached[task_id]`
- [x] The done-callback discards the entry. On an exception it logs `mcp.task.detached_failed`
      (`task_id`, error class only) and writes the row `failed` with Problem Details if it is not already terminal
- [x] Log `mcp.task.detached_started`
- [x] T2 tests for sc. 1, 5, 7, 9, 14 go green

## Task 5: Real cancel

- [x] `QueueRepository.update_task_status(..., only_from=("pending",))`: a conditional `UPDATE ... WHERE status IN (...)`
      that returns whether it applied
- [x] `tasks/cancel`: if a live task exists in the registry, `task.cancel()` and await it with a time limit.
      Otherwise use the conditional pending→failed update. Reply with the row's real final status
- [x] T2 tests for sc. 2, 3, 4 go green

## Task 6: `tasks/result`

- [x] Register a `MethodBinding("tasks/result", GetTaskPayloadRequestParams, ...)` that returns `_task_payload`
      (validated against the SDK 2.3.0 types)
- [x] T2 tests for sc. 13 go green

## Task 7: Shutdown

- [x] Use the shape T1 measured: on server shutdown, cancel every registry task and await them with a time
      limit, so each row is classified before exit
- [x] Test: shutdown with one fake running task leaves the row `failed`, not `processing`

## Task 8: e2e (Iron Law: the MCP queued path is its own surface)

- [x] `tests/e2e/features/mcp_wait_false.feature` with the two scenarios from SCENARIO.md, tagged `@e2e @e2e_image`
- [x] Bind them from `tests/e2e/test_mcp_wait_false_bdd.py`: a real stdio server and a live profile, image
      quota only, $0 credits
- [x] Run it and paste the result. A video twin is opt-in (`GFLOW_CLI_E2E_RUN_VIDEO=1`)

## Task 9: Docs

- [x] `_WAIT_DESCRIPTION` (`tools.py:69-80`): the job runs in the server process; read the result with
      `tasks/result`; a job dies with the server process
- [x] `docs/MCP.md:102-109` plus the regenerated `website/docs` mirror
- [x] `KNOWN_ISSUES.md` reconcile entry: a hard-killed server leaves the row `processing`
- [x] `CHANGELOG.md [Unreleased]` under **Fixed**, with `Closes #1097`
- [x] Add the new structlog events to `docs/ARCHITECTURE.md` if that doc lists MCP events

## Task 10: Gates
- [x] `/gflow:check` green
- [x] Then `/gflow:branch-review`, then the PR, then `/gflow:sonar` (PR #1101 merged ef7bf3e1; peer review YELLOW, fixed in 6cc5c979; SonarCloud 0 issues)

---

## Definition of done

- [x] All task steps checked off
- [x] `/gflow:check` green (ruff / format / pyright / pytest ≥ 80% coverage)
- [x] `CHANGELOG.md` `[Unreleased]` updated
- [x] `docs/MCP.md` and the tool schema text are truthful
- [x] Every Critical and High scenario from SCENARIO.md is covered by a test
- [x] The T8 e2e test has run live and its output is in the PR (2 passed in 80 s on 6cc5c979)
- [x] No `# TODO` in the diff without a tracked issue link
