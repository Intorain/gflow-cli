# Scenario: MCP `wait=false` jobs actually run (#1097)

Predict verdict: **CAUTION 7/10**, option C. On `wait=false`, the server runs the
existing inline path as a detached `asyncio` task in its own loop. This scenario
analysis feeds the plan alongside it.

## Coverage map

| Dim | Active? | Why |
|---|---|---|
| D1 Auth | yes | A detached job hits an expired session after the tool call has already returned |
| D2 WAF | yes | N `wait=false` calls become N back-to-back unattended runs |
| D3 Selectors | **skip** | No selector changes. The detached path runs the same `process_task` |
| D4 Batch/resume | yes | Double billing on resume or cancel. Old pending rows |
| D5 Concurrency | yes | `_profile_lock`, cross-process lease, detached-task registry |
| D6 Data layer | yes | Rows stuck in `processing`, conditional cancel update |
| D7 Errors | yes | A detached exception must not be lost. Problem Details on the row |
| D8 Paths | **skip** | No new paths. Files resolve through the existing asset catalog |
| D9 Transport | **skip** | The transport is unchanged |
| D10 Headless | yes | Server shutdown mid-run must not leave Chromium orphaned |
| D11 Input | yes | Unknown or foreign `task_id` passed to `tasks/result` / `tasks/cancel` |
| D12 Observability | yes | New structlog events for detached start, failure and cancel |
| D13 MCP parity | yes | MCP-only defect. The CLI has no `wait` parameter, so the parity table needs no change |

## Scenario table

| # | Dim | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D13 | `generate_image(wait=false)` | Critical | Returns `pending` immediately. The job runs. `tasks/get` → `completed`. `tasks/result` → same payload as `wait=true` (file paths, project id, seed) | E2E live (`e2e_image`, $0) |
| 2 | D4 | `tasks/cancel` after submit has fired | Critical | The asyncio task is cancelled. `mark_interrupted` leaves the row **`indeterminate`**, never `failed`. Never resubmitted | Integration + E2E (`e2e_image`) |
| 3 | D4 | `tasks/cancel` while the row is still `pending` (waiting on the lock) | High | Row → `failed` through a **conditional** update (`WHERE status='pending'`). The task never claims it | Unit |
| 4 | D4 | The job finishes in the same instant as the cancel | Critical | Whichever write lands first wins. Cancel never overwrites `completed`, and completion never overwrites a cancel-classified row. `tasks/cancel` reports the real final status | Unit |
| 5 | D4 | Old `pending` rows from earlier broken `wait=false` calls exist in the DB | Critical | **Never** claimed. Only the new `task_id` is claimed (`claim_task`, not `claim_next_pending`) | Unit |
| 6 | D6 | A second `gflow mcp run` starts while the first is mid-job | Critical | The first job's `processing` row is untouched. No startup `recover_processing` | Unit (assert no recovery call at server start) |
| 7 | D5 | A `wait=false` job is running, then a `wait=true` call arrives on the same profile | High | The second call queues on `_profile_lock` and does not fail fast on the lease. Both complete | Integration |
| 8 | D5 | 3× `wait=false` on one profile | Medium | They run one after another under the lock. Each finishes with its own row | Integration |
| 9 | D7 | The detached job raises an unexpected exception | High | Row → `failed` with the Problem Details shape. structlog `mcp.task.detached_failed` with no payload and no locals. The registry entry is removed | Unit |
| 10 | D10 | stdin closes (client exits) mid-generation | High | The detached task is cancelled and awaited within a time limit. The row ends `failed`/`indeterminate`, not `processing`. No Chromium process left behind | **Spike first**, then E2E (`e2e_image`) |
| 11 | D10 | The server process is hard-killed mid-run | Medium | The row stays `processing`. This is the existing KNOWN_ISSUES entry (manual reconcile), and the docs now say so | Doc only |
| 12 | D1 | The session expired before the detached job ran | High | Row → `failed` with the auth Problem Details. `tasks/result` returns that envelope, not an empty success | Integration |
| 13 | D11 | `tasks/result` for an unknown id / an unfinished job | Medium | Unknown → error. Unfinished → an error saying it is still working (no blocking wait) | Unit |
| 14 | D13 | `--no-spend` server | High | Spend tools are absent, so no detached task can ever be created | Unit |
| 15 | D2 | A client fires 20 `wait=false` calls | Medium | The existing per-call rate limiter (`tools.py:909`) still applies. No new burst path | Unit (existing limiter covers it) |
| 16 | D12 | Event names | Low | `mcp.task.detached_started`, `mcp.task.detached_failed` and `mcp.task.cancel_requested`, documented | Unit |
| 17 | D13 | Schema text / docs | High | `_WAIT_DESCRIPTION` stops saying "never generated", and `docs/MCP.md` + the website mirror describe it truthfully | Unit (`test_wait_parameter_documented.py`) |
| 18 | D13 | `generate_video(wait=false)` | High | Same as #1, through the shared helper. It spends credits, so e2e is opt-in (`e2e_video`) | Integration. E2E opt-in |

## Must-cover before merge (Critical + High)
Scenarios 1, 2, 3, 4, 5, 6, 7, 9, 10, 12, 14, 17 and 18. Scenario 18's e2e is opt-in. The shared helper makes it the same code as scenario 1.

## Deferred
- Scenario 11: an automated reconciler. It is blocked on F1, see KNOWN_ISSUES.
- A per-profile cap on detached tasks. YAGNI until scenario 15 is observed in practice.
- `serve --profile` / a poll loop. Out of scope; Predict ruled option A STOP.

## Suggested BDD scenarios

```gherkin
@e2e @e2e_image
Feature: MCP wait=false generation runs in the background

  Scenario: A queued image job completes and its result is readable
    Given a stdio gflow MCP server on a live profile
    When the agent calls gflow_generate_image with wait false
    Then the reply status is "pending" with a task id
    And tasks/get eventually reports "completed"
    And tasks/result returns a file path that exists on disk

  Scenario: Closing the client mid-job leaves a truthful row
    Given a stdio gflow MCP server running a wait=false image job
    When the client closes stdin before the job finishes
    Then the job's row is not "processing"
    And no Chromium process started by the server remains
```

Offline scenarios 3–9, 13 and 14 are pytest unit/integration tests in `tests/mcp/`. They are not
Gherkin, because they exercise our own code only.

## Known-issues cross-reference
- **KNOWN_ISSUES.md: "`gflow serve` / MCP worker queue: interrupted post-submit tasks need manual reconciliation".**
  Scenario 11 stays under it, because a hard kill is still manual. Scenarios 2 and 10 *use* its safe
  `indeterminate` classification and do not change it.
