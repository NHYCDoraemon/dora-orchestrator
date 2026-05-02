import unittest

from orchestrator.git_delivery import is_forbidden_orchestration_commit


class GitDeliveryTest(unittest.TestCase):
    def test_rejects_legacy_lock_commits(self):
        bad = [
            "chore(progress): claim lock for S1.5-P3-05",
            "chore(progress): release lock for S1.5-P3-05 (done)",
            "chore(progress): reset lock from executing to idle",
            "chore(progress): heartbeat DOR-217",
        ]
        for msg in bad:
            with self.subTest(msg=msg):
                self.assertTrue(is_forbidden_orchestration_commit(msg))

    def test_allows_deliverable_commits(self):
        good = [
            "docs(adr): DOR-5 update orchestration ADR",
            "feat(cognition): DOR-217 add context assembly budget policy",
        ]
        for msg in good:
            with self.subTest(msg=msg):
                self.assertFalse(is_forbidden_orchestration_commit(msg))
