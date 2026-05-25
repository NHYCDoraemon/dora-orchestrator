"""Tests for `orchestrator query-issues` CLI and the query_issues backend method."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestrator import query_issues
from orchestrator.in_memory_plane import InMemoryPlaneClient


def _packet_metadata() -> dict[str, object]:
    return {
        "execution_packet_version": 1,
        "source_docs": [],
        "source_tables": [],
        "source_queries": [],
    }


def _seed(client: InMemoryPlaneClient) -> None:
    client.upsert_project("demo", "Demo")
    # Two batches of issues, each with multiple modules.
    client.upsert_issue(
        "demo",
        "DEMO-PROG-20260505A-T01",
        {"name": "T01 design", "module": "architecture", "depends_on": []},
    )
    client.upsert_issue(
        "demo",
        "DEMO-PROG-20260505A-T02",
        {"name": "T02 implement", "module": "implementation", "depends_on": ["DEMO-PROG-20260505A-T01"]},
    )
    client.upsert_issue(
        "demo",
        "DEMO-PROG-20260506B-T01",
        {"name": "T01 verify", "module": "verification", "depends_on": []},
    )
    client.upsert_issue(
        "demo",
        "DEMO-PROG-ROOT",
        {"name": "Root epic", "module": "planning", "issue_type": "root_epic", "depends_on": []},
    )
    # Move one issue into Done.
    client.release_issue("demo", "DEMO-PROG-20260505A-T01", "Done")


class QueryIssuesBackendTest(unittest.TestCase):
    def test_no_filters_returns_task_issues_excluding_root_epic(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo")

        ids = [i["external_id"] for i in result]
        self.assertEqual(
            ids,
            [
                "DEMO-PROG-20260505A-T01",
                "DEMO-PROG-20260505A-T02",
                "DEMO-PROG-20260506B-T01",
            ],
        )

    def test_include_root_epic_returns_root_epic(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", include_root_epic=True)

        ids = {i["external_id"] for i in result}
        self.assertIn("DEMO-PROG-ROOT", ids)

    def test_state_filter_keeps_only_matching_states(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", states=["Done"])

        self.assertEqual([i["external_id"] for i in result], ["DEMO-PROG-20260505A-T01"])

    def test_state_filter_accepts_multiple_states(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", states=["Done", "Todo"])

        ids = {i["external_id"] for i in result}
        # T01 Done, T02 unblocked → Todo, T01-of-batchB has no deps → Todo.
        self.assertEqual(
            ids,
            {
                "DEMO-PROG-20260505A-T01",
                "DEMO-PROG-20260505A-T02",
                "DEMO-PROG-20260506B-T01",
            },
        )

    def test_module_filter_keeps_only_matching_modules(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", modules=["implementation"])

        self.assertEqual([i["external_id"] for i in result], ["DEMO-PROG-20260505A-T02"])

    def test_batch_filter_uses_third_dash_segment(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", batch="20260505A")

        ids = [i["external_id"] for i in result]
        self.assertEqual(
            ids,
            ["DEMO-PROG-20260505A-T01", "DEMO-PROG-20260505A-T02"],
        )

    def test_filters_combine_with_and(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues(
            "demo",
            states=["Todo"],
            batch="20260505A",
            modules=["implementation"],
        )

        self.assertEqual([i["external_id"] for i in result], ["DEMO-PROG-20260505A-T02"])

    def test_unknown_state_returns_empty(self):
        client = InMemoryPlaneClient()
        _seed(client)

        result = client.query_issues("demo", states=["NotAState"])

        self.assertEqual(result, [])


class QueryIssuesCliTest(unittest.TestCase):
    def _make_repo(self, tmp: str, *, slug: str = "demo", title: str = "Demo") -> Path:
        repo = Path(tmp)
        (repo / ".dora").mkdir()
        (repo / ".dora" / "project.json").write_text(
            json.dumps({"project_slug": slug, "title": title}),
            encoding="utf-8",
        )
        return repo

    def test_cli_emits_json_with_filters_and_match_count(self):
        client = InMemoryPlaneClient()
        _seed(client)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch("orchestrator.query_issues.create_plane_client", return_value=client):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = query_issues.main(
                        ["--repo", str(repo), "--batch", "20260505A", "--state", "Todo"]
                    )

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["filters"]["batch"], "20260505A")
        self.assertEqual(result["filters"]["states"], ["Todo"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(
            result["issues"][0]["external_id"],
            "DEMO-PROG-20260505A-T02",
        )
        self.assertEqual(result["issues"][0]["module"], "implementation")
        self.assertEqual(result["issues"][0]["depends_on"], ["DEMO-PROG-20260505A-T01"])

    def test_cli_no_filters_returns_all_task_issues(self):
        client = InMemoryPlaneClient()
        _seed(client)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch("orchestrator.query_issues.create_plane_client", return_value=client):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = query_issues.main(["--repo", str(repo)])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["match_count"], 3)
        ids = {i["external_id"] for i in result["issues"]}
        self.assertNotIn("DEMO-PROG-ROOT", ids)

    def test_cli_emits_source_context_in_json_and_stderr_rows(self):
        client = InMemoryPlaneClient()
        client.upsert_project("demo", "Demo")
        client.upsert_issue(
            "demo",
            "DEMO-SRC-T01",
            {"name": "legacy issue", "depends_on": []},
        )
        client.upsert_issue(
            "demo",
            "DEMO-SRC-T02",
            {"name": "packet issue", "depends_on": [], **_packet_metadata()},
        )
        client.upsert_issue(
            "demo",
            "DEMO-SRC-T03",
            {
                "name": "source evidence issue",
                "depends_on": [],
                "labels": ["dora:source-evidence-missing"],
                **_packet_metadata(),
            },
        )
        client.upsert_issue(
            "demo",
            "DEMO-SRC-T04",
            {
                "name": "source context issue",
                "depends_on": [],
                "labels": [{"name": "dora:source-context-missing"}],
                **_packet_metadata(),
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch("orchestrator.query_issues.create_plane_client", return_value=client):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = query_issues.main(["--repo", str(repo)])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        source_context = {i["external_id"]: i["source_context"] for i in result["issues"]}
        self.assertEqual(source_context["DEMO-SRC-T01"], "legacy_or_missing_packet")
        self.assertEqual(source_context["DEMO-SRC-T02"], "packet_v1")
        self.assertEqual(source_context["DEMO-SRC-T03"], "source_evidence_missing")
        self.assertEqual(source_context["DEMO-SRC-T04"], "source_context_missing")
        stderr_text = stderr.getvalue()
        self.assertIn("legacy_or_missing_packet", stderr_text)
        self.assertIn("packet_v1", stderr_text)
        self.assertIn("source_evidence_missing", stderr_text)
        self.assertIn("source_context_missing", stderr_text)


class QueryIssuesCliSubcommandTest(unittest.TestCase):
    def test_query_issues_is_registered_in_cli_subcommands(self):
        from orchestrator.cli import SUBCOMMANDS

        self.assertIn("query-issues", SUBCOMMANDS)
        self.assertIs(SUBCOMMANDS["query-issues"], query_issues.main)


if __name__ == "__main__":
    unittest.main()
