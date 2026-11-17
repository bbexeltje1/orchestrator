# Troubleshooting

Run `.\start.ps1 -Doctor` (or `.\doctor.ps1`) for a live readiness report.
Every `BLOCKED` line includes the actual error that was observed.

## Files from an older version (play.cmd missing, config without services)

**`play.cmd` is missing / `config.yaml` has no `services` section** — the
project directory is a **v1.0.x copy**; both were added in v1.1.x. Update the
directory from the branch (`git pull`) or extract the current release ZIP over
it. Run `.\play.cmd` afterwards and check the header shows the current
version. On startup the launcher now **upgrades an existing `config.yaml`
automatically** (adds missing sections such as `services`, never touching
existing keys, values or comments) and reports what it added.

## Python runtime

**`Python not found`** — the installer offers to install Python 3.12 user-locally
from the official python.org distribution, verifying the Authenticode signature.
If winget is available it is used first. No administrator rights are needed.

**`Failed to create the virtual environment`** — ensure the project directory is
writable and not on a sync folder (OneDrive) that locks files.

## Dependencies

**`pip install` fails** — check internet access to `pypi.org`. A proxy must be
configured for `pip` (`pip config set global.proxy …`). Re-run `.\start.ps1`.

## Git

**`git not found on PATH`** — install Git from <https://git-scm.com/downloads>
and re-run. The orchestrator never modifies your global Git configuration.

**`no git repository found`** — set `project.repository` to the DRIFTLINE repo
path, or run from inside the repository.

## Qwen

**`Qwen local BLOCKED`** — your llama.cpp server is not answering at
`http://127.0.0.1:8090/v1/models`. The one-command launcher can run it for
you: set the command in `config/config.yaml` and re-run `.\play.cmd`:

```yaml
services:
  qwen_start_command: llama-server -m C:\models\your-qwen.gguf --port 8090
```

Or start it yourself with your existing configuration:

```text
llama-server -m <your-existing-qwen-gguf> --port 8090
```

The orchestrator only reads `/v1/models` and posts `/v1/chat/completions`; it
never modifies, reconfigures or replaces your server. Auto-start runs exactly
the command you configured, hidden in the background, and `.\stop.ps1` stops
it again.

**`qwen reachable but /models listed no models`** — the server is up but reports
no loaded model. Reload the model in your existing llama.cpp setup.

## Background services (play.cmd / start.ps1)

**`<service> BLOCKED: process exited during startup`** or **`did not answer
within Ns`** — the configured command did not produce a healthy service. The
background output is captured in `logs/service-<name>.out.log` and
`logs/service-<name>.err.log`; read the error log first. Typical causes: a
wrong model path in `services.qwen_start_command`, a port already in use by
another process (that process is never touched), or a missing runtime.

**The launcher started a service I wanted to run myself** — set
`services.auto_start: false` (probe only, never start anything) or leave the
individual `*_start_command` empty. Already-running services are always only
probed, never restarted or replaced.

**`.\stop.ps1` did not stop my own service** — by design. stop.ps1 stops only
the orchestrator's Python processes and the background services recorded in
`run\` (i.e. started by play.cmd/start.ps1). Services you started yourself are
yours to manage.

## OmniRoute

**`OmniRoute BLOCKED`** — optional. The orchestrator stays on the direct Qwen
route. Check that OmniRoute is running on `127.0.0.1:20128` and that
`/health` returns 200. Routing is only enabled after a real chat completion
succeeds, so a health-only endpoint is correctly reported as unusable.

## THX-02

**`THX-02 BLOCKED: model not installed`** — run
`.\start.ps1` without `-SkipModelDownload` and approve the ~3.7 GB download, or
run `ollama pull HAL-X/thx-02:q4_k_m` yourself.

**`/v1/systemone NOT available`** — Ollama 0.35+ is required. Update Ollama from
the official installer. With `allow_fallback_chat: true` the engine still runs
in a degraded, clearly marked mode.

**`confidence below conservative threshold`** — the workflow state was
ambiguous. THX escalated instead of acting. Inspect the task and provide a
clearer state or choose a human approval.

## Arena

**`Arena MCP BLOCKED` / `ARENA_NOT_CONNECTED`** — see `KNOWN_LIMITATIONS.md`.
Check that the bridge is listening on `127.0.0.1:17864` and that it speaks MCP
(Streamable HTTP at `/mcp`, or stdio via `arena.stdio_command`). The exact
error from the transport is printed in the report.

**`Arena tools discovered UNVERIFIED`** — the MCP server connected but
`tools/list` failed. The task capability is not assumed to work.

**`Arena task capability UNVERIFIED`** — connected, but no dispatch tool was
found in the real tool list. Set `arena.task_tool` to the real tool name, or
adapt the adapter. The orchestrator will not guess.

**`schema mismatch`** — the real Arena tool requires a property the orchestrator
does not supply. The task is marked `BLOCKED` with the missing property names.
Map that property in `ArenaAdapter.build_dispatch_arguments`.

## Tests

**`Offline tests FAIL`** — run `.\.venv\Scripts\python.exe -m pytest -q` manually
for full output. Tests never contact real services; a failure is a real defect.

## Dispatch

**`task is DISPATCH_UNKNOWN`** — a dispatch timed out after possibly reaching
Arena. Reconcile it; do not re-dispatch blindly. The status report explains the
reconciliation path.

**`retry limit reached`** — `workflow.max_retries` is exhausted. Review the task
before raising the limit.
