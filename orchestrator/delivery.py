"""Optional git delivery pipeline for autonomous task runs.

When a ``DeliveryConfig`` is provided to ``_execute_one_task``, the
orchestrator will, after the executor and verification steps, optionally:
  - stage and commit changes in the worktree
  - push the branch to the remote
  - create or update a draft PR
  - fast-forward / merge the branch into the base branch

CLI invocations typically supply no DeliveryConfig (no git side-effects).
Dagster-driven runs supply a DeliveryConfig with all steps enabled.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DeliveryConfig:
    """Controls which git delivery steps are performed after a task run.

    All paths are resolved at construction time. The worktree path is
    computed from ``worktree_root / project_slug / batch_id``.
    """

    repo_root: Path
    project_slug: str
    batch_id: str = ""
    branch: str = ""
    git_branch_prefix: str = "orchestrator"
    worktree_root: Path = field(
        default_factory=lambda: Path.home() / ".dora" / "orchestrator" / "worktrees"
    )
    base_branch: str = "main"
    enable_commit: bool = False
    enable_push: bool = False
    enable_pr: bool = False
    auto_merge: bool = False
    # Subprocess watchdog: if no stdout line appears for this many seconds the
    # process group is killed and the outcome is ``agent_idle_timeout``.
    idle_timeout_seconds: int = 600
    # Hard wall clock: if the subprocess hasn't exited after this many seconds
    # the process group is killed and the outcome is ``agent_hard_timeout``.
    hard_timeout_seconds: int = 3600

    @property
    def worktree_path(self) -> Path:
        return self.worktree_root / self.project_slug / self.batch_id


@dataclass(frozen=True)
class DeliveryResult:
    """Summary of the git delivery pipeline outcome."""

    worktree_created: bool = False
    worktree_refreshed: bool = False
    commit_sha: str = ""
    is_wip: bool = False
    pushed: bool = False
    pr_url: str = ""
    merged: bool = False
    merge_strategy: str = ""


def run_delivery(
    cfg: DeliveryConfig,
    *,
    worktree_path: Path,
    branch: str,
    batch_id: str,
    external_id: str,
    task_title: str,
    outcome: str,
    verification_pass: bool,
    verification_results: list[dict],
    run_id: str,
) -> DeliveryResult:
    """Execute enabled git delivery steps in order.

    *worktree_path*, *branch*, and *batch_id* are derived from the task's
    external_id by the caller. They override whatever empty placeholders
    the DeliveryConfig may carry.

    Returns a ``DeliveryResult`` summarising what was done.
    """
    created = False
    refreshed = False

    # --- worktree -------------------------------------------------------
    if cfg.enable_commit:
        created, refreshed = ensure_worktree(
            cfg.repo_root, worktree_path, branch, cfg.base_branch,
        )

    # --- commit ---------------------------------------------------------
    commit_sha = ""
    is_wip = False
    if cfg.enable_commit and worktree_path.exists():
        commit_sha, is_wip = _stage_and_commit(
            worktree_path,
            external_id=external_id,
            task_title=task_title,
            outcome=outcome,
            verification_pass=verification_pass,
            verification_results=verification_results,
            run_id=run_id,
            branch=branch,
            is_wip=not verification_pass,
        )

    # --- push -----------------------------------------------------------
    pushed = False
    if cfg.enable_push and commit_sha and _has_remote(cfg.repo_root):
        pushed = _push_branch(cfg.repo_root, branch)

    # --- PR -------------------------------------------------------------
    pr_url = ""
    if cfg.enable_pr and pushed and _has_remote(cfg.repo_root):
        pr_url = _ensure_pr(
            cfg.repo_root, branch, batch_id,
            task_title=task_title,
        )

    # --- merge ----------------------------------------------------------
    merged = False
    merge_strategy = ""
    if cfg.auto_merge and commit_sha and outcome == "agent_done":
        merged, merge_strategy = _merge_to_base(
            cfg.repo_root, branch, cfg.base_branch,
            commit_sha=commit_sha, worktree_path=worktree_path,
        )

    return DeliveryResult(
        worktree_created=created,
        worktree_refreshed=refreshed,
        commit_sha=commit_sha,
        is_wip=is_wip,
        pushed=pushed,
        pr_url=pr_url,
        merged=merged,
        merge_strategy=merge_strategy,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

import subprocess
import time  # noqa: E402


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _has_remote(repo_root: Path) -> bool:
    proc = _git(repo_root, "remote")
    return bool(proc.stdout.strip())


def ensure_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch: str,
    base_branch: str,
    *,
    auto_clean: bool = False,
) -> tuple[bool, bool]:
    """Create or refresh a git worktree at *worktree_path* on *branch*.

    When *auto_clean* is True, dirty worktrees are reset to HEAD and
    cleaned so automation can recover from a crashed previous run without
    manual intervention.

    Returns ``(created, refreshed)``.
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    created = False
    refreshed = False

    if not worktree_path.exists():
        check = _git(repo_root, "rev-parse", "--verify", f"refs/heads/{branch}")
        if check.returncode == 0:
            _git(repo_root, "worktree", "add", str(worktree_path), branch, check=True)
        else:
            _git(
                repo_root, "worktree", "add", "-b", branch,
                str(worktree_path), base_branch, check=True,
            )
        created = True
    else:
        # Abort any in-flight merge left by a crashed run.
        _git(worktree_path, "merge", "--abort")

        wt_status = _git(worktree_path, "status", "--porcelain")
        is_dirty = bool(wt_status.stdout.strip())

        if is_dirty:
            if auto_clean:
                # A previous run crashed; discard its leftovers and refresh.
                _git(worktree_path, "reset", "--hard", "HEAD", check=True)
                _git(worktree_path, "clean", "-fd", check=True)
            else:
                # The worktree is intentionally dirty (executor just made
                # changes). Skip refresh — merging base_branch into a dirty
                # tree can create conflicts and destroy work.
                return False, False

        # Refresh on reuse: merge base_branch into branch so the executor
        # sees current trunk state and picks non-colliding identifiers.
        main_sha = _git(repo_root, "rev-parse", base_branch).stdout.strip()
        branch_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
        ancestor = _git(
            repo_root, "merge-base", "--is-ancestor",
            main_sha, branch_sha,
        )
        if ancestor.returncode != 0:
            merge = subprocess.run(
                [
                    "git", "merge", "--no-ff",
                    "-m", f"merge: refresh {base_branch} into {branch}",
                    base_branch,
                ],
                cwd=str(worktree_path),
                capture_output=True, text=True, check=False,
            )
            if merge.returncode != 0:
                subprocess.run(
                    ["git", "merge", "--abort"],
                    cwd=str(worktree_path),
                    capture_output=True, text=True, check=False,
                )
                if auto_clean:
                    # Automation path: conflict means branch diverged from
                    # main. Force-reset to base_branch — prior commits
                    # were already pushed/merged or are stale.
                    _git(worktree_path, "reset", "--hard", base_branch, check=True)
                    _git(worktree_path, "clean", "-fd", check=True)
                else:
                    raise RuntimeError(
                        f"stale-base refresh conflict in {branch}; "
                        f"resolve manually or use --auto-clean:\n"
                        f"{merge.stderr.strip()[:600]}"
                    )
            refreshed = True

    return created, refreshed


def _stage_and_commit(
    worktree_path: Path,
    *,
    external_id: str,
    task_title: str,
    outcome: str,
    verification_pass: bool,
    verification_results: list[dict],
    run_id: str,
    branch: str,
    is_wip: bool,
) -> tuple[str, bool]:
    """Stage all changes and commit. Returns ``(sha, is_wip)``."""
    diff = _git(worktree_path, "status", "--porcelain")
    if not diff.stdout.strip():
        return "", False

    prefix = "[WIP] " if is_wip else ""
    msg_lines = [
        f"{prefix}{external_id}: {task_title}",
        "",
        f"executor outcome: {outcome}",
        f"verification: {'pass' if verification_pass else 'FAIL'}",
    ]
    if not verification_pass:
        for r in verification_results:
            msg_lines.append(
                f"  - rc={r['returncode']} "
                f"{'ok' if r['ok'] else 'FAIL'}: {r['command']}"
            )
    msg_lines.extend([
        "",
        f"dagster-run-id: {run_id}",
        f"plane-issue: {external_id}",
    ])
    commit_msg = "\n".join(msg_lines) + "\n"

    _git(worktree_path, "add", "-A", check=True)
    commit_proc = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_proc.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit_proc.stderr}")

    sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    return sha, is_wip


def _push_branch(repo_root: Path, branch: str) -> bool:
    """Push *branch* to origin. Returns True on success."""
    proc = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _ensure_pr(
    repo_root: Path,
    branch: str,
    batch_id: str,
    task_title: str = "",
) -> str:
    """Create or return existing draft PR for *branch*. Returns PR URL or empty string."""
    import json

    view = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,state,number"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if view.returncode == 0:
        data = json.loads(view.stdout)
        return data.get("url", "")

    title = f"orchestrator: batch {batch_id}"
    body = (
        f"Auto-PR opened by orchestrator for batch `{batch_id}`.\n\n"
        f"Each task adds its own commit; reviewers can examine the diff "
        f"per commit. WIP commits indicate verification failures and "
        f"need human review.\n"
    )
    create = subprocess.run(
        [
            "gh", "pr", "create", "--draft",
            "--title", title, "--body", body,
            "--head", branch,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return create.stdout.strip() if create.returncode == 0 else ""


def _merge_to_base(
    repo_root: Path,
    branch: str,
    base_branch: str,
    *,
    commit_sha: str,
    worktree_path: Path,
) -> tuple[bool, str]:
    """Attempt to merge *branch* into *base_branch*.

    Returns ``(merged, strategy)`` where *strategy* is one of
    ``ff``, ``advance``, ``3-way``.
    """
    old_main_sha = _git(repo_root, "rev-parse", base_branch).stdout.strip()
    branch_sha = _git(repo_root, "rev-parse", branch).stdout.strip()

    if old_main_sha == branch_sha:
        return False, "already_at_tip"

    # Snapshot cleanliness before the ref moves.
    main_was_clean = (
        _git(repo_root, "diff", "--quiet", "HEAD").returncode == 0
        and not _git(repo_root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    )

    # Phase 1: fast-forward.
    is_ancestor = _git(
        repo_root, "merge-base", "--is-ancestor",
        old_main_sha, branch_sha,
    ).returncode == 0

    if is_ancestor:
        ok, strategy = _try_update_ref(
            repo_root, base_branch, branch_sha, old_main_sha,
            main_was_clean,
        )
        if ok:
            return True, strategy
        return False, "ff_update_ref_failed"

    # Phase 2: branch already ahead via refresh-on-reuse merge.
    # Already covered by is_ancestor above.

    # Phase 3: 3-way merge in worktree.
    if not worktree_path.exists():
        return False, "non_fast_forward_no_worktree"

    merge_proc = subprocess.run(
        [
            "git", "merge", "--no-ff",
            "-m", f"merge: {base_branch} into {branch}",
            base_branch,
        ],
        cwd=str(worktree_path),
        capture_output=True, text=True, check=False,
    )
    if merge_proc.returncode != 0:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(worktree_path),
            capture_output=True, text=True, check=False,
        )
        return False, "conflict"

    merge_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    ok, strategy = _try_update_ref(
        repo_root, base_branch, merge_sha, old_main_sha,
        main_was_clean,
    )
    if not ok:
        return False, "update_ref_after_merge_failed"
    return True, "3-way"


def _try_update_ref(
    repo_root: Path,
    base_branch: str,
    new_sha: str,
    old_sha: str,
    main_was_clean: bool,
) -> tuple[bool, str]:
    """Atomic CAS update-ref and optionally refresh the main checkout."""
    proc = subprocess.run(
        ["git", "update-ref", f"refs/heads/{base_branch}", new_sha, old_sha],
        cwd=str(repo_root),
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return False, "update_ref_rejected"

    # Refresh the main checkout's working tree.
    strategy = "ff" if main_was_clean else "ff_dirty"
    if main_was_clean:
        reset = subprocess.run(
            ["git", "reset", "--hard", "HEAD"],
            cwd=str(repo_root),
            capture_output=True, text=True, check=False,
        )
        if reset.returncode != 0:
            pass  # best-effort; the ref already moved
    return True, strategy
