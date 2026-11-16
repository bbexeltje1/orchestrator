# Security

## Network exposure

* Any local service binds to `127.0.0.1` (or `localhost`/`::1`) only.
  `config.server.bind_host` is validated at load time; a non-loopback value is
  a configuration error.
* The orchestrator never opens a listening port unless `server.enabled: true`.

## Secrets

* Credentials live in `.env`, which is excluded from Git and from the ZIP.
* `.env` is parsed locally; values are never written back to config or logs.
* `orchestrator.logutil.RedactingFormatter` redacts bearer tokens, API keys,
  passwords, GitHub tokens and any configured secret from every log line.
* Readiness reports and JSON output run through the same redaction.
* The Arena token is resolved only from the environment variable named by
  `arena.token_env`; it is never read from the config file.

The orchestrator does **not** read or export credentials from Claude Code,
GitHub, browser profiles or any other credential store.

## Command execution

* Commands are passed as argv lists. Shell strings are rejected.
* Only executables in the allowlist may run: `git`, `python`, `python3`,
  `pytest`, `rojo`, `luau-analyze`, `selene`, `ollama`.
* Shell metacharacters (`; & | ` $ > < newline`) in arguments are rejected.
* Destructive Git subcommands (`push`, `reset`, `clean`, `branch`, `rebase`,
  `cherry-pick`, `filter-branch`, `gc`, `prune`) are refused.
* `git checkout` is permitted **only** for branch creation (`checkout -b`).
* Every command has a timeout; timed-out processes are killed.

## Repository and branch safety

* Repository paths are validated (no traversal, no control characters).
* Branch names are validated against a strict character set and reject leading
  `-` and `..`.
* The default branch for implementation is an isolated
  `driftline/orchestrator-<timestamp>` feature branch.
* Force-push, branch deletion, protected-branch merge and publish are never
  performed automatically.

## Approval model

| Automatically allowed | Requires explicit approval |
|---|---|
| read repository | merge into protected branch |
| create plans | publish/release the game |
| dispatch bounded tasks | destructive Git operations |
| run tests / builds | delete project data |
| inspect Git diffs | change permissions |
| request targeted corrections | production infrastructure changes |
| repeat verification | overwrite user files |
| save results | force-push |

Approvals are stored in SQLite with a status and resolver. A pending approval
blocks the operation; resolving it does not automatically re-execute anything.

## Model output is not authorization

THX-02 confidence and Qwen recommendations are advisory. `Policy` re-evaluates
every operation independently. A model decision can never authorize an
irreversible operation, and low-confidence or unknown decisions escalate.

## Installer integrity

* Python and Ollama installers are downloaded only from official distributions.
* Both are verified with `Get-AuthenticodeSignature` before execution and are
  refused if the signature is invalid or the publisher is unexpected.
* No downloaded script is ever piped directly into a shell.
* Installs prefer user scope; administrator privileges are not required.

## Logging

Logs include timestamps and task context, are rotated, and are redacted. Logs
are excluded from the ZIP.
