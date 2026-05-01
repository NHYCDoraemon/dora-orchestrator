# dora-orchestrator

Dagster-first orchestration platform for Dora-style autonomous engineering workflows.

This project is intentionally independent from any single business repository. Repositories such as `dora`, `dora-code`, or future service projects provide structured orchestration specs; this orchestrator provisions Plane, schedules Dagster runs, invokes executors, verifies deliverables, and keeps git history free of lock/heartbeat noise.

## Responsibilities

- Compile project specs into executable task graphs.
- Provision Plane projects, cycles, modules, issues, dependencies, and summary links.
- Own Dagster jobs, assets, sensors, retry, concurrency, and run history.
- Invoke executor backends through a stable protocol.
- Enforce git delivery policy.

## Current status

Phase 1 foundation:

- TaskGraph validation and deterministic source hashes.
- JSON spec loader.
- Fake-client Plane provisioning seam.
- Noop/Codex/Claude executor adapters.
- Minimal Dagster definitions that import when Dagster is installed.
- Unit tests that do not require network or model calls.

## Local verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Dagster definition tests skip when Dagster is not installed.

## Example spec

```bash
python3 - <<'PY'
from pathlib import Path
from dora_orchestrator.spec_loader import load_project_spec
from dora_orchestrator.task_graph import build_task_graph

spec = load_project_spec(Path("examples/dora.orchestration.json"))
graph = build_task_graph(spec)
print(spec.project_slug, len(graph.tasks))
PY
```

