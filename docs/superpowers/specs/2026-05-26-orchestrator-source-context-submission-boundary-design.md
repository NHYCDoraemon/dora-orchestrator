---
title: Orchestrator Source Context Submission Boundary Design
date: 2026-05-26
status: approved-for-planning
owner: raymond
---

# Orchestrator Source Context Submission Boundary Design

## Purpose

The orchestrator must not let incomplete source context reach Plane live and then rely on the runner to discover it. Missing source documents, source tables, source queries, task markdown, summaries, or program pages are packaging and submission-chain defects. They are not business task blockers and must not be represented as task `Needs Input`.

This design tightens the lifecycle boundary around source context:

- `audit` proves the batch is executable.
- `approve` freezes only a complete, audited packet and hash.
- `submit` writes only an already approved and revalidated packet to Plane.
- `run_ready` treats unexpected source-context defects as invalid orchestrator submission, not human input.

## Current Problem

The current Execution Packet v1 flow correctly adds source metadata and source bundles to the runner path, but it still classifies runner-discovered source context failures as `source_context_missing`, releases the task to `Needs Input`, and labels it `dora:source-context-missing`.

That state is misleading. A missing source file or missing execution packet after submission means the audit/approval/submit chain accepted an invalid package. It should block orchestration and operator remediation, but it should not pollute business task state or compete with real `Needs Input` cases such as missing business samples, unresolved ADR decisions, or missing production permissions.

## Decisions

### 1. Audit Owns Executability

`orchestrator audit` must validate that the batch can be executed from the planned baseline:

- `batch.md` exists, has required metadata, and parses.
- `program-page.md` exists and parses.
- `tasks/*.md` exists, parses, and every task has required Issue Packet sections.
- Each task declares non-empty `source_pages`, `source_docs`, and `source_summaries`.
- Every declared source page/doc/summary path exists and resolves inside the target repository or batch directory according to existing path rules.
- Batch-level `source_tables` and `source_queries` parse, reference known tables, use supported formats/operators, and have deterministic output paths.
- Required source table files exist.
- Required source queries render successfully for every task that depends on them.
- A per-task source bundle dry-run can be built from the exact metadata that submit will serialize to Plane.

Audit remains offline and never needs Plane credentials.

### 2. Approve Freezes Only Audited Packets

`orchestrator approve` approves a batch that has already passed audit. The approval hash remains the existing authority over source inputs:

- normalized `batch.md`;
- `program-page.md`;
- task markdown files;
- `submit-preview.md`;
- `.dora/project.json` when present;
- declared source docs/pages/summaries;
- declared source tables and queries;
- declared source commits.

Generated `.dora/source-bundles/` artifacts are not added to the approval hash. They are deterministic runtime/preflight artifacts derived from the approved packet, not a new source of truth.

### 3. Submit Revalidates Before Plane Writes

`orchestrator submit` must be all-or-nothing for Plane writes:

- load the batch;
- rerun audit;
- load and validate `approval.json`;
- recompute and compare the approval hash;
- build the same per-task source execution metadata that will be written to Plane;
- run source bundle preflight for every task against the target repo;
- only then call `upsert_project`, `upsert_page`, `upsert_issue`, `add_label`, or any live Plane API.

If any source context preflight fails, submit raises a local error and performs zero Plane writes. This is especially important for `ORCHESTRATOR_PLANE_BACKEND=live`.

### 4. Run Ready Handles Invalid Submission Defensively

`run_ready` keeps a defensive source-context preflight before executor startup, but this is no longer a normal task outcome. If it finds:

- missing `execution_packet_version`;
- missing `source_docs`, `source_tables`, or `source_queries`;
- invalid source paths;
- missing required source docs or tables;
- failed required source query or bundle generation;

then it must:

- not start the executor;
- not claim the issue when the defect is visible before claim;
- classify the outcome as `orchestrator_invalid_submission`;
- add label `dora:orchestrator-invalid-submission`;
- write a diagnostic comment or run report when the backend supports it;
- leave business `Needs Input` untouched.

`Needs Input` remains reserved for real human judgment or external business blockers.

### 5. Ready Selection Skips Invalid Submissions

Memory, local, and live Plane backends must skip issues labeled `dora:orchestrator-invalid-submission` in `next_ready_issue`.

This prevents a bad submitted packet from repeatedly entering the runner loop. It also makes the operational signal explicit: repair the packaging/submission chain or submit a corrected later batch.

## Data And Label Semantics

Existing source evidence semantics remain separate:

- `dora:source-evidence-missing` means the executor ran but did not prove it read required sources.
- `dora:orchestrator-invalid-submission` means the issue should never have reached ready execution because the submitted packet is incomplete or unreconstructable.

The old `dora:source-context-missing` classification should be retired for new runner behavior. Operator CLIs may keep recognizing it for historical issues, but new invalid source-context failures should use `orchestrator_invalid_submission`.

## Implementation Shape

Add a reusable source preflight function that both audit and submit can call. It should use the existing source-context serialization path rather than inventing a separate validator:

1. Build per-task issue metadata with `execution_packet_version`, `execution_packet_hash`, `source_docs`, `source_tables`, `source_queries`, and task progress metadata.
2. Run `create_source_bundle(..., write_output=False)` or an equivalent dry-run mode that validates the same path/query behavior without leaving generated bundle files behind.
3. Return structured findings for audit and a hard exception for submit.

If a no-write dry-run would duplicate too much logic, the first implementation may write to a temporary directory under the OS temp area. It must not generate `.dora/source-bundles/` as part of audit/submit.

## Error Handling

Audit failures should use source-specific finding codes such as:

- `source_context`
- `source_not_found`
- `source_table_not_found`
- `source_query_empty`
- `source_bundle_preflight`

Submit failures should include the task id and the failing source context message.

Runner invalid-submission diagnostics should include:

- project slug;
- external id;
- failure reason;
- whether the issue was claimed;
- whether executor startup was skipped.

## Test Plan

Focused tests should cover:

- audit fails when a task source doc/page/summary is missing;
- audit fails when source bundle preflight cannot render a required query;
- submit performs zero Plane writes when source preflight fails after approval;
- submit still writes execution packet metadata for valid batches;
- run_ready returns `orchestrator_invalid_submission` instead of `source_context_missing`;
- run_ready does not set `Needs Input` for invalid submissions;
- `next_ready_issue` skips `dora:orchestrator-invalid-submission` for memory/local/live clients;
- status/query CLIs classify the new label while preserving historical classification for old labels.

## Compatibility

Historical issues labeled `dora:source-context-missing` may remain visible as historical source-context failures. Existing completed issues are not rewritten.

New submissions must pass source preflight before Plane writes. New runner failures caused by missing source context are treated as orchestrator invalid submissions and should be fixed by correcting the batch packaging or submitting a later corrected batch.
