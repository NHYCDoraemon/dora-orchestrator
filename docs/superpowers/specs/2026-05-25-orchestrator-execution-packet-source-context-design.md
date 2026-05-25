---
title: Orchestrator Execution Packet Source Context Design
date: 2026-05-25
status: approved-for-planning
owner: raymond
---

# Orchestrator Execution Packet Source Context Design

## Purpose

The current orchestrator can submit and run batch tasks, but the executor receives only a rendered Issue Packet plus generic operating rules. Source documents and TSV/CSV requirement tables are checked during batch audit and included in approval hashes, yet they are not delivered to the executor as a required execution input. This creates a real scheduling contract gap: an agent can implement from a summarized task while missing the original docs and data rows that define the work.

This design upgrades batch execution from "task description only" to a complete, verifiable Execution Packet. The packet must include required source docs, required table slices, hashes, query details, and read evidence requirements. A task cannot reach `Done` unless the executor both passes verification and proves it read the required source context.

## Goals

- Make source docs and TSV/CSV tables first-class execution inputs.
- Generate a source bundle and task-specific table slices before executor startup.
- Require executor prompts to name the exact source files and slices that must be read.
- Verify read evidence from executor event streams after execution.
- Block legacy incomplete issues that lack source execution context.
- Keep Plane lightweight by storing metadata and artifact paths, not large source bodies.
- Preserve existing skills, progress metadata, strict batch ordering, stale claim recovery, and verification command behavior.

## Non-Goals

- Do not copy full source documents into Plane.
- Do not copy large TSV/CSV files into executor prompts.
- Do not execute arbitrary scripts, SQL, or DuckDB queries for source slicing.
- Do not allow legacy incomplete tasks to keep running under the old contract.
- Do not make executors responsible for Plane claim, release, heartbeat, or source context bookkeeping.

## Existing Behavior

Batch tasks currently declare fields such as `source_pages`, `source_docs`, and `source_summaries`. The audit path checks those paths exist, and the approval hash includes their contents. During submit, however, the Plane issue only persists task body plus limited system metadata such as `source_hash`, `agent_hint`, `risk`, dependencies, skills, progress fields, and verification level.

At run time, `run_ready_task.py` renders a prompt from the claimed Plane issue. The prompt contains the Issue Packet and unattended operating rules. It does not include a source manifest, source file paths, table schemas, query details, or table slices. The executor chooses what to inspect, and the orchestrator later relies on `verification_commands` and exit status. This means source context is part of approval integrity, but not part of execution integrity.

## Design Overview

The selected approach is `Execution Packet + Evidence Gate`.

The orchestrator will build a source execution packet before starting the executor. The packet contains a manifest of required source docs and required table slices. The executor prompt will require reading the generated bundle, all required docs, and all required slices. After the executor exits and verification commands run, the orchestrator will parse the event stream and gate completion on evidence that those files were actually accessed.

The change is intentionally kept inside the existing orchestrator process. It adds small source-context modules and wires them into the existing audit, submit, Plane serialization, prompt rendering, run artifacts, and outcome logic.

## New Modules

### `source_manifest.py`

Responsibilities:

- Parse source metadata from batch task frontmatter and Plane issue frontmatter.
- Normalize and validate source paths.
- Compute source file hashes, file sizes, and file types.
- Build the per-task manifest used by source bundle generation.
- Reject path escapes by default.
- Provide stable JSON-serializable structures for submit, prompt rendering, run reports, and tests.

### `source_slicing.py`

Responsibilities:

- Execute the restricted YAML query DSL for TSV and CSV files.
- Read headers and validate requested columns.
- Apply filters in a deterministic order.
- Write task-specific slices under `.dora/loop-runs/<run_id>/sources/`.
- Enforce row and output size limits.
- Record row counts, selected columns, filter summaries, and slice hashes.

### `source_evidence.py`

Responsibilities:

- Parse Claude stream-json and Codex `--json` event files.
- Extract file read evidence from `Read`, `Grep`, `Glob`, and shell commands such as `cat`, `sed`, `awk`, `head`, `tail`, `rg`, and `grep`.
- Normalize accessed paths relative to the executor worktree.
- Check that required files were accessed.
- Write `source-evidence.json` and `source-evidence.md`.

## Batch Schema

Existing fields remain valid:

```yaml
source_pages:
  - docs/specs/product.md
source_docs:
  - docs/design.md
source_summaries:
  - docs/summary.md
```

New table fields:

```yaml
source_tables:
  - path: docs/requirements/original.tsv
    required: true
    label: original_requirements
    format: tsv
    key_columns:
      - row_id
      - route_path
```

New query fields:

```yaml
source_queries:
  - table: original_requirements
    required: true
    filters:
      - column: row_id
        op: equals
        value_from: row_id
      - column: route_path
        op: contains
        value_from: route_path
    columns:
      - row_id
      - route_path
      - requirement
      - acceptance
      - notes
    max_rows: 50
    sample_rows: 5
```

Rules:

- `source_tables` identifies source data files by stable labels.
- `source_queries` is the preferred source of slice generation.
- If `source_queries` is missing, the orchestrator may use limited inference only from explicit progress metadata such as `row_id`, `route_path`, `backend_contract`, and `progress_task_id`.
- If explicit queries are absent and inference cannot produce a deterministic slice, audit fails.
- Required source docs and required table slices must be read by the executor.
- `execution_packet_version` and `execution_packet_hash` must be persisted to Plane issue frontmatter before the issue is executable.

## Query DSL

The query DSL is intentionally constrained. It supports:

- `table`: required table label.
- `required`: boolean, default true for task-critical queries.
- `filters`: list of column filters.
- `columns`: output columns for the slice.
- `max_rows`: maximum rows in the slice.
- `sample_rows`: optional sample row count for manifest summaries.

Supported filter operators:

- `equals`
- `contains`
- `regex`
- `in`

Filter values can be literal `value` or task metadata references through `value_from`. `value_from` must resolve to a scalar or list in task metadata or extracted Plane issue metadata. Missing values are hard failures for required queries.

The DSL does not support joins, scripts, arbitrary expressions, SQL, external processes, or network access.

## Execution Packet Artifacts

For each run, the orchestrator creates:

```text
.dora/loop-runs/<run_id>/
  prompt.md
  events.ndjson
  summary.md
  verify.txt
  source-bundle.md
  source-manifest.json
  source-evidence.json
  source-evidence.md
  sources/
    <table-label>.slice.tsv
```

`source-bundle.md` is a manifest and reading contract. It does not embed full source docs.

Example:

```md
# Source Bundle

## Required Reading

- docs/design.md
  - sha256: <hash>
  - required: true

## Required Tables

- original_requirements
  - source: docs/requirements/original.tsv
  - slice: .dora/loop-runs/<run_id>/sources/original_requirements.slice.tsv
  - query: row_id equals F97-036; route_path contains /forms/list

## Evidence Required

Executor must read:

- this source-bundle.md
- all required source docs
- all required slice files
```

`source-manifest.json` contains the machine-readable version of the same packet. It includes normalized paths, hashes, file sizes, table schemas, slice paths, query summaries, and required evidence targets.

## Prompt Contract

`_render_executor_prompt()` gains a `Source Context Contract` section before the Issue Packet. It names:

- `source-bundle.md`
- required source docs
- required table slices
- the consequence of not reading them

The executor is instructed to read the source bundle first, then read each required source doc and slice before making implementation decisions. The existing unattended rules remain unchanged: stay in the worktree, do not commit, do not call Plane lifecycle tools, and exit when acceptance is satisfied.

## Run Flow

1. Producer writes task markdown with source docs, source tables, and source queries.
2. `orchestrator audit` validates source fields, source paths, table headers, query DSL, and required query determinism.
3. `orchestrator approve` recomputes the source-aware batch hash and writes `approval.json`.
4. `orchestrator submit` persists execution packet metadata into Plane issue frontmatter.
5. Dagster probes Plane for ready issues.
6. Ready selection skips root epics and respects strict batch order, dependencies, priorities, and stale-claim safety.
7. Source context preflight runs before `claim_issue()`. If the issue lacks a valid `execution_packet_version` or packet metadata, the executor is not started and the issue is moved to `Needs Input`.
8. Valid issues are claimed.
9. After claim, the orchestrator creates run artifacts.
10. Source manifest and source slices are generated.
11. Prompt is rendered with the Source Context Contract.
12. Executor runs in the task worktree.
13. Event stream is captured to `events.ndjson`.
14. Verification commands run.
15. Source Evidence Gate parses events and writes evidence artifacts.
16. Outcome is computed from executor result, verification result, and source evidence result.
17. Plane run report includes source manifest path, evidence path, and missing evidence if any.
18. Git delivery stages and commits changes when configured.
19. Worktree cleanup follows existing rules.

## Outcome Rules

Source outcomes are hard gates:

- Missing source context before executor startup: do not start executor, release `Needs Input`, add `needs:source-context`.
- Source slice generation failure: do not start executor, release `Needs Input`, add `needs:source-slice`.
- Source evidence failure after executor completion: release `Needs Input`, add `needs:source-evidence`.
- Verification failure: use the existing verification failure behavior.
- Verification pass plus source evidence pass: allow `Done`.

If source evidence fails after the executor produced file changes, git delivery may create a WIP commit for inspection, but auto-merge must be blocked.

## Legacy Compatibility

Legacy incomplete Plane issues without `execution_packet_version` are not executable. The orchestrator must release them to `Needs Input`, label them `needs:source-context`, and comment that the batch must be regenerated or resubmitted with a source execution packet.

Already completed legacy issues are not retroactively changed.

New batch submissions must use the new execution packet schema. The submit path must not silently overwrite or weaken existing issue metadata.

## Error Handling

### Source Context Errors

Examples:

- Missing `execution_packet_version`.
- Missing source docs.
- Missing source tables.
- Missing source queries.
- Missing required task metadata for query values.

Result: executor does not start. The task is released to `Needs Input` with `needs:source-context`.

### Source Slice Errors

Examples:

- TSV/CSV file missing.
- Header cannot be parsed.
- Requested filter column missing.
- Requested output column missing.
- Required query returns no rows.
- Slice exceeds configured row or size limits.

Result: executor does not start. The task is released to `Needs Input` with `needs:source-slice`.

### Source Evidence Errors

Examples:

- `events.ndjson` cannot be parsed.
- Executor did not read `source-bundle.md`.
- Executor did not read a required source doc.
- Executor did not read a required slice.

Result: task is released to `Needs Input` with `needs:source-evidence`. Verification results are still recorded, but they cannot promote the task to `Done`.

## Plane Serialization

Live Plane issue frontmatter must include enough source metadata to reconstruct the execution packet after claim:

- `execution_packet_version`
- `execution_packet_hash`
- `source_pages`
- `source_docs`
- `source_summaries`
- `source_tables`
- `source_queries`
- existing dependencies, skills, progress metadata, risk, source hash, and verification level

The current live backend behavior that loses structured `verification_commands` must be fixed as part of this work. Verification commands are execution-critical and must be restored from Plane issue metadata.

## Observability

Plane comments should remain concise. Run reports include:

- executor outcome
- verification result
- source context result
- source manifest path
- source evidence path
- missing evidence list
- slice generation errors when present

`orchestrator status` should surface counts and examples for:

- `needs:source-context`
- `needs:source-slice`
- `needs:source-evidence`
- legacy blocked issues

The batch page should not embed large source data. It may link to source artifact paths and summarize source readiness.

## Testing Strategy

### Batch Loader and Audit

- Parses `source_tables` and `source_queries`.
- Fails when required source docs, tables, or queries are missing.
- Fails when table headers cannot satisfy query columns.
- Fails when `value_from` references missing metadata.
- Fails when required query produces no rows.
- Includes source docs, tables, and queries in approval hash.

### Submit and Plane Serialization

- In-memory backend preserves source metadata.
- Live Plane issue frontmatter includes source refs, queries, packet version, and packet hash.
- `_adapt_issue()` restores source metadata from `description_html`.
- Verification commands survive live serialization and adaptation.

### Source Bundle and Slice Generation

- Generates `source-bundle.md`.
- Generates `source-manifest.json`.
- Generates `sources/*.slice.tsv`.
- Enforces selected columns, `max_rows`, and `sample_rows`.
- Records hashes and row counts.

### Evidence Gate

- Fails if bundle is not read.
- Fails if required source docs are not read.
- Fails if required slices are not read.
- Passes when bundle, docs, and slices are read.
- Supports Claude stream-json and Codex `--json` events.

### Run Orchestration

- Source context missing prevents executor startup.
- Slice failure prevents executor startup.
- Evidence failure prevents `Done`.
- Verification pass plus evidence pass allows `Done`.
- Existing strict batch order, stale claim recovery, progress metadata, and skills sections still work.

### Delivery and Status

- Evidence failure can create WIP commit but blocks auto-merge.
- `status` reports source blocked tasks.
- Live backend reporting does not depend on in-memory-only `issues` storage.

## Acceptance Criteria

- A submitted live Plane issue contains enough source metadata to reconstruct the Execution Packet.
- Executor prompt lists required source bundle, docs, and slices.
- Large TSV/CSV inputs enter execution through deterministic slices.
- A task that does not read required source context cannot reach `Done`.
- Legacy incomplete tasks without source execution context cannot run automatically.
- Run reports expose source manifest and evidence artifacts.
- Existing orchestrator features remain compatible.
