# PLAN — MCP tasks/get and tasks/cancel follow SEP-2663 (#1102)

**Goal:** the server declares `io.modelcontextprotocol/tasks` (SEP-2663), so its task methods must speak it. Today `tasks/get` and `tasks/cancel` speak the 2025-11-25 experimental spec, and `tasks/result` exists at all.
**Branch:** `bugfix/1102-sep2663-tasks` (worktree `C:\development\github\gflow-cli-1102`, off `develop` ef7bf3e1).
**Inputs:** `/gflow:issue-assessment` CONFIRMED-BUG; `/gflow:predict` CAUTION 7/10 (design decided there); [SCENARIO.md](SCENARIO.md).

Keep from #1097:
- `wait=false` runs the job;
- a cancel after submit leaves the row `indeterminate`;
- a cancelled job reads `cancelled`.

## Task 1: Failing tests first (red)
- [x] `tests/mcp/test_tasks_extension.py`: in-process `mcp.Client` wire tests, legacy and `2026-07-28`.
  - `tasks/result` → `-32601`.
  - `tasks/get` keys: `ttlMs` null, no `ttl`; `result` when completed.
  - `tasks/cancel` → `{}`.
- [x] Unit tests: status mapping for a failure (`completed` + `isError`), cancel-after-submit (`cancelled` + credit message), indeterminate not caused by a cancel (`completed` + `isError` + credit message), pending (`working`, no `result`).
- [x] Re-point the `tasks/result` tests in `tests/mcp/test_wait_false_runs.py` at `tasks/get`'s `result`; the cancel tests assert the following `tasks/get`.

## Task 2: Implementation (green)
- [x] `src/gflow_cli/mcp/tasks_extension.py`: drop the `tasks/result` binding and handler.
  - `_mcp_status`: failure → `completed`.
  - `tasks/get` returns a dict with `result` built via `CallToolResult(...).model_dump(by_alias=True, ...)`.
  - `tasks/cancel` keeps its wait and returns `{}`.

## Task 3: Surfaces and docs
- [x] `_WAIT_DESCRIPTION` + `task_payload` docstring (`tools.py`); `tests/mcp/test_wait_parameter_documented.py` phrase pins.
- [x] `docs/MCP.md` (+ `website/docs/` via generator), `docs/PROJECT_STATUS.md`. State plainly: get/cancel follow SEP-2663, `tools/call` does not return `resultType: "task"`, and the task id is a bearer capability.
- [x] `CHANGELOG.md` `[Unreleased]`: rewrite the `tasks/result` line; add **Changed** entries for the cancel ack and for failure now reading `completed` + `isError`.
- [x] BDD: `tests/features/mcp_tasks.feature` + steps; `tests/features/mcp_wait_false.feature`. Dev script `scripts/dev/live_verify_mcp_tasks.py`.

## Task 4: e2e
- [x] `tests/e2e/test_mcp_wait_false_bdd.py`: read the result from `tasks/get`, and assert the `ttlMs` key on the real wire. Run live: `-m e2e`, image tier, needs a `browser-session` lease.

## Task 5: Gates
- [x] `/gflow:check` green (scoped pytest, `-n 4` max)
- [x] Independent review in a pinned worktree (GREEN on fdb2a765), then the PR, then `/gflow:sonar`

## Done when
- [x] Every Critical and High scenario in SCENARIO.md has a test
- [x] Docs and tool schema text are truthful
- [x] The e2e has run live and its output is in the PR (2 passed in 54 s on fdb2a765)
- [x] Follow-up issue filed for server-directed task creation (`resultType: "task"`): #1105
