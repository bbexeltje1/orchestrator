# Architecture

## 1. Principle of control

```
                 ┌──────────────────────────────────────────────┐
                 │              PYTHON ORCHESTRATOR              │
                 │  owns state, policy, scheduling, verification │
                 └───────┬───────────────┬───────────────┬───────┘
                         │               │               │
                    (plan)          (route)        (implement)
                         │               │               │
                    ┌────▼────┐    ┌─────▼─────┐   ┌─────▼─────┐
                    │  QWEN   │    │  THX-02   │   │   ARENA   │
                    │ complex │    │  fast     │   │ implement │
                    │reasoning│    │ decisions │   │  engine   │
                    └─────────┘    └───────────┘   └───────────┘
```

AI models produce **advice**. Python produces **actions**. No model can create
a task, dispatch it, approve it, merge, publish or delete anything. Every state
change goes through `TaskManager` and every risky operation through `Policy`.

## 2. Modules

| Module | Responsibility |
|---|---|
| `orchestrator/cli.py` | Commands: `doctor`, `report`, `status`, `submit`, `idle`, `approve`, `init` |
| `orchestrator/app.py` | Bootstrap/wiring of config, storage, engines, adapters |
| `orchestrator/config.py` | YAML + `.env` + environment config, validation, secret resolution |
| `orchestrator/models.py` | Enums (`TaskStatus`, `Decision`, `ServiceHealth`), `Task`, validation, redaction |
| `orchestrator/storage/database.py` | SQLite WAL store, task/event/approval/idempotency tables |
| `orchestrator/state_machine.py` | Closed transition table and guards |
| `orchestrator/policy.py` | Operation classification, command allowlist, model-decision gating |
| `orchestrator/task_manager.py` | Creation, dedupe, dispatch, timeout reconciliation, retry |
| `orchestrator/workflow.py` | Goal planning, task execution loop, backoff polling, THX consultation |
| `orchestrator/engines/qwen.py` | `/v1/models` discovery, chat completions, OmniRoute validation |
| `orchestrator/engines/thx02.py` | System One `/v1/systemone` decisions + optional degraded fallback |
| `orchestrator/agents/arena.py` | MCP discovery, capability reporting, dispatch/status/result |
| `orchestrator/mcp/client.py` | Real MCP client: stdio + Streamable HTTP |
| `orchestrator/verification/git.py` | Read-only Git inspection and safe branch creation |
| `orchestrator/verification/tests.py` | Allowlisted test execution with timeouts |
| `orchestrator/verification/roblox_qa.py` | Rojo/Luau diagnostics, runtime logs, evidence |

## 3. Task state machine

```
READY ──dispatch──▶ DISPATCH_PENDING ──accepted──▶ RUNNING ──poll──▶ RESULT_RECEIVED
   ▲                     │                         │                     │
   │                     │timeout                  │fail                 ▼
   │                     ▼                         ▼                  VERIFYING
   │              DISPATCH_UNKNOWN ──reconcile──▶ FAILED / BLOCKED      │
   │                                                                     ▼
   └──retry/requeue── VERIFIED ◀──pass──── NEEDS_REVIEW ◀──fail───────┘
                          │
                          └──▶ AWAITING_APPROVAL
```

Illegal transitions raise `StateMachineError`. `DISPATCH_UNKNOWN` can never
return to `DISPATCH_PENDING`; it must be reconciled first.

### Dispatch safety rules

* `tasks.idempotency_key` is `UNIQUE` in SQLite. Duplicate creation is impossible.
* `DISPATCH_PENDING` is committed **before** the Arena call.
* If the call times out, the task becomes `DISPATCH_UNKNOWN` and a second
  `dispatch()` is refused. Only `reconcile()` (or an explicit human `retry()`)
  may move it.
* `dry_run: true` suppresses the Arena call entirely.
* A status poll failure never creates a task and never triggers AI inference.

## 4. Persistence

SQLite with `journal_mode=WAL` and `synchronous=FULL`. Four tables:

* `tasks` — one row per task, with status, Arena reference, changed files, test
  results, verification, error, retry count, final result.
* `task_events` — append-only audit of every status transition.
* `approvals` — pending/resolved human approvals.
* `idempotency_log` — durable record of dispatch keys (first write wins).

Closing and reopening the database loses nothing (see `test_database.py`).

## 5. THX-02 decision engine

Contract used (verified against the Ollama *System One* documentation):

```http
POST http://127.0.0.1:11434/v1/systemone
{
  "model": "HAL-X/thx-02:q4_k_m",
  "state": "<workflow state JSON as text>",
  "questions": {
    "workflow": {
      "type": "choice",
      "instructions": "…",
      "criteria": { "DISPATCH": "…", "WAIT": "…", … }
    }
  }
}
```

Response `answers.workflow.choice` + `probabilities` + `confidence` are stored.
The decision set is closed. Unknown values, missing probability distributions
and confidence below `thx.min_confidence` (default `0.55`) all escalate — the
engine returns *no decision* rather than guessing. Confidence is never an
authorization mechanism. If `/v1/systemone` is unavailable and
`allow_fallback_chat` is true, a degraded chat fallback runs and is explicitly
marked `UNVERIFIED`.

## 6. Qwen engine

* `GET /v1/models` discovers the real model id — nothing is assumed.
* `POST /v1/chat/completions` performs planning and complex-failure analysis.
* OmniRoute is **not** assumed. `validate_omniroute()` probes the health path
  and then validates a real chat route; only a 2xx on an actual completion
  enables routing. Otherwise traffic stays direct.

## 7. Arena adapter and integration audit

### What was verified

The Model Context Protocol transports are implemented from the specification:

* **stdio** — newline-delimited JSON-RPC over a child process.
* **Streamable HTTP** — POST JSON-RPC, `Mcp-Session-Id` handling, JSON or SSE
  responses, `DELETE` session teardown.

The adapter calls the real methods `initialize`, `notifications/initialized`,
`tools/list`, `tools/call`. It then selects a dispatch/status/result tool from
the **actual** `tools/list` output using an ordered candidate list
(`arena_task`, `arena_dispatch`, `arena_submit_task`, …) and, only if none
match, a schema heuristic. The discovered `inputSchema` is stored and used to
build the arguments; unsatisfiable required properties block the dispatch with a
diagnostic instead of being invented.

### Capability levels (reported separately, never conflated)

| Level | Meaning |
|---|---|
| `NOT_CONNECTED` | No MCP transport established |
| `CONNECTED` | `initialize` succeeded |
| `TOOL_FOUND` | A dispatch tool was identified from the real schema |
| `DISPATCH_ACCEPTED` | A dispatch call returned without MCP error |
| `COMPLETED` | A terminal result was retrieved |

### Honest limitation

The exact tool name, schema and transport of the bridge on your machine at
`127.0.0.1:17864` could not be introspected from the build environment, which
is separate from your Windows host. The adapter therefore **discovers** all of
these at runtime. If discovery fails it reports `ARENA_NOT_CONNECTED` with the
precise transport error, and the system runs in fallback mode. It never
simulates a successful connection, and it never claims dispatch works merely
because a TCP port is open.

The documented arena-bridge REST API (`GET /health`, `GET /tools`,
`POST /call`, `POST /mcp`) is probed separately and reported under
`Arena REST bridge`; it is never used to claim implementation-task capability.

## 8. Roblox-specific behaviour

* Studio design-time objects remain the source of truth for visuals and layout.
* Runtime scripts only handle behaviour.
* The orchestrator never opens Play mode or mutates Studio state during install,
  startup or routine verification. Studio testing is an explicit QA operation.
* Rojo build, Luau analysis, existing tests and Studio diagnostics are supported
  through `verification/`.

## 9. Testing strategy

* **Offline** — SQLite persistence, transitions, duplicate protection, timeout
  reconciliation, restart recovery, THX validation, Qwen parsing, MCP schema
  discovery, policy, approvals, packaging exclusions. All external services are
  replaced by local stubs that speak the real protocols.
* **Integration** — `start.ps1` probes Qwen, OmniRoute, THX and Arena against
  your real services. Arena is probed read-only; no test task is created.
* Every offline test is forbidden from contacting a real Arena instance: the
  adapter is only ever pointed at `127.0.0.1` stubs.

## 10. Security

See `SECURITY.md`. Highlights: loopback-only binding, secret redaction,
argv-only allowlisted commands, repository/branch validation, subprocess
timeouts and cleanup, protected config, durable local state.
