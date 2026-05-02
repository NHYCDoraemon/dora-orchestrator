import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_loader import load_task_issue_batch


class BatchLoaderTest(unittest.TestCase):
    def test_loads_batch_and_task_issue_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)

            batch = load_task_issue_batch(batch_dir, repo_root=repo)

            self.assertEqual(batch.batch_id, "20260501A")
            self.assertEqual(batch.program_prefix, "CTX")
            self.assertEqual(batch.program_page.path, (batch_dir / "program-page.md").resolve())
            self.assertEqual(len(batch.tasks), 1)
            self.assertEqual(batch.tasks[0].task_id, "DORA-CTX-20260501A-T01")
            self.assertEqual(batch.tasks[0].sections["Scope"], "Allowed files: `internal/cognition/context/*`.")


def create_batch(repo: Path) -> Path:
    batch_dir = repo / "docs" / "dora" / "batches" / "20260501A"
    tasks_dir = batch_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    (repo / "docs" / "summaries").mkdir(parents=True)
    (repo / "docs" / "summaries" / "S1.5-P3-05.md").write_text("# Prior work\n", encoding="utf-8")
    (repo / "docs" / "design.md").write_text("# Design\n", encoding="utf-8")
    (batch_dir / "batch.md").write_text(
        """---
batch_id: 20260501A
program_id: dora-context-assembly
program_prefix: CTX
title: Dora Context Assembly Phase 4
status: draft
created_by: raymond
created_at: 2026-05-01T21:30:00+08:00
---

# Batch

Build the context assembly hardening batch.
""",
        encoding="utf-8",
    )
    (batch_dir / "program-page.md").write_text(
        """# Program

The program owns context assembly quality.
""",
        encoding="utf-8",
    )
    (tasks_dir / "DORA-CTX-20260501A-T01.md").write_text(
        """---
task_id: DORA-CTX-20260501A-T01
id_scheme: batch-native
legacy_refs:
  - S1.5-P4-01
title: CLI context inspect surface
batch_id: 20260501A
program_id: dora-context-assembly
program_prefix: CTX
sequence: 1
cycle: S1.5 Phase 4
module: implementation
priority: P1
depends_on: []
depends_on_legacy:
  - S1.5-P3-05
source_pages:
  - ../program-page.md
source_docs:
  - docs/design.md
source_summaries:
  - docs/summaries/S1.5-P3-05.md
source_commits: []
---

# Task Summary

Add a CLI inspection surface for context assembly.

# Development Context

Use the linked design document and summary as source context.

# Scope

Allowed files: `internal/cognition/context/*`.

# Non-goals

Do not change memory persistence.

# Implementation Detail

Read the context assembler package and add focused tests.

# Acceptance

`go test ./internal/cognition/...` passes.

# Verification

Run L1 and L2 checks listed in the task.

# Stop Conditions

Stop if the design doc contradicts ADR constraints.

# Executor Prompt Contract

Read this packet, linked sources, and git history before editing.
""",
        encoding="utf-8",
    )
    return batch_dir
