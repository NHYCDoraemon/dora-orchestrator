---
title: Orchestrator Module Baseline And Progress Planner Design
date: 2026-05-26
status: approved-for-planning
owner: raymond
---

# Orchestrator Module Baseline And Progress Planner Design

## Purpose

The orchestrator must stop treating a reviewed Excel/TSV requirement row or a
manually written broad task as the direct unit of executor work. Contract input,
module design, execution planning, Plane issue submission, executor context, and
acceptance evidence are separate lifecycle stages.

This design introduces a module baseline and module PROGRESS layer between
contract ledgers and Plane task batches:

```text
Excel / TSV contract
  -> contract ledger
  -> module map
  -> module baseline design
  -> module PROGRESS ledger
  -> batch plan
  -> TaskIssueDraft files
  -> Plane issues
  -> execution packet
  -> code, tests, evidence, and status updates
  -> module gate and final acceptance
```

## Current Problem

The current batch pipeline validates structure and source context, then submits
whatever `tasks/*.md` files already exist. It does not prove that a development
task is small enough, tied to a module baseline, or derived from a PROGRESS row.

This allowed `process-engine` batch `20260526A` to pass audit while containing
broad development tasks such as:

- T01 covering seven Excel capability IDs.
- T06/T08/T10 covering four capability IDs each.
- T09 covering six capability IDs.
- No `module_id`, `progress_task_id`, `row_id`, `source_tables`, or
  `source_queries` metadata on the development tasks.

Those tasks are valid markdown packets, but they are not fine-grained execution
units. The missing layer is the planner that compiles contract rows into module
baselines and module PROGRESS rows before Plane issue creation.

## Goals

- Make Excel/TSV rows contract inputs, not direct executor tasks.
- Introduce a stable module baseline layer for business boundary, database,
  code design, external ownership, lineage, and acceptance rules.
- Generate module PROGRESS ledgers as the canonical execution source.
- Generate TaskIssueDraft batches from PROGRESS rows, not from ad hoc broad
  issue descriptions.
- Make executor context complete enough for fine-grained development without
  asking the executor to infer module ownership or delivery boundaries.
- Add audit rules that reject broad development tasks before submit.
- Preserve existing execution packet, source evidence, strict ordering, and
  invalid submission protections.

## Non-Goals

- Do not build a full generic project-management system.
- Do not infer business design solely from code names.
- Do not force one Excel row to equal one code commit.
- Do not make Plane the source of truth for contract closure.
- Do not move domain-specific workflow semantics into platform libraries.

## Core Concepts

### Contract Ledger

The normalized source of contract rows. Each row has:

- `contract_id` such as `R031`.
- `source_path`, `sheet`, `source_row`, and source content hash.
- `capability_name`, `description`, current Excel status, priority, and
  original responsibility boundary.
- Existing code evidence and known gap text when supplied by the source.

### Module Baseline

A module baseline is a delivery module, not necessarily a Maven or code module.
Examples for `process-engine` include `runtime-flow`, `audit-lineage`,
`form-data`, `assignment-iam`, `batch-admin`, and `subprocess-actions`.

Each module baseline records:

- Business boundary and non-goals.
- Owned contract rows.
- Domain/application/infrastructure/API/database boundaries.
- Data lineage from user/API action to domain object, tables, events, audit,
  projections, and replay/query surface.
- Platform ownership decision:
  `ENGINE_DOMAIN_ONLY`, `REUSE_PLATFORM`, `ENHANCE_PLATFORM`,
  `INCUBATE_IN_ENGINE`, `EXTERNAL_CONTRACT`, or `REJECT_PLATFORMIZATION`.
- Verification strategy and module gate rules.

### Module PROGRESS Ledger

The module PROGRESS ledger is the executable planning source. It breaks module
baseline work into atomic progress rows. A row may represent design, evidence,
test-first verification, implementation, migration, API, or module gate work.

Development-class rows should normally map to exactly one `progress_task_id`.
One Excel row can produce many PROGRESS rows. One PROGRESS row can reference
multiple contract rows only when they are an inseparable technical closure and
the module baseline explains why.

### Plane Batch

A Plane batch is only a delivery projection of selected PROGRESS rows. It is not
the planning source. Re-submitting or splitting batches must not mutate the
contract ledger or module baseline without regenerating hashes.

### Execution Packet

The execution packet is the full context given to the executor. It must include
the current PROGRESS row, its contract slices, module baseline, source bundle,
verification commands, stop conditions, and required result signal.

## Required Inputs

The planner consumes these inputs:

1. Contract inputs:
   - Excel workbooks, TSV, CSV, or markdown tables.
   - Sheet/table configuration and row identity columns.
   - Optional priority roadmap and excluded rows.

2. Authoritative project inputs:
   - Project instructions such as `AGENTS.md`.
   - Architecture, engineering, and coding conventions.
   - ADRs and platform governance rules.

3. Code baseline inputs:
   - Target repository path and current git SHA.
   - Source tree, migrations, API DTOs, tests, and known verification commands.

4. Historical evidence inputs:
   - Existing Dora batch evidence.
   - Audit reports and closure ledgers.
   - Existing `PROGRESS.md` files.
   - Plane state as a hint only, never as closure truth.

5. Execution policy inputs:
   - Project slug, program id, batch id prefix, executor type.
   - Verification levels.
   - Allowed external exception classes.
   - Module splitting and task-size policy.

## Generated Artifacts

The planner writes deterministic artifacts under the target repository:

```text
docs/dora/contracts/<contract-set-id>/contract-ledger.tsv
docs/dora/contracts/<contract-set-id>/module-map.tsv
docs/dora/modules/<module-id>/baseline.md
docs/dora/modules/<module-id>/progress.tsv
docs/dora/modules/<module-id>/gate.md
docs/dora/batches/<batch-id>/batch-plan.md
docs/dora/batches/<batch-id>/batch.md
docs/dora/batches/<batch-id>/program-page.md
docs/dora/batches/<batch-id>/summary.md
docs/dora/batches/<batch-id>/tasks/*.md
```

The exact root paths may be made configurable, but the artifact roles are fixed.

## PROGRESS Row Schema

Every executable row must contain:

| Field | Meaning |
|---|---|
| `progress_task_id` | Stable row id, unique inside the module or project. |
| `module_id` | Owning delivery module. |
| `contract_ids` | Contract rows covered by this progress row. |
| `task_kind` | `design`, `evidence_only`, `test_first`, `implementation`, `migration`, `api`, `external_contract`, or `module_gate`. |
| `closure_state` | Current planning state. |
| `business_closure` | Business closure expectation or missing evidence. |
| `lineage_closure` | Data lineage expectation or missing evidence. |
| `code_scope` | Allowed code areas. |
| `db_scope` | Tables/migrations/projections involved. |
| `platform_decision` | Platform ownership decision. |
| `depends_on` | Progress dependencies, not broad task labels. |
| `verification_commands` | Commands required before `done`. |
| `done_signal` | Required evidence and status update. |
| `no_go_signal` | Conditions that force partial or needs_input. |

## Closure State Model

The planner and executor use these states:

- `UNASSESSED`: imported but not adjudicated.
- `EVIDENCE_MISSING`: code may exist, but closure evidence is insufficient.
- `DESIGN_REQUIRED`: module boundary or ADR decision is missing.
- `CODE_REQUIRED`: implementation is missing or incomplete.
- `TEST_REQUIRED`: implementation exists, but tests do not prove closure.
- `EXTERNAL_BLOCKED`: required external contract or permission is missing.
- `PARTIAL`: some closure evidence exists, but delivery is not complete.
- `CLOSED`: module baseline, code, tests, lineage, and evidence all close.

Plane `Done` is not equivalent to `CLOSED`. `CLOSED` belongs to the PROGRESS
ledger, module gate, and final acceptance artifacts.

## Planner Flow

### Stage 1: Contract Normalize

Read Excel/TSV inputs and produce `contract-ledger.tsv`. Hash every source row.
Excluded rows are kept in the contract set with an exclusion reason, not lost.

### Stage 2: Module Assignment

Assign each non-excluded contract row to a `module_id`. Assignment can be
explicit, rule-based, or reviewed. Rows without a module are not executable.

### Stage 3: Module Baseline Generate

For each module, create or update `baseline.md`. If existing code already
appears to satisfy the contract, the baseline must still record why the business
closure and lineage closure are complete. If a design decision is not covered by
existing ADRs or conventions, the module emits `DESIGN_REQUIRED`.

### Stage 4: PROGRESS Generate

Generate `progress.tsv` rows from module baselines. Existing code can produce
`evidence_only` or `test_first` rows. Missing implementation produces
`implementation`, `migration`, or `api` rows. External ownership produces
`external_contract` rows.

### Stage 5: Batch Plan

Select PROGRESS rows for one or more batches based on dependency, priority,
risk, and size. The batch plan should prefer small tasks and module-local
sequencing. Different modules may be emitted as separate batches when there is
no dependency.

### Stage 6: TaskIssueDraft Generate

Generate `tasks/*.md` from selected PROGRESS rows. Development-class tasks must
carry:

- `module_id`
- `progress_schema`
- `progress_task_id`
- `row_id`
- `task_kind`
- `contract_ids` or `covers`
- `source_tables` / `source_queries` through batch metadata
- required skills and verification commands when known

### Stage 7: Audit And Submit

`orchestrator audit` validates that development tasks are generated from module
PROGRESS rows and have complete source context. `submit` remains all-or-nothing
and writes only audited, approved tasks to Plane.

### Stage 8: Execute And Update

`run_ready` builds an execution packet from the Plane issue metadata, source
bundle, module baseline, and progress slice. The executor returns a result
signal. The orchestrator updates run reports and progress projection artifacts.

### Stage 9: Module Gate

Each module has a gate task that recomputes module closure from progress rows,
evidence files, and verification commands. A module can be `GO` only when all
required rows are `CLOSED` or explicitly accepted external contracts.

## Audit Rules

Add audit rules for generated development batches:

1. A task with `task_kind` in `implementation`, `migration`, `api`, or
   `test_first` must have `module_id`, `progress_schema`, `progress_task_id`,
   and `row_id`.
2. A development task should cover exactly one `progress_task_id`.
3. A development task covering multiple contract IDs must cite a module baseline
   section explaining why those IDs are inseparable.
4. A broad `covers: R001-R099` range is allowed only for audit/gate tasks.
5. Missing `source_tables` / `source_queries` for progress-controlled batches is
   an audit failure unless the task is explicitly `design` and has source docs.
6. `EXTERNAL_CONTRACT` rows must include the external owner and engine-side
   deliverable.
7. `CLOSED` cannot be set by a task body alone; it requires evidence and module
   gate recomputation.
8. Historical broad batches may be referenced as evidence sources but must not
   be treated as executable fine-grained plans.

## Executor Context Requirements

Every generated executor prompt must include:

- Current `progress_task_id` and `module_id`.
- Contract row slice with original text and source hash.
- Module baseline path and required sections to read.
- Current PROGRESS row slice.
- Task kind and permitted edit scope.
- Business closure and lineage closure requirements.
- DB/API/event/audit expectations.
- Platform ownership decision and external boundary.
- Existing code evidence hints.
- Verification commands.
- Required final `RESULT: <progress_task_id> <done|partial|needs_input> - <reason>` signal.

If these inputs cannot be built, the issue is an orchestrator invalid submission,
not a business `Needs Input`.

## CLI Shape

Add planner commands without changing existing submit semantics:

```bash
orchestrator plan-contract \
  --repo /path/to/repo \
  --project process-engine \
  --input process-engine_需求实现对照清单_v2.xlsx \
  --contract-set 20260526-excel-v2

orchestrator plan-modules \
  --repo /path/to/repo \
  --contract-set 20260526-excel-v2

orchestrator plan-progress \
  --repo /path/to/repo \
  --contract-set 20260526-excel-v2

orchestrator plan-batches \
  --repo /path/to/repo \
  --contract-set 20260526-excel-v2 \
  --program P1R12 \
  --batch-prefix 20260526B
```

The names may be consolidated later, but the pipeline stages must remain
separable and testable.

## Migration For Existing Broad Batches

Existing broad batches such as `20260526A` should be treated as historical
evidence inputs:

- Keep their Plane and evidence history.
- Import their produced code and evidence into the relevant module baselines.
- Re-adjudicate each contract row against current code and evidence.
- Generate new module PROGRESS rows for remaining gaps.
- Do not mutate already submitted historical task packets to pretend they were
  fine-grained.

## Failure Semantics

- Missing contract source: audit failure.
- Missing module assignment: planning failure.
- Missing module baseline: planning failure or generated `DESIGN_REQUIRED`.
- Missing progress metadata on development tasks: audit failure.
- Missing execution packet context at runtime: orchestrator invalid submission.
- Executor cannot prove source reads: source evidence failure.
- Executor finds real external business blocker: `needs_input` result signal.

## Test Plan

Focused tests should cover:

- Contract normalization from an XLSX fixture into `contract-ledger.tsv`.
- Module assignment emits `module-map.tsv` and rejects unassigned rows.
- Module baseline generation records required business and lineage sections.
- PROGRESS generation creates multiple rows for a single complex contract row.
- Batch generation produces one TaskIssueDraft per selected PROGRESS row.
- Audit rejects development tasks without `module_id` or `progress_task_id`.
- Audit rejects broad development tasks covering multiple unrelated contract IDs.
- Audit allows broad coverage only for `design`, `evidence_only`, or
  `module_gate` tasks when source context is complete.
- Submit still writes execution packet metadata for valid generated tasks.
- Run-ready executor prompt contains contract slice, module baseline, and
  progress slice.

## Compatibility

Existing approved batches remain valid historical records. New strict audit
rules should first apply to batches that declare planner metadata or a new batch
schema version such as `batch_schema: module-progress/v1`. After existing broad
work is migrated, the strict rules can become default for all development
batches.

## Open Decisions Resolved Here

- Excel/TSV is a contract input, not an execution source.
- Module baseline is the design and ownership source.
- Module PROGRESS is the execution source.
- Plane batches are delivery projections of PROGRESS rows.
- Execution packets must be complete enough for fine-grained development.
- Module gates, not Plane `Done`, decide delivery closure.
