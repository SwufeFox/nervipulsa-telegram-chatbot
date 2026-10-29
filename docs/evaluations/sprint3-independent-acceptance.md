# Sprint 3 — Independent Acceptance Record

Date: 2026-10-29 (workspace environment date)
Repository: `E:\Projects\Magixyne\nervipulsa-telegram-chatbot`
Branch at inspection: `main`, HEAD `111075f` (`origin/main`)

## Scope and source of acceptance

The Sprint 3 contract is `C:\Users\Hileb\.config\alma\missions\mvrqb3dj7fjlp037uf\sprints\sprint-3\contract-v1.json` (the supplied `contract.json` path does not exist). The full recovered phase-one handoff is `...\sprints\sprint-1\handoff-recovered.md`, especially its six requirements and shared process-interruption requirement. Those six requirements are: atomic update/message persistence; monotonic debounce; durable turn/decision recovery; safe pending-send recovery while preserving `unknown`; continued ingestion with per-topic order and bounded cross-topic concurrency/supersession; atomic decision-version check plus send claim.

## Independent verification

Ran:

```text
python -m pytest -q tests/test_sprint2_process_recovery.py tests/test_sprint2_live_process.py tests/test_phase_one_blackbox_acceptance.py tests/test_phase_one_additional.py tests/test_phase_one_edge_cases.py tests/test_claim_action_atomicity.py tests/test_observation_cursor_migration.py tests/test_action_terminal_recovery.py
53 passed in 102.63s
```

This is a regression result, not a blanket production acceptance. `test_sprint2_process_recovery.py` uses child processes and hard exits for inbound transaction and debounce recovery, races independent processes for claims/supersession, and reopens the database to inspect persisted outcomes. The live scheduler test (`test_sprint2_live_process.py`) runs `main.run_live` in child processes, hard-exits while an activation is blocked, and starts a second process to inspect recovery. Its Telegram transport and Agent/model are fakes. Other process-level tests also use fake transport, fake clock, or legacy policy paths. Therefore the test run does **not** prove recovery of a real Telegram/model production deployment, nor does it prove every failure boundary in the default autonomous path. No real Telegram service or model provider was exercised.

The tests provide specific evidence for several boundaries: an abrupt exit inside inbound persistence leaves no partial update/message/observation rows and retry writes them; a real monotonic debounce interval continues across process restart and merges two messages; simultaneous process claims yield one winner; send-claim versus supersession yields one winner and restart preserves the resulting pending/unknown or superseded/failed state; a blocked live-loop activation is interrupted and a new live-loop process recovers stored observations. These findings are bounded by the fixtures and code paths described above. Test output alone is not evidence that all six are fully accepted.

## Historical data compatibility

The migration regression `tests/test_observation_cursor_migration.py` passed as part of the 53-test run. Existing schema migration and legacy recovery fixtures remain covered by the targeted suite. This verifies the tested old-schema fixtures, not every deployed historical database. The application dependency remains pinned in `pyproject.toml` to Nervipulsa commit `7618bd067e08ef8f8a7dc2f77ecf68fb9b0cec89`.

## Six-item status

1. **Inbound atomicity:** targeted hard-exit and retry regression passed.
2. **Monotonic debounce:** child-process restart regression passed; it injects a wall clock for persisted timestamps while the turn manager uses monotonic time.
3. **Turn/decision recovery:** persistence/recovery suites passed and the live-loop child-process test recovers an observation after a blocked activation exits. Full default autonomous production-path crash coverage remains unproven.
4. **Pending send semantics:** claim/restart tests passed; preserve `unknown` for ambiguous in-flight requests. No remote exactly-once guarantee is established.
5. **Production scheduling:** a live-loop scheduling test uses the real `run_live` orchestration but fake network and Agent/model; a process exit/restart scenario is covered. Real provider/network behavior and complete default autonomous mode recovery remain unverified.
6. **Atomic version check and send claim:** cross-process claim/supersession race regression passed and restart state was inspected. External send cancellation after request transmission cannot be established by this test.

## Git state and delivery boundary

At audit start the worktree already contained a user change to `app/console.py` and an untracked `tests/test_console_completion.py`. They are outside this Sprint 3 acceptance record and were left untouched. The audit record is the only file in this local commit. No push was performed.
