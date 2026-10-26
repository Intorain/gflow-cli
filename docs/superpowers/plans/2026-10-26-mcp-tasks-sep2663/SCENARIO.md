# Scenario: MCP tasks/get and tasks/cancel follow SEP-2663 (#1102)

Design, from `/gflow:predict` (CAUTION 7/10, no STOP):
- remove `tasks/result`;
- `tasks/get` returns one SEP-2663 shape in every era, with the finished tool result inline;
- a generation failure reads `completed` + `result.isError`;
- `tasks/cancel` returns an empty ack after it has stopped the job.

Measured on the wire before any change (in-process `mcp.Client`, both eras, 2026-10-26):
- `tasks/get` sends `ttl: 3600`, the 2025-11-25 key. SEP-2663 wants `ttlMs`, and 3600 ms is not true of rows kept with no expiry.
- `tasks/result` answers in the `2026-07-28` era, where SEP-2663 L949 requires `-32601`.
- The modern era stamps `resultType: "complete"` on extension replies itself.

## Coverage map
- **Active:** D6 data layer (rows read back, statuses), D7 error propagation (JSON-RPC codes), D11 input boundaries (unknown or terminal task ids), D12 observability (per-poll log), D13 MCP surface (this change lives only there).
- **Skipped:**
  - D1-D5, D9, D10: no auth, WAF, selector, batch, page-pool, transport or headed-browser change; the job runner is untouched.
  - D8: no paths are built.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D13 | Modern client calls `tasks/result` | High | `-32601` Method Not Found | Integration (in-process client, `2026-07-28`) |
| 2 | D13 | Legacy-era client calls `tasks/result` | Medium | `-32601` too: the method is gone, not gated | Integration (`legacy`) |
| 3 | D6/D13 | `tasks/get` on a completed job | High | `status: completed`, `result` = the CallToolResult `wait=true` returns (`structuredContent.files` present), `ttlMs: null`, no `ttl` key | Integration |
| 4 | D6/D13 | `tasks/get` on a generation that failed (row `failed`, not a cancel) | Critical | `status: completed` + `result.isError: true` with the Problem Details. Never `failed` (SEP L835) | Unit + Integration |
| 5 | D6 | `tasks/get` on a job cancelled after submit (row `indeterminate`, reason cancelled) | Critical | `status: cancelled`, statusMessage says a credit may be spent, never `completed` | Unit |
| 6 | D6 | `tasks/get` on a row `indeterminate` for a reason other than cancel (server killed mid-job) | High | `completed` + `isError` and the credit statusMessage. A run that may have billed is never silent | Unit |
| 7 | D6 | `tasks/get` on a pending or processing job | High | `status: working`, no `result` | Unit |
| 8 | D7/D13 | `tasks/cancel` on a running job | Critical | Job cancelled and awaited (row written), reply `{}` (+ `resultType` in modern era); the next `tasks/get` is already `cancelled` | Unit (existing #1097 tests, re-asserted via get) |
| 9 | D7 | `tasks/cancel` on a pending row nobody runs | High | Row → failed/cancelled, reply `{}`, `tasks/get` → `cancelled` | Unit |
| 10 | D7 | `tasks/cancel` on a finished task | Medium | Reply `{}`; row unchanged; `tasks/get` still `completed` (SEP L406: cancel is cooperative, may end non-cancelled) | Unit |
| 11 | D11 | Unknown taskId on get or cancel | High | `-32602` (unchanged) | Unit (existing) |
| 12 | D13 | Agent reads `_WAIT_DESCRIPTION` via `tools/list` | High | Says: poll tasks/get; `completed` carries `result`; check `result.isError`; no `tasks/result` | Unit (phrase pin) |
| 13 | D12 | Polling a completed task repeatedly | Low | Log noise from `mcp.tool.task_completed` on each poll. Accepted, see Deferred | — |
| 14 | D13 | Real `gflow mcp run` over stdio, image `wait=false` | High | `tasks/get` reaches `completed` and its `result.structuredContent.files` exist on disk | E2E (`@e2e @e2e_image`) |

## Must-cover before merge (Critical + High)
1-9, 11, 12, 14.

## Deferred
- #13: a log line per poll of a completed task. Low; the payload builder logs on read.
- `tools/call` never returns `resultType: "task"`. The SEP allows that (L951), and gflow's handle stays app-level. The docs must not claim more than this. A follow-up issue covers server-directed task creation.
- Task ids are bearer capabilities on `gflow serve` (any holder of the daemon token can read or cancel any id). This predates the change; it is noted in docs/MCP.md.

## Suggested BDD scenarios
`tests/features/mcp_wait_false.feature` (`@e2e @e2e_image`, bound from `tests/e2e/test_mcp_wait_false_bdd.py`):
```gherkin
  Scenario: a queued image job completes and its result is readable
    Given a gflow mcp run server over stdio on the e2e profile
    When the agent calls gflow_generate_image with wait false
    Then the reply is pending with a task id
    And tasks/get eventually reports the task completed
    And the completed task carries an image file that exists on disk
```
`tests/features/mcp_tasks.feature` (offline): the cancel scenario changes from "status is updated to failed" to "tasks/get then reports cancelled".

## Known-issues cross-reference
`KNOWN_ISSUES.md` ~L960 (a crashed job reads `working`) still holds; this change neither resolves nor worsens it.
