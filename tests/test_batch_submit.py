import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.batch_audit import audit_task_issue_batch
from orchestrator.batch_hash import compute_batch_hash
from orchestrator.batch_submit import submit_task_issue_batch
from orchestrator.in_memory_plane import InMemoryPlaneClient
from tests.test_batch_loader import create_batch


class BatchSubmitTest(unittest.TestCase):
    def test_submits_approved_batch_as_pages_root_epic_and_task_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_approved_batch(repo)
            client = InMemoryPlaneClient()

            result = submit_task_issue_batch(
                batch_dir,
                repo_root=repo,
                project_slug="dora",
                project_title="Dora",
                plane_client=client,
            )

            self.assertEqual(result["projects"], 1)
            self.assertEqual(result["pages"], 2)
            self.assertEqual(result["root_epic_issues"], 1)
            self.assertEqual(result["task_issues"], 1)
            self.assertIn(("dora", "program-dora-context-assembly"), client.pages)
            self.assertIn(("dora", "batch-20260501A"), client.pages)
            root = client.issues[("dora", "DORA-CTX-20260501A-ROOT")]
            task = client.issues[("dora", "DORA-CTX-20260501A-T01")]
            self.assertEqual(root["issue_type"], "root_epic")
            self.assertEqual(task["parent_external_id"], "DORA-CTX-20260501A-ROOT")
            self.assertEqual(client.pages[("dora", "program-dora-context-assembly")]["title"], "计划：dora-context-assembly")
            self.assertEqual(client.pages[("dora", "batch-20260501A")]["title"], "批次：Dora 上下文装配第四阶段")
            self.assertEqual(root["name"], "[批次] Dora 上下文装配第四阶段")
            self.assertIn("# CLI 上下文检查能力", task["body"])
            self.assertIn("## 任务概要", task["body"])
            self.assertNotIn("# Task Summary", task["body"])

    def test_submits_required_skills_to_task_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)
            task_path = batch_dir / "tasks" / "DORA-CTX-20260501A-T01.md"
            task_text = task_path.read_text(encoding="utf-8")
            task_path.write_text(
                task_text.replace(
                    "priority: P1\n",
                    "priority: P1\nrequired_skills:\n  - maritime-java-backend-development\n",
                ),
                encoding="utf-8",
            )
            batch_dir = approve_batch(repo, batch_dir)
            client = InMemoryPlaneClient()

            submit_task_issue_batch(
                batch_dir,
                repo_root=repo,
                project_slug="dora",
                project_title="Dora",
                plane_client=client,
            )

            task = client.issues[("dora", "DORA-CTX-20260501A-T01")]
            self.assertEqual(task["required_skills"], ["maritime-java-backend-development"])

    def test_rejects_batch_without_approval_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_batch(repo)

            with self.assertRaisesRegex(ValueError, "approval.json"):
                submit_task_issue_batch(
                    batch_dir,
                    repo_root=repo,
                    project_slug="dora",
                    project_title="Dora",
                    plane_client=InMemoryPlaneClient(),
                )

    def test_rejects_batch_changed_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            batch_dir = create_approved_batch(repo)
            (repo / "docs" / "design.md").write_text("# Design\n\nChanged after approval.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "approval hash"):
                submit_task_issue_batch(
                    batch_dir,
                    repo_root=repo,
                    project_slug="dora",
                    project_title="Dora",
                    plane_client=InMemoryPlaneClient(),
                )


def create_approved_batch(repo: Path) -> Path:
    batch_dir = create_batch(repo)
    return approve_batch(repo, batch_dir)


def approve_batch(repo: Path, batch_dir: Path) -> Path:
    (repo / ".dora").mkdir()
    (repo / ".dora" / "project.json").write_text(
        json.dumps({"project_slug": "dora", "title": "Dora"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    audit_task_issue_batch(batch_dir, repo_root=repo, write_generated=True)
    batch_hash = compute_batch_hash(batch_dir, repo_root=repo)
    (batch_dir / "approval.json").write_text(
        json.dumps(
            {
                "approval_version": 1,
                "batch_id": "20260501A",
                "program_id": "dora-context-assembly",
                "approved_by": "raymond",
                "approved_at": "2026-05-01T21:30:00+08:00",
                "approved_batch_hash": batch_hash,
                "approved_task_ids": ["DORA-CTX-20260501A-T01"],
                "approved_plane_creates": {
                    "cycles": ["S1.5 Phase 4"],
                    "root_epic_issue": True,
                    "task_issues": 1,
                    "pages": ["Program Page", "Batch Page"],
                },
                "policy": {
                    "allow_create_cycles": True,
                    "allow_create_root_epic": True,
                    "allow_create_task_issues": True,
                    "allow_update_existing_submitted_issues": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return batch_dir
