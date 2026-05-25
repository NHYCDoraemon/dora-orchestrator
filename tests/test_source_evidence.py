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

    def test_claude_glob_event_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Glob",
                                    "input": {"path": str(doc)},
                                }
                            ]
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

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

    def test_codex_command_repo_relative_src_path_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            app = tmp_path / "src" / "app.py"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat src/app.py",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[app],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_shell_wrapper_inner_command_path_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "/bin/zsh -lc 'cat docs/design.md'",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_common_filename_token_satisfies_required_readme_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readme = tmp_path / "README.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "sed -n '1,20p' README.md"},
                                }
                            ]
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[readme],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_sed_in_place_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                    "input": {"command": "sed -i 's/a/b/' docs/design.md"},
                                }
                            ]
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_sed_print_command_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                    "input": {"command": "sed -n '1,20p' docs/design.md"},
                                }
                            ]
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_basename_token_does_not_satisfy_nested_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readme = tmp_path / "docs" / "README.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat README.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[readme],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (readme.resolve(),))

    def test_extensionless_repo_relative_token_satisfies_required_makefile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            makefile = tmp_path / "Makefile"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat Makefile",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[makefile],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_rm_command_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rm docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_git_add_command_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "git add docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_cat_redirect_write_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat > docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_cat_compact_redirect_write_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat >docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_cat_compact_append_redirect_write_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat >>docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_cat_with_input_and_compact_redirect_counts_only_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "docs" / "source.md"
            output = tmp_path / "docs" / "design.md"

            source_result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat docs/source.md >docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[source],
            )
            output_result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat docs/source.md >docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[output],
            )

            self.assertIs(source_result.ok, True)
            self.assertEqual(source_result.missing_paths, ())
            self.assertIs(output_result.ok, False)
            self.assertEqual(output_result.missing_paths, (output.resolve(),))

    def test_rg_pattern_only_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            api = tmp_path / "src" / "api" / "forms.ts"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rg src/api/forms.ts",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[api],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (api.resolve(),))

    def test_rg_pattern_with_file_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            api = tmp_path / "src" / "api" / "forms.ts"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rg TODO src/api/forms.ts",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[api],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_grep_pattern_only_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            api = tmp_path / "src" / "api" / "forms.ts"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "grep src/api/forms.ts",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[api],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (api.resolve(),))

    def test_grep_pattern_with_file_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            api = tmp_path / "src" / "api" / "forms.ts"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "grep TODO src/api/forms.ts",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[api],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_later_rm_segment_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat README.md && rm docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_later_cat_segment_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rm README.md && cat docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_pipeline_counts_only_read_operands_in_each_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat README.md | grep TODO docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, True)
            self.assertEqual(result.missing_paths, ())

    def test_compact_semicolon_later_rm_segment_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat README.md;rm docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_compact_and_later_rm_segment_does_not_satisfy_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "cat README.md&&rm docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
            )

            self.assertIs(result.ok, False)
            self.assertEqual(result.missing_paths, (doc.resolve(),))

    def test_compact_and_later_cat_segment_satisfies_required_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            doc = tmp_path / "docs" / "design.md"

            result = evaluate_source_evidence(
                events=[
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rm README.md&&cat docs/design.md",
                        },
                    }
                ],
                worktree_root=tmp_path,
                required_paths=[doc],
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
