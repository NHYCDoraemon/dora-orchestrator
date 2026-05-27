"""Tests for orchestrator.delivery._try_update_ref.

The dirty-WT branch existed but skipped the working-tree refresh, leaving
the main checkout N commits behind ``refs/heads/main`` whenever the user
had any uncommitted edits. These tests pin the autostash → reset → pop
behaviour added to recover from that.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.delivery import _try_update_ref


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo_with_branch() -> tuple[Path, str, str, tempfile._TemporaryDirectoryWithoutSuffix | tempfile.TemporaryDirectory]:
    """Build a fresh repo on ``main`` plus a ``feature`` branch one commit ahead.

    Returns ``(repo_root, old_main_sha, feature_sha, tmp)``. Caller must keep
    *tmp* alive until the test finishes.
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "repo"
    root.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for k, v in env.items():
        os.environ[k] = v
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("v1\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    old_main = _git(root, "rev-parse", "HEAD").stdout.strip()

    _git(root, "checkout", "-q", "-b", "feature")
    (root / "b.txt").write_text("from-feature\n")
    (root / "a.txt").write_text("v2\n")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-q", "-m", "feature work")
    feature_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    _git(root, "checkout", "-q", "main")
    return root, old_main, feature_sha, tmp


class TryUpdateRefTest(unittest.TestCase):
    def test_clean_main_fast_forwards_and_refreshes_worktree(self):
        root, old_main, feature_sha, tmp = _init_repo_with_branch()
        try:
            ok, strategy = _try_update_ref(
                root, "main", feature_sha, old_main, main_was_clean=True,
            )
            self.assertTrue(ok)
            self.assertEqual(strategy, "ff")
            # ref moved
            self.assertEqual(
                _git(root, "rev-parse", "main").stdout.strip(), feature_sha,
            )
            # WT picked up the new file and the modified file
            self.assertTrue((root / "b.txt").exists())
            self.assertEqual((root / "a.txt").read_text(), "v2\n")
            # nothing left dangling
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(root),
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(status.stdout, "")
        finally:
            tmp.cleanup()

    def test_dirty_main_unrelated_edit_is_preserved_via_autostash(self):
        """User edited an unrelated file. After ff, that edit must survive
        and the new commits must materialise."""
        root, old_main, feature_sha, tmp = _init_repo_with_branch()
        try:
            (root / ".gitignore").write_text("/var/\n")  # untracked-then-tracked dance
            _git(root, "add", ".gitignore")
            _git(root, "commit", "-q", "-m", "add gitignore")
            # bump old_main since we added a commit on main
            old_main = _git(root, "rev-parse", "main").stdout.strip()
            # rebase feature onto new main so ff is still possible
            _git(root, "checkout", "-q", "feature")
            _git(root, "rebase", "-q", "main")
            feature_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
            _git(root, "checkout", "-q", "main")
            # now make an unstaged edit
            (root / ".gitignore").write_text("/var/\n/tmp/\n")

            ok, strategy = _try_update_ref(
                root, "main", feature_sha, old_main, main_was_clean=False,
            )

            self.assertTrue(ok)
            self.assertEqual(strategy, "ff_dirty_applied")
            self.assertEqual(
                _git(root, "rev-parse", "main").stdout.strip(), feature_sha,
            )
            # new commits materialised
            self.assertTrue((root / "b.txt").exists())
            self.assertEqual((root / "a.txt").read_text(), "v2\n")
            # user edit preserved
            self.assertEqual((root / ".gitignore").read_text(), "/var/\n/tmp/\n")
            # stash list is empty (pop succeeded)
            stash = subprocess.run(
                ["git", "stash", "list"], cwd=str(root),
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(stash.stdout, "")
        finally:
            tmp.cleanup()

    def test_dirty_main_conflicting_edit_keeps_stash_for_human(self):
        """User's unstaged edit collides with incoming change. Pop fails,
        the ref still moves forward, and the stash is preserved so the
        user can resolve manually."""
        root, old_main, feature_sha, tmp = _init_repo_with_branch()
        try:
            # Edit the same file the feature branch also modified.
            (root / "a.txt").write_text("user-local-edit\n")

            ok, strategy = _try_update_ref(
                root, "main", feature_sha, old_main, main_was_clean=False,
            )

            self.assertTrue(ok)
            self.assertEqual(strategy, "ff_dirty_stashed")
            self.assertEqual(
                _git(root, "rev-parse", "main").stdout.strip(), feature_sha,
            )
            # b.txt from feature must exist; that is the whole point of the fix
            self.assertTrue((root / "b.txt").exists())
            # stash entry is preserved for the user
            stash = subprocess.run(
                ["git", "stash", "list"], cwd=str(root),
                capture_output=True, text=True, check=True,
            )
            self.assertIn("orchestrator-autostash", stash.stdout)
        finally:
            tmp.cleanup()

    def test_update_ref_rejected_returns_false(self):
        """CAS rejection must return (False, 'update_ref_rejected') and
        not touch the working tree."""
        root, _old_main, feature_sha, tmp = _init_repo_with_branch()
        try:
            wrong_old = "0" * 40  # never matches
            ok, strategy = _try_update_ref(
                root, "main", feature_sha, wrong_old, main_was_clean=True,
            )
            self.assertFalse(ok)
            self.assertEqual(strategy, "update_ref_rejected")
            # ref unchanged
            self.assertNotEqual(
                _git(root, "rev-parse", "main").stdout.strip(), feature_sha,
            )
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()


class StageAndCommitVerificationResultShapeTest(unittest.TestCase):
    """WIP commits list failed verification items. Results come in two shapes:
    acceptance_checks -> {kind, ok, detail}; verification_commands ->
    {command, ok, returncode}. The message builder must handle both."""

    def _repo_with_change(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "repo"
        root.mkdir()
        for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
            os.environ[k] = v
        _git(root, "init", "-q", "-b", "main")
        (root / "seed.txt").write_text("x\n")
        _git(root, "add", "seed.txt"); _git(root, "commit", "-q", "-m", "init")
        (root / "evidence.md").write_text("new\n")  # uncommitted change to stage
        return root, tmp

    def test_wip_commit_with_acceptance_check_results(self):
        from orchestrator.delivery import _stage_and_commit
        root, tmp = self._repo_with_change()
        try:
            sha, is_wip = _stage_and_commit(
                root,
                external_id="DORA-X-T01",
                task_title="t",
                outcome="agent_unverified",
                verification_pass=False,
                verification_results=[
                    {"kind": "shell", "ok": False, "detail": "grep -Eq rc=0 (no match)"},
                    {"kind": "contains_sections", "ok": True, "detail": "all sections present"},
                ],
                run_id="r1",
                branch="feature",
                is_wip=True,
            )
            self.assertTrue(sha)
            self.assertTrue(is_wip)
            msg = _git(root, "log", "-1", "--format=%B").stdout
            self.assertIn("shell", msg)        # failed acceptance check surfaced
            self.assertIn("FAIL", msg)
        finally:
            tmp.cleanup()

    def test_wip_commit_with_command_results(self):
        from orchestrator.delivery import _stage_and_commit
        root, tmp = self._repo_with_change()
        try:
            sha, _ = _stage_and_commit(
                root,
                external_id="DORA-X-T02",
                task_title="t",
                outcome="agent_unverified",
                verification_pass=False,
                verification_results=[{"command": "mvn -q test", "ok": False, "returncode": 1}],
                run_id="r2",
                branch="feature",
                is_wip=True,
            )
            self.assertTrue(sha)
            msg = _git(root, "log", "-1", "--format=%B").stdout
            self.assertIn("mvn -q test", msg)
            self.assertIn("rc=1", msg)
        finally:
            tmp.cleanup()
