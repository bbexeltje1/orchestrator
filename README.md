# DRIFTLINE Orchestrator v1.1.4

A locally running, hybrid-autonomy development orchestrator for the **DRIFTLINE**
Roblox project. Python owns the workflow. Qwen reasons. THX-02 routes. Arena
implements. **Python policy is never overridden by an AI model.**

The application is a standalone, Windows-installable package. One touch does
everything: install, test, start every needed service in the background and
open the console. No separate terminals, no manual steps.

---

## Quick start (Windows)

**Double-click `play.cmd`.** That is the only thing you need to do. It will:

1. Check Windows/PowerShell and internet connectivity.
2. Find Python, or install a user-local Python 3.12 runtime (official sources,
   signature verified, no administrator required).
3. Create an isolated `.venv` and install pinned dependencies.
4. Check Git.
5. Check Ollama; offer to install it from the official distribution if missing.
6. Offer to download `HAL-X/thx-02:q4_k_m` (~3.7 GB) — only with your approval.
7. Start the services that are not running yet (Ollama, and any service with a
   configured `services.*_start_command`, e.g. your llama.cpp Qwen server) as
   hidden background processes — **no separate terminals**. Services that are
   already running are only probed and left untouched.
8. Probe the Qwen endpoint, OmniRoute health and the Arena bridge MCP endpoint.
9. Create `config/config.yaml` and `.env` from templates if absent.
10. Run the offline test suite and local integration smoke tests.
11. Print a readiness report.
12. Open the **orchestrator console** in the same window.

Your existing Qwen/llama.cpp installation is **never** modified, reinstalled or
replaced. The model download is skipped entirely if the model already exists.

### The console

Everything is driven from the one window you launched:

```text
DRIFTLINE ORCHESTRATOR - console
Everything runs from this window. Type 'help' for commands, 'quit' to leave.
driftline> doctor
driftline> submit "Add a combo counter to the combat system"
driftline> status
driftline> quit
```

Commands: `doctor`, `report`, `status`, `submit`, `approve`, `init`,
`qwen-models`, `help`, `quit`. A goal is planned only; nothing is dispatched
until you review the plan and run `submit ... --execute` with
`workflow.dry_run: false`. `qwen-models` discovers local `.gguf` model files
and prints paste-ready `services.qwen_start_command` lines - you choose the
model; the tool never guesses it.

### Options

```powershell
.\play.cmd                       # double-click "press play" (or run from a terminal)
.\play.cmd -Force                # unattended first run: auto-accept the Ollama
                                 # install and the ~3.7 GB model download
.\start.ps1                      # same as play.cmd, for a terminal
.\start.ps1 -Doctor              # probe services only, change nothing
.\start.ps1 -Test                # run offline + integration tests
.\start.ps1 -SkipModelDownload   # never download THX-02
.\start.ps1 -DryRun              # install/validate/report, do not launch
.\stop.ps1                       # stop the orchestrator and the background
                                 # services this launcher started (nothing else)
.\test.ps1                       # alias for start.ps1 -Test
.\doctor.ps1                     # alias for start.ps1 -Doctor
```

The standard first run installs, tests and launches **without dispatching an
actual implementation task**. A task is only dispatched after you submit a goal
and disable `dry_run` (or use `--execute` after review).

---

## Using the orchestrator

Everything happens in the console (see above). The same commands are available
non-interactively through the CLI:

```powershell
# Show the readiness report
.\.venv\Scripts\python.exe -m orchestrator doctor

# Submit a goal (planning only - dry_run is on by default)
.\.venv\Scripts\python.exe -m orchestrator submit "Add a combo counter to the combat system"

# Review the created tasks
.\.venv\Scripts\python.exe -m orchestrator status

# Dispatch after you have reviewed the plan and set workflow.dry_run: false
.\.venv\Scripts\python.exe -m orchestrator submit "Add a combo counter" --execute

# Resolve an approval request
.\.venv\Scripts\python.exe -m orchestrator approve <approval-id>
```

All commands also work through `start.ps1`'s environment; the `python` used is
the one inside `.venv`.

---

## Readiness report

```text
DRIFTLINE ORCHESTRATOR
======================

Python runtime           PASS
Dependencies             PASS
SQLite                   PASS

Qwen local               CONNECTED / BLOCKED
OmniRoute                CONNECTED / BLOCKED / NOT CONFIGURED
THX-02                   CONNECTED / BLOCKED
Arena MCP                CONNECTED / BLOCKED

Arena tools discovered   PASS / UNVERIFIED
Arena task capability    VERIFIED / UNVERIFIED

Git repository           PASS / BLOCKED
Workflow                 IDLE
```

`BLOCKED` means the service was probed with a real request and did not answer.
A bare TCP connect is never reported as `PASS`.

---

## Requirements

* Windows 10/11 with PowerShell 5.1+
* Git on `PATH`
* Python 3.10+ (the installer can provision 3.12 user-locally)
* Ollama 0.35+ for the `/v1/systemone` decision endpoint
* A local Qwen/llama.cpp server on `127.0.0.1:8090` — your existing one, or
  auto-started by the launcher when you set `services.qwen_start_command`
* The Arena bridge reachable at `127.0.0.1:17864`

---

## What is in the box

```text
driftline-orchestrator/
├── play.cmd, start.ps1, stop.ps1, doctor.ps1, test.ps1
├── README.md, ARCHITECTURE.md, SECURITY.md, LICENSES.md
├── CONFIGURATION.md, TROUBLESHOOTING.md, KNOWN_LIMITATIONS.md
├── config/config.yaml, config/config.example.yaml
├── orchestrator/…            # modular Python package
├── tests/…                   # offline + integration tests with local stubs
├── requirements.txt, requirements-dev.txt, pyproject.toml
├── .env.example
└── packaging/build_zip.py
```

Runtime state lives in `data/` (SQLite), `logs/` (including per-service logs)
and `run/` (records of background services started by the launcher). None of
it is packaged or committed.

See `ARCHITECTURE.md` for the module-by-module design and
`KNOWN_LIMITATIONS.md` for the exact Arena integration status.

---

## Safety defaults

* `workflow.dry_run: true` — no Arena call is made until you disable it.
* All commands run without a shell and through an allowlist.
* Merges, publishes, destructive Git, deletes, permission changes and
  production changes always require explicit human approval.
* The orchestrator never force-pushes, deletes branches, merges protected
  branches or publishes.
* Any local service binds to `127.0.0.1` only.

---

## Deliverable

`release/DRIFTLINE-Orchestrator-v1.1.4.zip` plus `release/SHA256SUMS.txt`,
`release/MANIFEST.txt` and `release/TEST-REPORT.txt`. See
`packaging/build_zip.py` for the exact exclusion rules.
