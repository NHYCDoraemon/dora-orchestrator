import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.source_evidence import evaluate_source_evidence, evaluate_source_evidence_from_event_path


class SourceEvidenceTest(unittest.TestCase):
    def test_claude_read_event_satisfies_required_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle = tmp_path / ".dora" / "source-bundles" / "20260501A" / "T01" / "source-bundle.md"
            doc = tmp_path / "docs" / "design.md"
            slice_file = tmp_path / ".dora" / "source-bundles" / "20260501A" / "T01" / "slices" / "current_task_row.tsv"
            for path in (bundle, doc, slice_file):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")
            event_path = tmp_path / "events.ndjson"
            events = [
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(bundle)}}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(doc)}}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(slice_file)}}]}},
            ]
            event_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            result = evaluate_source_evidence_from_event_path(event_path, worktree_root=tmp_path, required_paths=[bundle, doc, slice_file])

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_missing_required_read_evidence_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            event_path = tmp_path / "events.ndjson"
            event_path.write_text("", encoding="utf-8")
            bundle = tmp_path / ".dora" / "source-bundles" / "source-bundle.md"

            result = evaluate_source_evidence_from_event_path(event_path, worktree_root=tmp_path, required_paths=[bundle])

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (bundle.resolve(),))

    def test_bash_and_codex_command_paths_satisfy_required_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle = tmp_path / ".dora" / "source-bundles" / "B06" / "T01" / "source-bundle.md"
            doc = tmp_path / "docs" / "design.md"
            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": f"sed -n '1,80p' {bundle}"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": ["cat", "docs/design.md"],
                        },
                    },
                ],
                worktree_root=tmp_path,
                required_paths=[bundle, doc],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_event_path_ignores_malformed_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle = tmp_path / ".dora" / "source-bundles" / "B06" / "T01" / "source-bundle.md"
            event_path = tmp_path / "events.ndjson"
            event_path.write_text(
                "{not json}\n"
                + json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": str(bundle)},
                            }
                        ]
                    },
                })
                + "\n",
                encoding="utf-8",
            )

            result = evaluate_source_evidence_from_event_path(event_path, worktree_root=tmp_path, required_paths=[bundle])

            self.assertIs(result.ok, True)
