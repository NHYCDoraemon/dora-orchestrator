# Orchestrator Source Context Submission Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent incomplete source context from being submitted to Plane, and classify any runner-discovered source context defect as an orchestrator invalid submission instead of task `Needs Input`.

**Architecture:** Add a reusable source preflight layer that builds the same Execution Packet v1 source metadata used by submit and validates it with the existing source bundle/query code. Wire that layer into audit and submit before any Plane writes. Keep run_ready source checks as a defensive fallback that labels and skips invalid submissions without changing business task state.

**Tech Stack:** Python 3.10+, `unittest`, existing `orchestrator` package modules, in-memory/local/live Plane client abstractions.

---

## File Structure

- Create `orchestrator/source_preflight.py`: shared per-task Execution Packet source metadata builder and preflight validator.
- Modify `orchestrator/source_bundle.py`: add a no-write mode so audit/submit can validate bundle generation without creating `.dora/source-bundles/`.
- Modify `orchestrator/batch_audit.py`: call source preflight and convert failures to audit findings.
- Modify `orchestrator/batch_submit.py`: run source preflight after approval/hash validation but before any `plane_client` write.
- Modify `orchestrator/run_ready_task.py`: replace source-context-missing task handling with `orchestrator_invalid_submission`.
- Modify `orchestrator/in_memory_plane.py`: skip `dora:orchestrator-invalid-submission` in ready selection and preserve that state through blocked refresh.
- Modify `orchestrator/plane_live.py`: skip the invalid-submission label in live ready selection.
- Modify `orchestrator/source_visibility.py`: classify the new label while preserving historical `dora:source-context-missing`.
- Add/modify focused tests in `tests/test_source_bundle.py`, `tests/test_batch_audit.py`, `tests/test_batch_submit.py`, `tests/test_run_ready_batch_task.py`, `tests/test_live_plane.py`, `tests/test_status.py`, and `tests/test_query_issues.py`.

---

### Task 1: Source Bundle No-Write Mode

**Files:**
- Modify: `orchestrator/source_bundle.py`
- Test: `tests/test_source_bundle.py`

- [ ] **Step 1: Write failing test**

Add a test proving `create_source_bundle(..., write_output=False)` validates required docs/tables/queries but writes no files:

```python
def test_create_source_bundle_no_write_validates_without_artifacts(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        (repo / "docs").mkdir()
        (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
        (repo / "ledger.tsv").write_text("row_id\tvalue\nF97-036\tready\n", encoding="utf-8")
        issue = {
            "external_id": "DORA-PLN-20260501B-T01",
            "execution_packet_version": 1,
            "row_id": "F97-036",
            "source_docs": [{"kind": "source_docs", "path": "docs/design.md", "required": True}],
            "source_tables": [{"id": "ledger", "path": "ledger.tsv", "format": "tsv", "required": True}],
            "source_queries": [{
                "id": "current_task",
                "table": "ledger",
                "required": True,
                "filters": [{"column": "row_id", "op": "equals", "value_from": "task.row_id"}],
                "columns": ["row_id", "value"],
                "max_rows": 10,
            }],
        }

        result = create_source_bundle(issue=issue, worktree_root=repo, write_output=False)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.slice_results[0].row_count, 1)
        self.assertFalse((repo / ".dora" / "source-bundles").exists())
```

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_source_bundle.SourceBundleTest.test_create_source_bundle_no_write_validates_without_artifacts
```

Expected: fail with `TypeError: create_source_bundle() got an unexpected keyword argument 'write_output'`.

- [ ] **Step 3: Implement minimal no-write mode**

Add `write_output: bool = True` to `create_source_bundle`. When false, pass `write_output=False` to `render_query_slice`, do not write `manifest.json` or `source-bundle.md`, and return required read paths using the computed bundle path/doc/table paths while omitting generated slice paths that do not exist.

- [ ] **Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.test_source_bundle.SourceBundleTest.test_create_source_bundle_no_write_validates_without_artifacts
```

Expected: pass.

---

### Task 2: Shared Source Preflight

**Files:**
- Create: `orchestrator/source_preflight.py`
- Test: `tests/test_source_preflight.py`

- [ ] **Step 1: Write failing tests**

Create tests for valid and invalid preflight:

```python
def test_preflight_builds_task_issue_metadata_without_plane_writes(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        batch_dir = create_batch(repo, with_source_table=True, with_task_row_id=True)
        batch = load_task_issue_batch(batch_dir, repo_root=repo)
        result = preflight_batch_source_context(batch, execution_packet_hash="sha256:test")

        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.task_results[0].issue["execution_packet_version"], 1)
        self.assertEqual(result.task_results[0].issue["execution_packet_hash"], "sha256:test")
        self.assertIn("source_docs", result.task_results[0].issue)
        self.assertIn("source_tables", result.task_results[0].issue)
        self.assertIn("source_queries", result.task_results[0].issue)

def test_preflight_reports_missing_required_doc(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        batch_dir = create_batch(repo)
        (repo / "docs" / "design.md").unlink()
        batch = load_task_issue_batch(batch_dir, repo_root=repo)
        result = preflight_batch_source_context(batch, execution_packet_hash="sha256:test")

        self.assertFalse(result.ok)
        self.assertEqual(result.findings[0].task_id, "DORA-CTX-20260501A-T01")
        self.assertIn("required source doc not found", result.findings[0].message)
```

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_source_preflight
```

Expected: import failure for missing `orchestrator.source_preflight`.

- [ ] **Step 3: Implement minimal module**

Define frozen dataclasses `SourcePreflightFinding`, `SourcePreflightTaskResult`, and `SourcePreflightResult`. Implement `build_task_issue_source_metadata(batch, task, execution_packet_hash)` and `preflight_batch_source_context(batch, execution_packet_hash)` using `source_docs_for_task`, `source_tables_from_batch`, `source_queries_from_batch`, progress metadata, and `create_source_bundle(..., write_output=False)`.

- [ ] **Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.test_source_preflight
```

Expected: pass.

---

### Task 3: Audit And Submit Preflight Gate

**Files:**
- Modify: `orchestrator/batch_audit.py`
- Modify: `orchestrator/batch_submit.py`
- Test: `tests/test_batch_audit.py`
- Test: `tests/test_batch_submit.py`

- [ ] **Step 1: Write failing audit and submit tests**

Add an audit test that a preflight-only failure becomes a `source_bundle_preflight` finding, and a submit test using a spy client that raises if any Plane write happens when a previously approved batch loses a source file before submit.

```python
class _WriteFailingPlaneClient(InMemoryPlaneClient):
    def upsert_project(self, slug, title):
        raise AssertionError("submit wrote to Plane before source preflight")

def test_submit_rejects_missing_source_after_approval_before_plane_writes(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        batch_dir = create_approved_batch(repo)
        (repo / "docs" / "design.md").unlink()

        with self.assertRaisesRegex(ValueError, "source preflight failed"):
            submit_task_issue_batch(
                batch_dir,
                repo_root=repo,
                project_slug="dora",
                project_title="Dora",
                plane_client=_WriteFailingPlaneClient(),
            )
```

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_batch_audit tests.test_batch_submit
```

Expected: new tests fail because audit/submit do not use shared preflight yet.

- [ ] **Step 3: Implement audit wiring**

In `audit_task_issue_batch`, call `preflight_batch_source_context(batch, execution_packet_hash="audit")` after existing source table/query validation. Append `AuditFinding(code="source_bundle_preflight", message=f"{task_id}: {message}", path=task_path)` for each failure not already covered by direct source path checks.

- [ ] **Step 4: Implement submit wiring**

After `_validate_approval` and `compute_batch_hash`, call `preflight_batch_source_context(batch, execution_packet_hash=batch_hash)`. If not ok, raise `ValueError("source preflight failed: ...")`. Reuse each `task_result.issue` to populate `execution_packet_version`, `execution_packet_hash`, `source_docs`, `source_tables`, and `source_queries` during `upsert_issue`.

- [ ] **Step 5: Verify green**

Run:

```bash
python3 -m unittest tests.test_batch_audit tests.test_batch_submit tests.test_source_preflight
```

Expected: pass.

---

### Task 4: Runner Invalid Submission Classification

**Files:**
- Modify: `orchestrator/run_ready_task.py`
- Test: `tests/test_run_ready_batch_task.py`

- [ ] **Step 1: Update failing expectations**

Change the existing source-context tests to expect:

```python
self.assertEqual(task_result["outcome"], "orchestrator_invalid_submission")
self.assertNotEqual(task_result.get("state"), "Needs Input")
self.assertNotEqual(issue["state"], "Needs Input")
self.assertIn("dora:orchestrator-invalid-submission", issue.get("labels") or [])
self.assertNotIn("dora:source-context-missing", issue.get("labels") or [])
```

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_run_ready_batch_task.RunReadyBatchTaskTest.test_missing_required_source_doc_blocks_before_claim tests.test_run_ready_batch_task.RunReadyBatchTaskTest.test_legacy_issue_without_execution_packet_blocks_before_claim tests.test_run_ready_batch_task.RunReadyBatchTaskTest.test_packet_v1_without_source_metadata_blocks_before_claim
```

Expected: fail because current code returns `source_context_missing` and sets `Needs Input`.

- [ ] **Step 3: Implement runner fallback**

Rename the helper behavior to `_mark_orchestrator_invalid_submission`, add label `dora:orchestrator-invalid-submission`, emit marker `dora-loop:orchestrator-invalid-submission`, do not call `release_issue(..., "Needs Input")`, and return state as the existing issue state.

- [ ] **Step 4: Verify green**

Run the same focused tests. Expected: pass.

---

### Task 5: Ready Queue Skip For Invalid Submissions

**Files:**
- Modify: `orchestrator/in_memory_plane.py`
- Modify: `orchestrator/plane_live.py`
- Test: `tests/test_run_ready_batch_task.py`
- Test: `tests/test_live_plane.py`

- [ ] **Step 1: Write failing tests**

Add memory and live tests proving `next_ready_issue` skips issues labeled `dora:orchestrator-invalid-submission`.

```python
client.upsert_issue("dora", "DORA-CTX-20260501A-T01", {
    "name": "Invalid packet",
    "issue_type": "task",
    "priority": "P1",
    "depends_on": [],
    "labels": ["dora:orchestrator-invalid-submission"],
})
self.assertIsNone(client.next_ready_issue("dora"))
```

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_run_ready_batch_task tests.test_live_plane
```

Expected: new ready-skip tests fail because invalid labeled issues are still candidates.

- [ ] **Step 3: Implement skip**

Add constant `ORCHESTRATOR_INVALID_SUBMISSION_LABEL = "dora:orchestrator-invalid-submission"`. In memory/local selection, skip if issue labels contain it. In live selection, resolve label IDs to names and skip when the issue labels include the invalid-submission label.

- [ ] **Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.test_run_ready_batch_task tests.test_live_plane
```

Expected: pass.

---

### Task 6: Operator Visibility

**Files:**
- Modify: `orchestrator/source_visibility.py`
- Test: `tests/test_status.py`
- Test: `tests/test_query_issues.py`

- [ ] **Step 1: Write failing tests**

Add expectations that issues labeled `dora:orchestrator-invalid-submission` classify as `orchestrator_invalid_submission`, while existing `dora:source-context-missing` still classifies as `source_context_missing`.

- [ ] **Step 2: Verify red**

Run:

```bash
python3 -m unittest tests.test_status tests.test_query_issues
```

Expected: fail for the new classification.

- [ ] **Step 3: Implement classification**

In `source_visibility.py`, add `ORCHESTRATOR_INVALID_SUBMISSION_LABEL` and return `"orchestrator_invalid_submission"` before checking the historical source-context label.

- [ ] **Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.test_status tests.test_query_issues
```

Expected: pass.

---

### Task 7: Final Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run focused source-context suite**

```bash
python3 -m unittest \
  tests.test_source_bundle \
  tests.test_source_preflight \
  tests.test_batch_audit \
  tests.test_batch_submit \
  tests.test_run_ready_batch_task \
  tests.test_live_plane \
  tests.test_status \
  tests.test_query_issues
```

Expected: all tests pass.

- [ ] **Step 2: Run full suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass; Dagster-dependent tests may skip if `dagster` is unavailable.
