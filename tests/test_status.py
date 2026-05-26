"""Tests for `orchestrator status` CLI."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestrator import status


def _packet_metadata() -> dict[str, object]:
    return {
        "execution_packet_version": 1,
        "source_docs": [],
        "source_tables": [],
        "source_queries": [],
    }


class StatusCliTest(unittest.TestCase):
    def _make_repo(self, tmp: str, *, slug: str = "demo", title: str = "Demo") -> Path:
        repo = Path(tmp)
        (repo / ".dora").mkdir()
        (repo / ".dora" / "project.json").write_text(
            json.dumps({"project_slug": slug, "title": title}),
            encoding="utf-8",
        )
        return repo

    def test_state_summary_includes_every_state_not_only_blocked(self):
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        for ext in ("DEMO-T01", "DEMO-T02", "DEMO-T03", "DEMO-T04"):
            client.upsert_issue("demo", ext, {"name": ext, "depends_on": []})
        client.claim_issue("demo", "DEMO-T01", "run-1")
        client.release_issue("demo", "DEMO-T02", "Done")

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp, slug="demo", title="Demo")
            with patch("orchestrator.status.create_plane_client", return_value=client):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = status.main(["--repo", str(repo)])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        # state_summary must reflect ALL states, not just Blocked.
        self.assertEqual(result["state_summary"].get("In Progress"), 1)
        self.assertEqual(result["state_summary"].get("Done"), 1)
        # total_issues = sum across all states (4 issues)
        self.assertEqual(result["total_issues"], 4)

    def test_repo_argument_used_for_project_slug_resolution(self):
        """status --repo /path/to/X must use X's project.json even when
        cwd is /path/to/Y with its own project.json."""
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("target-slug", "Target")

        with tempfile.TemporaryDirectory() as tmp_target, tempfile.TemporaryDirectory() as tmp_cwd:
            target = self._make_repo(tmp_target, slug="target-slug", title="Target")
            cwd = self._make_repo(tmp_cwd, slug="cwd-slug", title="Cwd")

            import os
            old = os.getcwd()
            try:
                os.chdir(str(cwd))
                with patch("orchestrator.status.create_plane_client", return_value=client):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                        rc = status.main(["--repo", str(target)])
            finally:
                os.chdir(old)

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["project_slug"], "target-slug")

    def test_blocked_issues_carry_real_depends_on(self):
        """blocked_issues output must surface each issue's actual
        depends_on, not always []. Pre-fix, status' live branch + the
        live backend's _adapt_issue both dropped this field."""
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        client.upsert_issue("demo", "DEMO-T01", {"name": "T01", "depends_on": []})
        client.release_issue("demo", "DEMO-T01", "Done")
        # T02 depends on a not-yet-Done T03 → Blocked
        client.upsert_issue(
            "demo",
            "DEMO-T02",
            {"name": "T02", "depends_on": ["DEMO-T03"]},
        )
        client.upsert_issue("demo", "DEMO-T03", {"name": "T03", "depends_on": []})

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp, slug="demo", title="Demo")
            with patch("orchestrator.status.create_plane_client", return_value=client):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = status.main(["--repo", str(repo), "--show-blocked"])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        blocked = {i["external_id"]: i for i in result["blocked_issues"]}
        self.assertIn("DEMO-T02", blocked)
        self.assertEqual(blocked["DEMO-T02"]["depends_on"], ["DEMO-T03"])

    def test_show_blocked_emits_source_context_in_json_and_stderr(self):
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        client.upsert_issue(
            "demo",
            "DEMO-T01",
            {"name": "legacy blocked", "depends_on": ["DEMO-MISSING"]},
        )
        client.upsert_issue(
            "demo",
            "DEMO-T02",
            {"name": "packet blocked", "depends_on": ["DEMO-MISSING"], **_packet_metadata()},
        )
        client.upsert_issue(
            "demo",
            "DEMO-T03",
            {
                "name": "evidence blocked",
                "depends_on": ["DEMO-MISSING"],
                "labels": ["dora:source-evidence-missing"],
                **_packet_metadata(),
            },
        )
        client.upsert_issue(
            "demo",
            "DEMO-T04",
            {
                "name": "context blocked",
                "depends_on": ["DEMO-MISSING"],
                "labels": [{"name": "dora:source-context-missing"}],
                **_packet_metadata(),
            },
        )
        client.upsert_issue(
            "demo",
            "DEMO-T05",
            {
                "name": "invalid submission blocked",
                "depends_on": ["DEMO-MISSING"],
                "labels": ["dora:orchestrator-invalid-submission"],
                **_packet_metadata(),
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp, slug="demo", title="Demo")
            with patch("orchestrator.status.create_plane_client", return_value=client):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = status.main(["--repo", str(repo), "--show-blocked"])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        blocked = {i["external_id"]: i["source_context"] for i in result["blocked_issues"]}
        self.assertEqual(blocked["DEMO-T01"], "legacy_or_missing_packet")
        self.assertEqual(blocked["DEMO-T02"], "packet_v1")
        self.assertEqual(blocked["DEMO-T03"], "source_evidence_missing")
        self.assertEqual(blocked["DEMO-T04"], "source_context_missing")
        self.assertEqual(blocked["DEMO-T05"], "orchestrator_invalid_submission")
        stderr_text = stderr.getvalue()
        self.assertIn("source_context: legacy_or_missing_packet", stderr_text)
        self.assertIn("source_context: packet_v1", stderr_text)
        self.assertIn("source_context: source_evidence_missing", stderr_text)
        self.assertIn("source_context: source_context_missing", stderr_text)
        self.assertIn("source_context: orchestrator_invalid_submission", stderr_text)

    def test_blocked_stderr_includes_source_context_without_show_blocked(self):
        from orchestrator.in_memory_plane import InMemoryPlaneClient

        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        client.upsert_issue(
            "demo",
            "DEMO-T01",
            {"name": "packet blocked", "depends_on": ["DEMO-MISSING"], **_packet_metadata()},
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp, slug="demo", title="Demo")
            with patch("orchestrator.status.create_plane_client", return_value=client):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = status.main(["--repo", str(repo)])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["blocked_issues"], [])
        self.assertIn("source_context: packet_v1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
