# Known limitations and integration status

This document is deliberately explicit about what is implemented, what was
tested, and what remains unverified or blocked.

## Status legend

| Mark | Meaning |
|---|---|
| **IMPLEMENTED** | Code exists and is exercised by automated tests |
| **TESTED** | Passed against a local stub speaking the real protocol |
| **UNVERIFIED** | Implemented but not confirmed against your live service |
| **BLOCKED** | Cannot be completed from the build environment; exact reason given |

---

## 1. Arena integration — the important one

**IMPLEMENTED.** The adapter speaks real MCP (stdio + Streamable HTTP), performs
`initialize`, `tools/list`, `tools/call`, discovers the real tool names and
input schemas, and reports five distinct capability levels. It supports the
documented arena-bridge REST API (`/health`, `/tools`) as a separate probe.

**BLOCKED (environment).** The bridge on your Windows host at
`127.0.0.1:17864` is not reachable from the build environment, which is a
separate machine. Therefore the following could **not** be confirmed against
your actual bridge:

* the exact transport it exposes;
* the exact dispatch tool name and `inputSchema`;
* the task-id / conversation-id field names;
* the status and result retrieval mechanism;
* whether it supports persistent conversations or idempotency keys.

**Consequence.** The adapter discovers all of these at runtime. On your first
`.\start.ps1` run it will print one of:

* `Arena task capability VERIFIED` — a dispatch tool was found and its schema was
  read. Dispatch still requires your approval and `dry_run: false`.
* `Arena task capability UNVERIFIED` / `ARENA_NOT_CONNECTED` — no transport or no
  dispatch tool. The system runs in fallback mode, **no task is dispatched**, and
  nothing is faked.

**To resolve.** Run `.\start.ps1 -Doctor` and read the `Arena MCP` and
`Arena capability` details. If a transport is reachable, the report lists the
real tool names that were discovered; set `arena.task_tool`, `arena.status_tool`
and `arena.result_tool` accordingly. If only stdio is available, set
`arena.transport: stdio` and `arena.stdio_command`.

**Idempotency.** Exactly-once dispatch is only possible if the Arena
integration supports idempotency. This was not verifiable. Until confirmed, a
dispatch that times out is marked `DISPATCH_UNKNOWN` and reconciled — it is
never blindly resent.

## 2. THX-02

**IMPLEMENTED + TESTED** against the documented `/v1/systemone` contract
(choice question, probabilities, confidence). Strict validation: unknown
decisions, missing distributions and low confidence all escalate.

**UNVERIFIED** against your live Ollama until `.\start.ps1` probes it. The
`HAL-X/thx-02:q4_k_m` weights are not bundled (~3.7 GB, downloaded separately
with your approval).

If your Ollama build does not expose `/v1/systemone`, the engine can use a
degraded chat fallback that is explicitly marked `UNVERIFIED`. THX can be
disabled entirely (`thx.enabled: false`); the orchestrator then runs in fallback
mode.

## 3. Qwen

**IMPLEMENTED + TESTED** against a stub implementing `/v1/models` and
`/v1/chat/completions`. Model id is discovered, never assumed.

**UNVERIFIED** against your llama.cpp server until probed. Your existing
llama.cpp installation and model are never modified, reconfigured or replaced,
and a running server is never touched. The launcher can start a *stopped*
server for you — only when you set `services.qwen_start_command`, only in the
background, and `.\stop.ps1` stops it again.

## 4. OmniRoute

**IMPLEMENTED + TESTED** detection logic. The actual API shape is not assumed:
health is probed first, then a real chat completion must return 2xx before
routing is enabled. If validation fails, routing stays off and traffic goes
direct. **UNVERIFIED** against your live instance.

## 5. Roblox QA

**IMPLEMENTED** Rojo/Luau/test diagnostics and runtime-log error collection.

**NOT AUTOMATIC.** Studio Play mode is never opened and Studio state is never
modified by installation, startup or routine verification. Studio testing is an
explicit QA operation you initiate. Screenshot/evidence collection requires a
path you provide.

## 6. Deliberately out of scope

* No dashboard or web UI (a clear CLI and status output were prioritised).
* No automatic merging, publishing, force-push or branch deletion.
* No credential reading from Claude Code, GitHub or browser profiles.
* No model weights, game assets or unrelated DRIFTLINE files in the ZIP.
* No event-driven Arena notifications; configurable polling with exponential
  backoff is used instead.

## 7. Test coverage

Offline tests cover persistence, transitions, duplicate protection, timeout
reconciliation, restart recovery, THX validation, Qwen parsing, MCP schema
discovery, policy, approvals and packaging exclusions. All external services
are stubs; no test contacts a real Arena instance. See `TEST-REPORT.txt`
for the exact results.
