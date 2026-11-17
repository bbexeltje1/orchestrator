# Configuration

Configuration is read from `config/config.yaml`, then overridden by `.env`,
then by environment variables. Precedence (lowest → highest):

```
built-in defaults  <  config/config.yaml  <  .env  <  process environment
```

Environment overrides use the form `DRIFTLINE_<SECTION>_<KEY>`, e.g.
`DRIFTLINE_WORKFLOW_DRY_RUN=false`.

## project

| Key | Default | Notes |
|---|---|---|
| `name` | `DRIFTLINE` | project identifier stored on every task |
| `repository` | `auto` | `auto` searches upward for `.git` |
| `default_branch` | `auto` | `auto` resolves to `main` |
| `workspace_root` | `.` | base for relative paths |

## qwen

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `provider` | `llama_cpp` | |
| `base_url` | `http://127.0.0.1:8090/v1` | your existing server |
| `model` | `auto` | discovered from `/v1/models` |
| `timeout` | `60.0` | seconds |
| `max_tokens` | `4096` | |

Your llama.cpp installation and model are never modified.

## omniroute

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | optional |
| `base_url` | `http://127.0.0.1:20128` | |
| `health_path` | `/health` | probed before routing is enabled |
| `api_key_env` | `OMNIROUTE_API_KEY` | name of the env var, not the secret |

Routing is only enabled after a real health check **and** a real chat-route
completion both succeed. Otherwise traffic stays direct.

## thx

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | if unavailable, fallback mode |
| `provider` | `ollama` | |
| `base_url` | `http://127.0.0.1:11434` | |
| `model` | `HAL-X/thx-02:q4_k_m` | |
| `decision_endpoint` | `/v1/systemone` | System One contract |
| `timeout` | `60.0` | seconds |
| `min_confidence` | `0.55` | below this the decision escalates |
| `allow_fallback_chat` | `true` | degraded, marked `UNVERIFIED` |

## arena

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `transport` | `auto` | `auto` \| `streamable_http` \| `stdio` |
| `endpoint` | `http://127.0.0.1:17864/mcp` | your local bridge |
| `stdio_command` | `""` | e.g. `npx -y …` for an MCP stdio server |
| `task_tool` | `auto` | discovered from `tools/list` |
| `status_tool` | `auto` | discovered |
| `result_tool` | `auto` | discovered |
| `token_env` | `ARENA_BRIDGE_TOKEN` | env var name only |
| `request_timeout` | `30.0` | also the dispatch timeout |
| `poll_interval` | `10.0` | seconds |
| `poll_max` | `120` | maximum status polls |
| `backoff_base` | `2.0` | exponential backoff multiplier |

The real tool names and schemas are discovered at runtime. See
`KNOWN_LIMITATIONS.md` for the current Arena status.

## claude_code

The actual coding agent: the **official Claude Code CLI**, run headlessly with
**your own subscription login** (`claude login`). DRIFTLINE never reads, stores
or substitutes credentials, and never uses `--dangerously-skip-permissions`.

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | `false` = the executor and its readiness rows report NOT_CONFIGURED |
| `command` | `claude` | executable name or full path |
| `timeout` | `600.0` | per-task run timeout in seconds |
| `probe_timeout` | `90.0` | readiness probe timeout in seconds |
| `model` | `\\\"\\\"` | optional `--model` override; empty = your CLI default |
| `permission_mode` | `acceptEdits` | non-bypassing mode so edits work headlessly |
| `extra_args` | `\\\"\\\"` | extra CLI args (shell-safe tokens only) |

Execution happens only through explicit commands: `claude-run <objective> --yes`
(free-form task; `--yes` is required) and `claude-acceptance` (bounded test in a
disposable repository under `run\`). Both record a real task, capture the
agent's output and exit status, and never touch files outside the chosen
working directory.

## workflow

| Key | Default | Notes |
|---|---|---|
| `autonomy` | `hybrid` | `hybrid` \| `manual` \| `autonomous` |
| `max_parallel_tasks` | `1` | |
| `max_retries` | `2` | explicit retries only |
| `dry_run` | `true` | **no Arena call while true** |
| `require_approval_for_merge` | `true` | |
| `require_approval_for_publish` | `true` | |
| `safe_idle_on_start` | `true` | |
| `poll_interval` | `10.0` | seconds |

## verification

| Key | Default | Notes |
|---|---|---|
| `run_git_checks` | `true` | |
| `run_tests` | `true` | |
| `run_rojo_build` | `false` | enable for Rojo projects |
| `luau_lsp_command` | `""` | e.g. `luau-analyze .` |
| `roblox_studio_diagnostics` | `false` | never opens Play mode |

## logging

| Key | Default |
|---|---|
| `level` | `INFO` |
| `log_file` | `logs/orchestrator.log` |
| `redact_secrets` | `true` |
| `max_bytes` | `5242880` |
| `backup_count` | `3` |

## storage

| Key | Default |
|---|---|
| `database` | `data/orchestrator.db` |

## services

Background service management used by `play.cmd` / `start.ps1`, so nothing
needs a separate terminal. A service whose `*_start_command` is empty is never
started automatically. Services that are already running are only probed and
left untouched. Commands run as hidden background processes with output in
`logs/service-<name>.out.log` / `.err.log`; `.\stop.ps1` stops exactly the
processes the launcher started (recorded in `run\`) and nothing else.

| Key | Default | Notes |
|---|---|---|
| `auto_start` | `true` | `false` = probe only, never start anything |
| `health_wait_seconds` | `60` | how long to wait for a started service to answer |
| `ollama_start_command` | `\"\"` | empty = `<detected ollama> serve` |
| `qwen_start_command` | `\"\"` | e.g. `llama-server -m C:\models\qwen.gguf --port 8090` |
| `omniroute_start_command` | `\"\"` | optional |
| `arena_start_command` | `\"\"` | your Arena bridge, if it should be launched too |

The commands are plain Windows command lines; quote executables whose path
contains spaces. Starting a service is always opt-in via these commands (or
the Ollama default); the launcher never modifies, reconfigures or replaces an
existing installation. A config file written by an older version is upgraded
automatically on startup: missing sections (such as `services`) are appended,
existing keys, values and comments are never touched. Any key here can also be
set without editing the file, e.g.
`DRIFTLINE_SERVICES_QWEN_START_COMMAND=llama-server -m C:\models\qwen.gguf --port 8090`.

## server

| Key | Default | Notes |
|---|---|---|
| `bind_host` | `127.0.0.1` | loopback only; validated |
| `port` | `17870` | |
| `enabled` | `false` | no listener by default |

## Secrets

Put secrets in `.env` (created from `.env.example`):

```dotenv
ARENA_BRIDGE_TOKEN=
OMNIROUTE_API_KEY=
GITHUB_TOKEN=
```

`.env` is excluded from Git and from the ZIP. Never commit it.
