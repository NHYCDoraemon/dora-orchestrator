# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Repository purpose

`orchestrator` is the **platform-side** of autonomous engineering. It is intentionally separate from any business repo (`dora`, `dora-code`, etc. — `dora` in particular is a different agent project, not this one). Business repos produce *reviewed TaskIssueDraft batches* under `docs/dora/batches/<batch-id>/`; this repo audits those batches, submits Issue Packets to Plane, drives execution through Dagster, invokes executor backends, and enforces git delivery policy.

The authoritative design document is `docs/specs/2026-05-01-dora-orchestration-design.md`. Read it before making non-trivial changes to batch flow, hashing, or Plane semantics.

## Common commands

Tests use `unittest` (no pytest). The package has zero required runtime deps; `dagster` is an optional extra used only by `orchestrator/dagster_defs/`.

```bash
# Full test suite
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'

# Single test module / case
python3 -m unittest tests.test_batch_audit
python3 -m unittest tests.test_batch_audit.BatchAuditTest.test_passes_full_batch
```

Dagster-dependent tests (`tests/test_dagster_definitions.py`) auto-skip when `dagster` is not installed. To exercise them: `pip install -e ".[dagster]"`.

The repo has no lint/format config. Style follows what's already in the tree: `from __future__` not used, `dataclass(frozen=True)` everywhere, `pathlib.Path`, `dict[str, X]` PEP-585 generics (requires-python ≥ 3.10).

## CLI

A single `orchestrator` binary (registered via `[project.scripts]` in `pyproject.toml`, dispatched by `orchestrator/cli.py`) routes to five subcommands:

```
orchestrator audit      ─► orchestrator.audit_batch:main
orchestrator approve    ─► orchestrator.approve_batch:main
orchestrator submit     ─► orchestrator.submit_batch:main
orchestrator scaffold   ─► orchestrator.scaffold:main
orchestrator provision  ─► orchestrator.provision:main
```

Each subcommand module exposes `main(argv: list[str] | None = None) -> int`. The dispatcher forwards the remaining args via `argparse.REMAINDER`. When adding a new subcommand: register it in `SUBCOMMANDS` in `cli.py` and ensure `main()` matches the signature.

Install once with `pipx install --editable .` (or `pip install -e .`) so `orchestrator` is on PATH from any directory.

## Module taxonomy and Plane semantics — invariants

These are **fixed contracts**, not preferences:

- **One repo → one Plane Project.** Batches are appended to that Project; never create a Project per batch.
- **Fixed module taxonomy** (`orchestrator/batch_models.py:FIXED_MODULE_TAXONOMY`): `product, architecture, planning, implementation, verification, operations, governance`. Audit rejects any other module; do not extend without updating the design doc.
- **Required Issue Packet sections** (`REQUIRED_ISSUE_PACKET_SECTIONS`) must be present, non-empty, and in order. Every executable Plane issue is a complete packet — never just a title and links.
- **Task ID scheme**: `<PROJECT>-<PROGRAM>-<YYYYMMDDA>-T<NN>` enforced by `TASK_ID_RE` in `batch_audit.py`. The batch segment of every task ID must match the batch dir name; sequence segment must match `sequence` metadata.
- **Plane cycle semantics stay native.** Missing cycles are *previewed* during audit and *created* only after approval. Do not invent a phase system on top of cycles.
- **Approval is whole-batch.** Updates ship as a *new* batch — never silently rewrite a submitted Plane issue. The approval hash (`batch_hash.py`) covers `batch.md` (normalized), `program-page.md`, every task file, `submit-preview.md`, `.dora/project.json`, and every declared source path / commit. Changing what goes into the hash is a breaking change to the approval contract.

## Architecture (the parts that span files)

### Pipeline
```
docs/dora/batches/<id>/  ──►  audit_batch  ──►  approve_batch  ──►  submit_batch  ──►  Plane Project
                              (validates,        (freezes hash,      (Program Page,
                               writes preview)    approval.json)      Batch Page,
                                                                      Root Epic Issue,
                                                                      Task Issues)
                                                                                        │
                                                                                        ▼
                                                                            Dagster `run_ready_task`
                                                                                        │
                                                                                        ▼
                                                                          Executor (noop / codex / Codex)
```

### Plane backend abstraction
`orchestrator/plane_backends.py:create_plane_client` selects from three implementations based on `OrchestratorConfig.plane_backend`:

- `memory` — `InMemoryPlaneClient` (default; tests, dry-runs).
- `local` — `LocalPlaneClient` persists to `<DORA_TARGET_REPO>/.dora/orchestrator-plane-state.json`.
- `live` — `LivePlaneClient` uses **two** Plane API surfaces simultaneously: v1 REST with `X-API-Key` for projects/cycles/issues, and the *internal* Pages API with login + CSRF + `session-id` for Program/Batch Pages. Configuration via env, `~/.dora/plane.env`, or `~/dagster/.env`.

All three implement the same duck-typed interface used by `plane_provisioner.py`, `batch_submit.py`, and `run_ready_task.py` (`upsert_project/cycle/module/issue`, `next_ready_issue`, `claim_issue`, `release_issue`, `publish_run_report`, ...). When adding a method, add it to *all three* or feature-detect at the call site.

### Executor protocol
`executor_protocol.py` defines the `Executor` Protocol (`run(TaskRunContext) -> ExecutorResult`). Executors are pulled lazily by name in `executors/__init__.py:get_executor` (`noop`, `codex`, `Codex`). The `noop` executor has no third-party imports; codex/Codex imports are deferred so tests don't require those SDKs.

### Dagster layer (optional)
`orchestrator/dagster_defs/` is the only place that imports `dagster`. `dora_definitions.py` exports `defs` (assets + jobs + sensors). Assets read config from env via `load_config()` — `ORCHESTRATOR_BATCH_PATH`, `ORCHESTRATOR_PROJECT_SLUG`, `ORCHESTRATOR_PROJECT_TITLE`, `ORCHESTRATOR_RUN_ID` are required for the batch-submission and ready-task assets respectively.

### Config
All runtime config flows through `OrchestratorConfig` (`config.py`) loaded from env. The relevant vars:

- `DORA_TARGET_REPO` — business repo this orchestrator operates on (path). **Kept under the `DORA_` prefix because it points at the dora business repo, not at the orchestrator.**
- `ORCHESTRATOR_PLANE_BACKEND` — `memory` | `local` | `live`.
- `ORCHESTRATOR_EXECUTOR` — `noop` | `codex` | `Codex`.
- `ORCHESTRATOR_BATCH_PATH`, `ORCHESTRATOR_PROJECT_SLUG`, `ORCHESTRATOR_PROJECT_TITLE` — batch submission.
- `ORCHESTRATOR_RUN_ID` — Dagster `ready_task_run` asset.
- `ORCHESTRATOR_SPEC` — legacy JSON spec path (used by the older `run_ready_task` smoke; the batch path is the long-term direction).
- `DORA_HOME`, `PLANE_*` — live backend (see README "Live Plane backend"). `DORA_HOME` stays `DORA_*` because it points at `~/.dora/`.

## Working with batches locally

Typical loop when iterating on the audit/submit flow:

```bash
export DORA_TARGET_REPO=/Users/raymond/GolandProjects/dora
export ORCHESTRATOR_PLANE_BACKEND=local

orchestrator audit \
  --batch docs/dora/batches/20260501A --repo "$DORA_TARGET_REPO" --write-generated --json
orchestrator approve \
  --batch docs/dora/batches/20260501A --repo "$DORA_TARGET_REPO" --approved-by raymond --json
orchestrator submit \
  --batch docs/dora/batches/20260501A --repo "$DORA_TARGET_REPO" \
  --project-slug dora --project-title Dora --json
```

Switching to `ORCHESTRATOR_PLANE_BACKEND=live` requires the full `PLANE_*` env (see README). Auditing is offline and never needs Plane credentials.

## Things that look like cleanup but aren't

- The package directory is `orchestrator/` even though the repo dir is still `dora-orchestrator/`. The repo dir name is incidental; the import path and binary are both `orchestrator`.
- `.dora/` directories (scaffold output, local plane state) are kept under that name on purpose — they live inside the dora business repo's filesystem and are read by dora-side tooling, so renaming them would break that contract.
- The legacy JSON spec path (`spec_loader.py`, `examples/dora.orchestration.json`, `run_ready_task.py`) coexists with the batch flow on purpose — it is the executor smoke until live Plane execution is fully wired. Don't delete it without coordinating with the design doc.
- `dagster_defs/sensors.py` is intentionally `SENSORS = []` in Phase 1 (live Plane resources not yet wired). Adding a sensor before the live resources land will break the Definitions import.
- `task_graph.py` / `models.py` are tied to the JSON-spec path; the batch flow has its own `batch_models.py`. They are not redundant.

## Imported Claude Cowork project instructions
