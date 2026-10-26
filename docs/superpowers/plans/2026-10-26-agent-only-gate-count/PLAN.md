# PLAN — read the agent-only credit gate's count before approving (#988)

**Goal:** never approve a credit confirmation that quotes more generations than requested. One gate can bill several clips, and an MCP prompt may be untrusted.
**Branch:** `bugfix/988-gate-count` (worktree `C:\development\github\gflow-cli-988`).
**Inputs:**
- `/gflow:issue-assessment`: CONFIRMED-BUG at `agent_only_composer.py:530`.
- `/gflow:predict`: CAUTION 7/10, no STOP.
- The contributor's en + ru gate capture on #988.

## Scenarios (severity · test)
| # | Scenario | Severity | Expected | Test |
|---|---|---|---|---|
| 1 | Video gate quotes 2, request is 1 | Critical | Reject clicked, nothing approved, exit 25, `retryable: false`, hint "rejected before approval" | driver: `test_a_gate_quoting_more_clips_than_requested_is_rejected_not_approved` |
| 2 | Title renders after the option rows | Critical | Never approved on a half title; approved once it has count + credits | driver: `test_a_gate_title_that_renders_late_is_read_once_complete` |
| 3 | Measured ru title, count 1 | High | Approved | driver: `test_the_measured_russian_gate_is_approved_for_one_clip` |
| 4 | Video gate title with no numbers (unmeasured shape) | High | Not approved, nothing clicked, exit 25 | driver: `test_a_video_gate_whose_count_cannot_be_read_is_never_approved` |
| 5 | Image gate title unreadable | Medium | Approved with a warning; the extra-tile check still guards | driver: `test_an_image_gate_whose_count_cannot_be_read_is_still_approved` |
| 6 | Prompt slips a digit into the title | High | Can only refuse: every number but the last is checked | unit: `test_gate_quotes_more` |
| 7 | Reject click or settle fails | High | "Nothing approved" is never claimed; the after-submit hint instead | unit: `test_a_reject_that_does_not_take_keeps_the_after_submit_advice` |
| 8 | Title quotes 1, agent queues 2 anyway | High | Existing post-hoc check still fails the run | driver: `test_two_clips_queued_for_one_is_a_typed_failure` (unchanged) |

## Tasks
- [x] Red: the scenarios above as driver + unit tests (10 failed for the right reason)
- [x] Green:
  - `gate_quotes_more`;
  - `_Run.count` / `gate_seen_at`;
  - `_gate_within_count` in `_gate_turn`;
  - `_reject_gate`;
  - `REJECT_ROW` / `GATE_TITLE` with their measurements.
- [x] Docs: KNOWN_ISSUES, CONFIGURATION, `config.py` help, `.env.template`, `errors.py` docstring, `skills/gflow-cli` exit-25 row, CHANGELOG (Security)
- [ ] `/gflow:check` green (scoped, `-n 4`)
- [ ] Independent review in a pinned worktree; then the PR; then `/gflow:sonar`

## e2e
**Named external blocker:** no maintainer profile is served the agent-only composer (`docs/superpowers/spikes/2026-10-17-agent-only-cohort-profiles.md`). The contributor who captured the gate offered $0 raise-and-reject runs. The PR asks them for three:
- (a) a count-1 gate approved;
- (b) an over-count gate rejected;
- (c) a prompt with digits, to show whether the title echoes prompt text.
