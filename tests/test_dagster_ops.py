import json
import unittest

from orchestrator.dagster_defs.ops import _format_progress_log


class DagsterOpsProgressLogTest(unittest.TestCase):
    def test_executor_output_logs_summary_only(self):
        self.assertEqual(
            _format_progress_log(
                "executor_output",
                {
                    "external_id": "PE-BREVIEW-20260519A-T11",
                    "summary": "command rc=0  |  2L  |  done",
                },
            ),
            "command rc=0  |  2L  |  done",
        )

    def test_non_executor_output_keeps_structured_event_json(self):
        line = _format_progress_log(
            "executor_started",
            {"external_id": "PE-BREVIEW-20260519A-T11", "agent": "codex"},
        )

        self.assertEqual(
            json.loads(line),
            {
                "event": "executor_started",
                "external_id": "PE-BREVIEW-20260519A-T11",
                "agent": "codex",
            },
        )

