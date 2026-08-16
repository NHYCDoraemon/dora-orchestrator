# orchestrator

Dagster-first orchestration platform for autonomous engineering workflows.

This project is intentionally independent from any single business repository. Repositories such as `dora`, `dora-code`, or future service projects provide reviewed TaskIssueDraft batches; this orchestrator audits those batches, submits complete Issue Packets to a single Plane Project per repository, drives execution through Dagster, invokes executors, verifies deliverables, and keeps git history free of lock/heartbeat noise.

## Responsibilities

- Audit reviewed TaskIssueDraft batches before Plane submission.
- Submit Program Pages, Batch Pages, Root Epic Issues, and complete Task Issues into one Plane Project per repository.
- Provision Plane cycles, issues, dependencies, and summary links only after batch approval.
- Own Dagster jobs, assets, sensors, retry, concurrency, and run history.
- Invoke executor backends through a stable protocol.
- Enforce git delivery policy.

## Current status

Phase 1 foundation:

- Standard project scaffold for cross-project docs and module taxonomy.
- TaskGraph validation and deterministic source hashes for spec-based smoke tests.
- JSON spec loader.
- Memory and local-file Plane-compatible backends.
- `run_ready_task` noop execution loop.
- Batch-based TaskIssueDraft design is now the main direction; the platform spec lives in `docs/specs/2026-05-01-dora-orchestration-design.md`.
- Noop/Codex/Claude executor adapters.
- Minimal Dagster definitions that import when Dagster is installed.
- Unit tests that do not require network or model calls.

## Install

### Quick install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/NHYCDoraemon/dora-orchestrator/main/install.sh | bash
```

Checks Python >= 3.10, then installs via `pipx` (or `pip` if pipx is not available).

### Manual install

```bash
pipx install git+https://github.com/NHYCDoraemon/dora-orchestrator.git
# or:
pip install git+https://github.com/NHYCDoraemon/dora-orchestrator.git
```

### Editable install (for development)

```bash
pipx install --editable /path/to/dora-orchestrator
# or:
pip install -e /path/to/dora-orchestrator
```

Editable install means source edits are picked up without reinstalling.

## Local verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Dagster definition tests skip when Dagster is not installed.

## CLI overview

```bash
orchestrator scaffold    # Create standard Dora project scaffold
orchestrator batches     # List available batches in a project
orchestrator audit       # Audit a TaskIssueDraft batch
orchestrator approve     # Approve an audited batch
orchestrator submit      # Submit an approved batch to Plane
orchestrator provision   # Provision a spec into Plane (legacy)
```

All commands output JSON to stdout and a human-readable summary to stderr.

> **Plane backend**：query-issues / submit 等命令默认使用内存后端（`ORCHESTRATOR_PLANE_BACKEND` 默认 `memory`）。
> 要读写真实 Plane（DOR 项目，见 ADR-0119：workspace `console` / project `36803e0a-…`），必须
> `export ORCHESTRATOR_PLANE_BACKEND=live`，并确保 `~/.dora/plane.env` 已配置当前目标项目。

## Project discovery

After scaffolding a project, the orchestrator can auto-discover its configuration:

- **From the project directory**: walking up from `cwd` finds `.dora/project.json`, which supplies `--repo`, `--project-slug`, and `--project-title`.
- **From anywhere via `--project <slug>`**: scaffold also writes `~/.dora/orchestrator/projects/<slug>.json`, so the tool can look up the repo path by slug.

Explicit CLI flags (`--repo`, `--project-slug`, `--project-title`) always take priority over auto-discovery.

## Standard project scaffold

Every orchestrated repository should start with the same document slots and module taxonomy. Business-specific concepts go inside the fixed slots; they should not create ad-hoc Plane modules.

```bash
orchestrator scaffold \
  --repo /path/to/project \
  --project-slug my-project \
  --title "My Project"
```

The scaffold creates:

```text
.dora/project.json
docs/dora/index.md
docs/dora/modules.md
docs/dora/product/
docs/dora/architecture/
docs/dora/planning/
docs/dora/quality/
docs/dora/operations/
docs/dora/governance/
```

It also writes a project registry entry at `~/.dora/orchestrator/projects/my-project.json` for cross-directory lookup.

The fixed module taxonomy is:

```text
product
architecture
planning
implementation
verification
operations
governance
```

## Listing available batches

```bash
# From the project directory (auto-discovered):
orchestrator batches

# From anywhere by slug:
orchestrator batches --project my-project

# Explicit repo:
orchestrator batches --repo /path/to/repo
```

JSON output includes `batch_id`, `title`, `task_count`, and `path` for each batch.

## Batch-based business submission

Once a project is scaffolded and batches are created, the three-step pipeline is:

```bash
# Within the project directory — everything auto-discovered:
cd /path/to/my-project
orchestrator audit --batch 20260501A --write-generated
orchestrator approve --batch 20260501A --approved-by alice
orchestrator submit --batch 20260501A

# From anywhere using project slug:
orchestrator audit --project my-project --batch 20260501A --write-generated
orchestrator approve --project my-project --batch 20260501A --approved-by alice
orchestrator submit --project my-project --batch 20260501A
```

`--batch` accepts just the batch ID (e.g. `20260501A`), which is resolved to `docs/dora/batches/20260501A/` under the project root. Omit `--batch` to see available batches before selecting one.

`audit` validates the full batch structure, source hashes, Issue Packet completeness, dependency consistency, and Plane dry-run. `approve` records a user-approved hash and freezes the batch. `submit` runs only after approval and writes the Program Page, Batch Page, Root Epic Issue, and executable Task Issues.

With `ORCHESTRATOR_PLANE_BACKEND=local`, state is persisted in:

```text
<DORA_TARGET_REPO>/.dora/orchestrator-plane-state.json
```

## Live Plane backend

The live backend uses two Plane API surfaces:

- v1 API with `X-API-Key` for projects, cycles, modules, issues, comments, claims, and releases.
- internal Pages API with CSRF + `session-id` for Program Pages and Batch Pages.

Environment can be provided through process env, `~/.dora/plane.env`, or `~/dagster/.env`:

```bash
export PLANE_BASE_URL=http://plane.dev.internal
export PLANE_WORKSPACE_SLUG=doraemon
export PLANE_API_KEY=...

# Optional. If omitted, submit looks up or creates a Plane Project.
export PLANE_PROJECT_ID=...

# Required for Pages.
export PLANE_USER_EMAIL=...
export PLANE_USER_PASSWORD=...

# Required for live claim/release execution.
export PLANE_AGENT_UUID=...
```

Submit to real Plane:

```bash
export ORCHESTRATOR_PLANE_BACKEND=live

orchestrator submit --project my-project --batch 20260501A
```

## Spec-based execution smoke

The older JSON spec path remains a local executor smoke until batch submission and Plane-backed execution are implemented:

```bash
export ORCHESTRATOR_SPEC=/path/to/dora-orchestrator/examples/dora.orchestration.json
export DORA_TARGET_REPO=/path/to/your-project
export ORCHESTRATOR_EXECUTOR=noop
export ORCHESTRATOR_PLANE_BACKEND=local

python3 - <<'PY'
from orchestrator.config import load_config
from orchestrator.run_ready_task import run_ready_task

print(run_ready_task(load_config(), run_id="local-dry-run-1"))
print(run_ready_task(load_config(), run_id="local-dry-run-2"))
PY
```

With `ORCHESTRATOR_PLANE_BACKEND=local`, state is persisted in:

```text
<DORA_TARGET_REPO>/.dora/orchestrator-plane-state.json
```

The first run completes the ready task. The second run should return `no_ready` until the spec adds another ready task or the local state is intentionally reset.

## Example spec

```bash
python3 - <<'PY'
from pathlib import Path
from orchestrator.spec_loader import load_project_spec
from orchestrator.task_graph import build_task_graph

spec = load_project_spec(Path("examples/dora.orchestration.json"))
graph = build_task_graph(spec)
print(spec.project_slug, len(graph.tasks))
PY
```

## Environment variables

| Variable | Purpose |
|---|---|
| `DORA_TARGET_REPO` | Path to the business repository being orchestrated. |
| `ORCHESTRATOR_PLANE_BACKEND` | `memory` (default) / `local` / `live`. |
| `ORCHESTRATOR_EXECUTOR` | `claude` (default) / `codex` / `noop`. |
| `ORCHESTRATOR_BATCH_PATH` | Batch directory (used by Dagster assets). |
| `ORCHESTRATOR_PROJECT_SLUG`, `ORCHESTRATOR_PROJECT_TITLE` | Plane Project identity for batch submission. |
| `ORCHESTRATOR_RUN_ID` | Run identifier for the Dagster `ready_task_run` asset. |
| `ORCHESTRATOR_SPEC` | Legacy JSON spec path for the executor smoke. |
| `PLANE_*`, `DORA_HOME` | Live Plane backend (see above). |
