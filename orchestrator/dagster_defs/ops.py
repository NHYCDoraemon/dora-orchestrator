"""Per-project Dagster ops for the run-ready-batch-task pipeline.

The 8-op graph (closure-built per project so names are slug-prefixed):

    pick_ready -> claim -> ensure_worktree -> run_executor -> run_verification
                -> commit_push -> ensure_pr -> release

Each op returns a dataclass; downstream ops short-circuit when the upstream
result has `skipped=True`. This keeps the linear graph simple while letting
us "exit early" when there's no ready issue, or when an upstream op classified
the run as a non-business outcome.
"""

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dagster import MetadataValue, OpExecutionContext, op

if TYPE_CHECKING:
    from .factory import ProjectConfig


# ──────────────────── op output dataclasses ────────────────────


@dataclass
class PickResult:
    has_ready: bool
    issue: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimResult:
    skipped: bool
    external_id: str = ""
    batch_id: str = ""
    issue: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""


@dataclass
class WorktreeInfo:
    skipped: bool
    external_id: str = ""
    batch_id: str = ""
    branch: str = ""
    worktree_path: str = ""
    issue: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    created: bool = False


@dataclass
class ExecResult:
    skipped: bool
    worktree: WorktreeInfo | None = None
    outcome: str = ""
    summary: str = ""
    touched_files: list[str] = field(default_factory=list)
    event_log_path: str = ""


@dataclass
class VerifyResult:
    skipped: bool
    exec: ExecResult | None = None
    pass_: bool = True
    skipped_no_commands: bool = True
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CommitResult:
    skipped: bool
    verify: VerifyResult | None = None
    sha: str = ""
    is_wip: bool = False
    pushed: bool = False


@dataclass
class PRResult:
    skipped: bool
    commit: CommitResult | None = None
    pr_url: str = ""


# ──────────────────── helpers ────────────────────


def _extract_batch_id(external_id: str) -> str:
    """`DORA-AGCORE-20260501C-T01` → `20260501C` (third dash-delimited segment)."""
    parts = external_id.split("-")
    return parts[2] if len(parts) >= 3 else ""


def _client():
    from orchestrator.plane_live import LivePlaneClient, LivePlaneSettings

    return LivePlaneClient(LivePlaneSettings.from_env())


def _emit(client, project_slug: str, external_id: str, marker: str, body: str):
    try:
        client.add_comment(project_slug, external_id, body, marker=marker)
    except (RuntimeError, KeyError, ValueError):
        # Comments are best-effort; never let a Plane glitch fail the op.
        pass


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


# ──────────────────── op factories ────────────────────


def build_ops(cfg: "ProjectConfig") -> dict[str, Any]:
    """Return a dict of ops, name-spaced by `cfg.slug`."""

    return {
        "pick_ready": _build_pick_ready(cfg),
        "claim": _build_claim(cfg),
        "ensure_worktree": _build_ensure_worktree(cfg),
        "run_executor": _build_run_executor(cfg),
        "run_verification": _build_run_verification(cfg),
        "commit_push": _build_commit_push(cfg),
        "ensure_pr": _build_ensure_pr(cfg),
        "release": _build_release(cfg),
        "merge_to_main": _build_merge_to_main(cfg),
    }


def _build_pick_ready(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_pick_ready",
        description=f"Ask Plane for the next ready batch task for project '{cfg.slug}'.",
    )
    def pick_ready(context: OpExecutionContext) -> PickResult:
        client = _client()
        issue = client.next_ready_issue(cfg.slug)
        if issue is None:
            context.log.info("no ready issue; this run will exit early")
            context.add_output_metadata({"has_ready": MetadataValue.bool(False)})
            return PickResult(has_ready=False)
        context.log.info(
            f"picked {issue['external_id']} (priority={issue.get('priority')})"
        )
        context.add_output_metadata(
            {
                "has_ready": MetadataValue.bool(True),
                "external_id": MetadataValue.text(str(issue["external_id"])),
                "priority": MetadataValue.text(str(issue.get("priority", ""))),
                "module": MetadataValue.text(str(issue.get("module", ""))),
            }
        )
        return PickResult(has_ready=True, issue=issue)

    return pick_ready


def _build_claim(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_claim",
        description=(
            f"Atomically transition the picked issue from Backlog to In Progress "
            f"and assert ownership via assignees=[agent_uuid]."
        ),
    )
    def claim(context: OpExecutionContext, pick: PickResult) -> ClaimResult:
        if not pick.has_ready:
            return ClaimResult(skipped=True)
        external_id = str(pick.issue["external_id"])
        batch_id = _extract_batch_id(external_id)
        client = _client()
        claimed = client.claim_issue(cfg.slug, external_id, context.run_id)
        _emit(
            client,
            cfg.slug,
            external_id,
            "dora-loop:claim",
            f"claimed by dagster run_id={context.run_id} job={context.job_name}",
        )
        context.log.info(f"claimed {external_id} (batch={batch_id})")
        context.add_output_metadata(
            {
                "external_id": MetadataValue.text(external_id),
                "batch_id": MetadataValue.text(batch_id),
            }
        )
        return ClaimResult(
            skipped=False,
            external_id=external_id,
            batch_id=batch_id,
            issue=claimed,
            run_id=context.run_id,
        )

    return claim


def _build_ensure_worktree(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_ensure_worktree",
        description=(
            f"Idempotent: ensure git worktree at "
            f"{cfg.worktree_root / cfg.slug}/<batch_id> exists on branch "
            f"{cfg.git_branch_prefix}/<batch_id>, anchored at {cfg.git_base_branch}."
        ),
    )
    def ensure_worktree(context: OpExecutionContext, claim: ClaimResult) -> WorktreeInfo:
        if claim.skipped:
            return WorktreeInfo(skipped=True)
        batch_id = claim.batch_id
        branch = f"{cfg.git_branch_prefix}/{batch_id}"
        worktree_path = cfg.worktree_root / cfg.slug / batch_id
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        client = _client()
        created = False
        if not worktree_path.exists():
            # First task in this batch — create worktree on a new branch from base.
            check = _git(
                cfg.repo_root,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            )
            if check.returncode == 0:
                # Branch exists but worktree directory was removed — re-attach.
                proc = _git(
                    cfg.repo_root,
                    "worktree",
                    "add",
                    str(worktree_path),
                    branch,
                )
            else:
                proc = _git(
                    cfg.repo_root,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree_path),
                    cfg.git_base_branch,
                )
            if proc.returncode != 0:
                _emit(
                    client,
                    cfg.slug,
                    claim.external_id,
                    "dora-loop:err:worktree",
                    f"git worktree add failed:\n{proc.stderr}",
                )
                # Bubble as Partial via release path: mark skipped but propagate
                # the failure context. We still attach a label later.
                raise RuntimeError(f"git worktree add failed: {proc.stderr}")
            created = True

        # Refresh-on-reuse: when an existing worktree is being picked up for
        # a NEW task in the batch, main may have moved (eg. another batch
        # merged, or a manual merge). Pull main into the branch BEFORE the
        # executor runs so the agent sees the current trunk state and picks
        # non-colliding identifiers (ADR numbers, file names, etc.).
        # First-run worktrees are skipped — they were just branched from main.
        refreshed = False
        if not created:
            main_sha = _git(cfg.repo_root, "rev-parse", cfg.git_base_branch).stdout.strip()
            branch_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
            already_contains_main = (
                main_sha == branch_sha
                or _git(cfg.repo_root, "merge-base", "--is-ancestor", main_sha, branch_sha).returncode == 0
            )
            if not already_contains_main:
                # Worktree must be clean before merge — leftover state from a
                # killed run could otherwise be silently consumed by the merge.
                wt_status = _git(worktree_path, "status", "--porcelain")
                if wt_status.stdout.strip():
                    _emit(
                        client, cfg.slug, claim.external_id,
                        "dora-loop:err:stale-base",
                        f"worktree {worktree_path} has uncommitted state from a prior run; refusing to refresh:\n{wt_status.stdout}",
                    )
                    client.add_label(cfg.slug, claim.external_id, "needs:input")
                    raise RuntimeError(f"worktree dirty before refresh: {worktree_path}")
                merge = subprocess.run(
                    [
                        "git", "merge", "--no-ff",
                        "-m", f"merge: refresh {cfg.git_base_branch} into {branch}",
                        cfg.git_base_branch,
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
                    _emit(
                        client, cfg.slug, claim.external_id,
                        "dora-loop:err:stale-base",
                        (
                            f"branch {branch} fell behind {cfg.git_base_branch}; "
                            f"the auto-refresh merge conflicts.\n\n"
                            f"git stderr:\n{merge.stderr.strip()[:600]}\n\n"
                            f"Resolve manually before retrying:\n"
                            f"  cd {worktree_path}\n"
                            f"  git merge {cfg.git_base_branch}\n"
                            f"  # resolve conflict markers, commit\n"
                            f"  git push . HEAD:{cfg.git_base_branch}  # (when ready)"
                        ),
                    )
                    client.add_label(cfg.slug, claim.external_id, "needs:input")
                    raise RuntimeError(
                        f"stale-base refresh conflict in {branch}; needs:input added"
                    )
                refreshed = True
                refreshed_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
                _emit(
                    client, cfg.slug, claim.external_id,
                    "dora-loop:branch:refreshed",
                    (
                        f"refreshed {branch} ↔ {cfg.git_base_branch} → {refreshed_sha}\n"
                        f"(branch was at {branch_sha[:12]}, main was at {main_sha[:12]})"
                    ),
                )
                context.log.info(
                    f"refreshed {branch}: merged {main_sha[:8]} into {branch_sha[:8]} → {refreshed_sha[:8]}"
                )

        sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
        _emit(
            client,
            cfg.slug,
            claim.external_id,
            "dora-loop:branch",
            f"worktree={worktree_path}\nbranch={branch}\nhead={sha}\ncreated={created}",
        )
        context.log.info(f"worktree at {worktree_path} on {branch} @ {sha} (created={created})")
        context.add_output_metadata(
            {
                "branch": MetadataValue.text(branch),
                "worktree_path": MetadataValue.path(str(worktree_path)),
                "head_sha": MetadataValue.text(sha),
                "created": MetadataValue.bool(created),
            }
        )
        return WorktreeInfo(
            skipped=False,
            external_id=claim.external_id,
            batch_id=batch_id,
            branch=branch,
            worktree_path=str(worktree_path),
            issue=claim.issue,
            run_id=claim.run_id,
            created=created,
        )

    return ensure_worktree


def _build_run_executor(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_run_executor",
        description=(
            f"Render the prompt from the Plane issue body and run the configured "
            f"executor (default '{cfg.default_executor}') in the worktree."
        ),
    )
    def run_executor(context: OpExecutionContext, worktree: WorktreeInfo) -> ExecResult:
        if worktree.skipped:
            return ExecResult(skipped=True, worktree=worktree)
        from orchestrator.executor_protocol import TaskRunContext
        from orchestrator.executors import get_executor
        from orchestrator.local_artifacts import create_run_artifacts

        client = _client()
        repo_path = Path(worktree.worktree_path)
        artifacts = create_run_artifacts(repo_path, worktree.run_id)
        body = str(worktree.issue.get("body") or worktree.issue.get("name") or "")
        prompt = _render_executor_prompt(worktree, body)
        artifacts.prompt_path.write_text(prompt, encoding="utf-8")

        executor_name = (
            os.environ.get(f"{cfg.slug.upper()}_EXECUTOR")
            or str(worktree.issue.get("agent_hint") or "")
            or cfg.default_executor
        )
        executor = get_executor(executor_name)

        # Extract one human-readable line per significant stream-json event so
        # the Dagster UI shows live progress while the full JSONL still goes
        # to event_path. Bounded to keep run-log size reasonable.
        emitted = [0]
        max_emits = 500

        def stream_to_log(line: str) -> None:
            if emitted[0] >= max_emits:
                return
            pretty = _pretty_stream_event(line)
            if pretty is None:
                return
            context.log.info(pretty)
            emitted[0] += 1

        idle_timeout = int(
            os.environ.get(
                f"{cfg.slug.upper()}_IDLE_TIMEOUT",
                str(min(600, cfg.max_runtime_seconds // 6)),
            )
        )
        ctx = TaskRunContext(
            run_id=worktree.run_id,
            issue_key=str(worktree.issue.get("key") or worktree.external_id),
            external_id=worktree.external_id,
            project_slug=cfg.slug,
            repo_root=repo_path,
            branch=worktree.branch,
            agent=executor_name,
            prompt_path=artifacts.prompt_path,
            event_path=artifacts.event_path,
            verification_level=list(worktree.issue.get("verification_level") or []),
            idle_timeout_seconds=idle_timeout,
            hard_timeout_seconds=cfg.max_runtime_seconds,
            on_line=stream_to_log,
        )
        result = executor.run(ctx)

        _emit(
            client,
            cfg.slug,
            worktree.external_id,
            f"dora-loop:tool:{executor_name}",
            (
                f"outcome={result.outcome}\n"
                f"summary={result.summary}\n"
                f"touched_files={[str(p) for p in result.touched_files]}\n"
                f"event_log={artifacts.event_path}"
            ),
        )
        context.log.info(
            f"executor={executor_name} outcome={result.outcome} "
            f"touched={len(result.touched_files)}"
        )
        context.add_output_metadata(
            {
                "executor": MetadataValue.text(executor_name),
                "outcome": MetadataValue.text(result.outcome),
                "touched_count": MetadataValue.int(len(result.touched_files)),
                "event_log": MetadataValue.path(str(artifacts.event_path)),
            }
        )
        return ExecResult(
            skipped=False,
            worktree=worktree,
            outcome=result.outcome,
            summary=result.summary,
            touched_files=[str(p) for p in result.touched_files],
            event_log_path=str(artifacts.event_path),
        )

    return run_executor


def _build_run_verification(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_run_verification",
        description=(
            f"Run the issue's `verification_commands` (shell) inside the worktree. "
            f"Empty list = `skipped_no_commands=True`, `pass_=True`."
        ),
    )
    def run_verification(context: OpExecutionContext, exec_result: ExecResult) -> VerifyResult:
        if exec_result.skipped or exec_result.worktree is None:
            return VerifyResult(skipped=True, exec=exec_result)
        wt = exec_result.worktree
        commands = list(wt.issue.get("verification_commands") or [])
        client = _client()
        if not commands:
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:verify",
                "verification: skipped (no commands declared)",
            )
            return VerifyResult(skipped=False, exec=exec_result, pass_=True, skipped_no_commands=True)

        results: list[dict[str, Any]] = []
        all_pass = True
        for command in commands:
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(wt.worktree_path),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                ok = proc.returncode == 0
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                ok = False
                rc = None
            all_pass = all_pass and ok
            results.append({"command": command, "ok": ok, "returncode": rc})

        body_lines = [f"verification: {'pass' if all_pass else 'fail'}"]
        for r in results:
            body_lines.append(f"  - rc={r['returncode']} {'ok' if r['ok'] else 'FAIL'}: {r['command']}")
        _emit(client, cfg.slug, wt.external_id, "dora-loop:verify", "\n".join(body_lines))
        context.log.info(f"verification {'pass' if all_pass else 'FAIL'} ({len(commands)} cmds)")
        context.add_output_metadata(
            {
                "pass": MetadataValue.bool(all_pass),
                "command_count": MetadataValue.int(len(commands)),
                "results": MetadataValue.json(results),
            }
        )
        return VerifyResult(
            skipped=False,
            exec=exec_result,
            pass_=all_pass,
            skipped_no_commands=False,
            results=results,
        )

    return run_verification


def _build_commit_push(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_commit_push",
        description=(
            "Stage and commit any worktree changes. Verify-fail commits are "
            "prefixed with `[WIP]` so reviewers can still see the diff. "
            "Push to remote if one is configured AND `enable_push=True`."
        ),
    )
    def commit_push(context: OpExecutionContext, verify: VerifyResult) -> CommitResult:
        if verify.skipped or verify.exec is None or verify.exec.worktree is None:
            return CommitResult(skipped=True, verify=verify)
        wt = verify.exec.worktree
        client = _client()
        repo = Path(wt.worktree_path)

        # Anything to commit?
        diff = _git(repo, "status", "--porcelain")
        if not diff.stdout.strip():
            context.log.info("no changes in worktree; nothing to commit")
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:commit",
                "no changes — executor produced no diff",
            )
            return CommitResult(skipped=False, verify=verify, sha="", is_wip=False, pushed=False)

        is_wip = not verify.pass_
        title = str(wt.issue.get("name") or wt.external_id)
        prefix = "[WIP] " if is_wip else ""
        msg_lines = [
            f"{prefix}{wt.external_id}: {title}",
            "",
            f"executor outcome: {verify.exec.outcome}",
            f"verification: {'pass' if verify.pass_ else 'FAIL'}",
        ]
        if not verify.pass_:
            for r in verify.results:
                msg_lines.append(f"  - rc={r['returncode']} {'ok' if r['ok'] else 'FAIL'}: {r['command']}")
        msg_lines.extend(["", f"dagster-run-id: {wt.run_id}", f"plane-issue: {wt.external_id}"])
        commit_msg = "\n".join(msg_lines) + "\n"

        _git(repo, "add", "-A", check=True)
        commit_proc = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_proc.returncode != 0:
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:err:commit",
                f"git commit failed:\n{commit_proc.stderr}",
            )
            raise RuntimeError(f"git commit failed: {commit_proc.stderr}")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _emit(
            client,
            cfg.slug,
            wt.external_id,
            "dora-loop:commit",
            f"sha={sha}\nbranch={wt.branch}\nis_wip={is_wip}\nmessage:\n{commit_msg}",
        )

        pushed = False
        if cfg.enable_push and _has_remote(cfg.repo_root):
            push = _git(repo, "push", "-u", "origin", wt.branch)
            pushed = push.returncode == 0
            if pushed:
                _emit(
                    client,
                    cfg.slug,
                    wt.external_id,
                    "dora-loop:push",
                    f"pushed {wt.branch}",
                )
            else:
                _emit(
                    client,
                    cfg.slug,
                    wt.external_id,
                    "dora-loop:err:push",
                    f"git push failed:\n{push.stderr}",
                )

        context.add_output_metadata(
            {
                "sha": MetadataValue.text(sha),
                "is_wip": MetadataValue.bool(is_wip),
                "pushed": MetadataValue.bool(pushed),
                "message": MetadataValue.md(f"```\n{commit_msg}\n```"),
            }
        )
        return CommitResult(skipped=False, verify=verify, sha=sha, is_wip=is_wip, pushed=pushed)

    return commit_push


def _build_ensure_pr(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_ensure_pr",
        description=(
            "If `enable_pr=True` and remote+gh available, create or update a draft "
            "PR for this batch's branch. Otherwise no-op (just records branch state "
            "as a Plane comment)."
        ),
    )
    def ensure_pr(context: OpExecutionContext, commit: CommitResult) -> PRResult:
        if commit.skipped or commit.verify is None or commit.verify.exec is None or commit.verify.exec.worktree is None:
            return PRResult(skipped=True, commit=commit)
        wt = commit.verify.exec.worktree
        client = _client()

        if not (cfg.enable_pr and _has_remote(cfg.repo_root) and commit.pushed):
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:pr",
                f"PR skipped (enable_pr={cfg.enable_pr} has_remote={_has_remote(cfg.repo_root)} pushed={commit.pushed}); branch={wt.branch}",
            )
            return PRResult(skipped=False, commit=commit, pr_url="")

        # Try to find existing PR for this branch first.
        view = subprocess.run(
            ["gh", "pr", "view", wt.branch, "--json", "url,state,number"],
            cwd=str(cfg.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if view.returncode == 0:
            data = json.loads(view.stdout)
            pr_url = data.get("url", "")
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:pr",
                f"existing PR found: {pr_url} (state={data.get('state')})",
            )
            context.add_output_metadata({"pr_url": MetadataValue.url(pr_url)})
            return PRResult(skipped=False, commit=commit, pr_url=pr_url)

        title = f"orchestrator: batch {wt.batch_id}"
        body = (
            f"Auto-PR opened by orchestrator for batch `{wt.batch_id}`.\n\n"
            f"Tracks tasks under `{cfg.slug}` with prefix `DORA-*-{wt.batch_id}-T*`. "
            f"Each task adds its own commit; reviewers can examine the diff per commit. "
            f"WIP commits indicate verification failures and need human review.\n"
        )
        create = subprocess.run(
            ["gh", "pr", "create", "--draft", "--title", title, "--body", body, "--head", wt.branch],
            cwd=str(cfg.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        pr_url = create.stdout.strip() if create.returncode == 0 else ""
        marker = "dora-loop:pr" if create.returncode == 0 else "dora-loop:err:pr"
        body_msg = (
            f"PR opened: {pr_url}" if create.returncode == 0
            else f"gh pr create failed:\n{create.stderr}"
        )
        _emit(client, cfg.slug, wt.external_id, marker, body_msg)
        if pr_url:
            context.add_output_metadata({"pr_url": MetadataValue.url(pr_url)})
        return PRResult(skipped=False, commit=commit, pr_url=pr_url)

    return ensure_pr


def _build_release(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_release",
        description=(
            "Final state machine: derive Plane state (Done/Partial) and labels "
            "(needs:review on verify-fail / needs:input on hard error) from the "
            "upstream op outputs, then `release_issue` and `update_page` the Batch Page."
        ),
    )
    def release(context: OpExecutionContext, pr: PRResult) -> dict[str, Any]:
        if pr.skipped:
            context.log.info("release: pipeline was no-op (no ready issue or skipped upstream)")
            return {"outcome": "no_ready"}
        wt = pr.commit.verify.exec.worktree  # type: ignore[union-attr]
        verify = pr.commit.verify  # type: ignore[union-attr]
        exec_r = verify.exec  # type: ignore[union-attr]
        client = _client()

        # Executor exit code is necessary but not sufficient for Done.
        # An agent that exits 0 without writing any code (eg. claude asking a
        # clarifying question, codex hitting auth fail-fast) must NOT be marked
        # Done; downgrade to `agent_no_changes` → Partial + needs:review.
        commit_made = bool(pr.commit.sha)  # type: ignore[union-attr]
        if exec_r.outcome == "agent_done" and verify.pass_ and commit_made:
            outcome = "agent_done"
        elif exec_r.outcome == "agent_done" and not verify.pass_:
            outcome = "agent_unverified"
        elif exec_r.outcome == "agent_done" and not commit_made:
            outcome = "agent_no_changes"
        elif (
            exec_r.outcome.startswith("agent_err")
            and commit_made
            and verify.pass_
        ):
            # Agent exited non-zero but produced verified code.
            # Treat as done — the artifact is good even if the process
            # had a non-zero exit (e.g. a cleanup step failed after
            # all substantive work passed).
            context.log.info(
                f"agent_err + commit + verify-pass → treating as agent_done"
            )
            outcome = "agent_done"
        else:
            outcome = exec_r.outcome
        terminal_state = "Done" if outcome == "agent_done" else "Partial"

        if outcome != "agent_done":
            label = (
                "needs:config"
                if outcome == "agent_config_error"
                else (
                    "needs:auto-retry"
                    if outcome == "agent_transient_error"
                    else (
                        "needs:review"
                        if outcome
                        in (
                            "agent_unverified",
                            "agent_no_changes",
                            "agent_idle_timeout",
                            "agent_hard_timeout",
                        )
                        else "needs:input"
                    )
                )
            )
            try:
                client.add_label(cfg.slug, wt.external_id, label)
            except Exception as exc:
                context.log.error(f"add_label failed (non-fatal): {exc}")

        try:
            client.publish_run_report(
                cfg.slug,
                wt.external_id,
                {
                    "run_id": wt.run_id,
                    "outcome": outcome,
                    "summary": exec_r.summary,
                    "verification": {
                        "pass": verify.pass_,
                        "skipped": verify.skipped_no_commands,
                        "results": verify.results,
                    },
                    "branch": wt.branch,
                    "commit_sha": pr.commit.sha,  # type: ignore[union-attr]
                    "is_wip": pr.commit.is_wip,  # type: ignore[union-attr]
                    "pushed": pr.commit.pushed,  # type: ignore[union-attr]
                    "pr_url": pr.pr_url,
                },
            )
        except Exception as exc:
            context.log.error(f"publish_run_report failed (non-fatal): {exc}")

        # Release MUST succeed — retry is built into release_issue.
        # If it still fails, we let the exception propagate so Dagster
        # can retry the entire release step rather than silently leaving
        # the task stuck "In Progress".
        client.release_issue(cfg.slug, wt.external_id, terminal_state)
        context.log.info(
            f"release_issue: {wt.external_id} → {terminal_state} (outcome={outcome})"
        )

        # Best-effort Batch Page refresh.
        try:
            _refresh_batch_page(client, cfg.slug, wt.batch_id)
        except (RuntimeError, KeyError, ValueError) as exc:
            _emit(
                client,
                cfg.slug,
                wt.external_id,
                "dora-loop:err:page",
                f"batch page refresh skipped: {exc}",
            )

        result = {
            "outcome": outcome,
            "state": terminal_state,
            "external_id": wt.external_id,
            "batch_id": wt.batch_id,
            "branch": wt.branch,
            "commit_sha": pr.commit.sha,  # type: ignore[union-attr]
            "is_wip": pr.commit.is_wip,  # type: ignore[union-attr]
            "pr_url": pr.pr_url,
        }
        context.add_output_metadata({k: MetadataValue.text(str(v)) for k, v in result.items()})
        try:
            context.instance.add_run_tags(
                context.run_id,
                {
                    f"{cfg.slug}/outcome": outcome,
                    f"{cfg.slug}/external_id": wt.external_id,
                    f"{cfg.slug}/batch_id": wt.batch_id,
                    f"{cfg.slug}/business_completed": "true" if outcome == "agent_done" else "false",
                },
            )
        except Exception:  # pragma: no cover — tagging is best-effort
            pass
        return result

    return release


def _build_merge_to_main(cfg: "ProjectConfig"):
    @op(
        name=f"{cfg.slug}_merge_to_main",
        description=(
            f"Fast-forward `{cfg.git_base_branch}` to the batch branch when the "
            "task reached `Done`. If main has diverged we don't force — instead "
            "tag the issue with `needs:input` and surface the conflict in a "
            "Plane comment so a human can resolve."
        ),
    )
    def merge_to_main(context: OpExecutionContext, release_result: dict[str, Any]) -> dict[str, Any]:
        outcome = str(release_result.get("outcome", ""))
        external_id = str(release_result.get("external_id", ""))
        branch = str(release_result.get("branch", ""))
        commit_sha = str(release_result.get("commit_sha", ""))

        if outcome == "no_ready":
            return {"merged": False, "reason": "no_ready"}
        if outcome != "agent_done":
            context.log.info(f"skipping merge: outcome={outcome} (only agent_done is auto-merged)")
            return {"merged": False, "reason": f"outcome={outcome}"}
        if not branch or not commit_sha:
            return {"merged": False, "reason": "missing_branch_or_sha"}

        client = _client()
        batch_id = str(release_result.get("batch_id", ""))
        worktree_path = cfg.worktree_root / cfg.slug / batch_id

        # We use `git update-ref refs/heads/<base> <new> <old>` (atomic CAS)
        # instead of `git push . <branch>:<base>`. push refuses to update a
        # ref whose branch is currently checked out anywhere — including the
        # user's main repo dir. update-ref bypasses that check and is also
        # safer (fails atomically if main moved). Trade-off: the user's main
        # checkout's working tree won't auto-refresh — we handle that below
        # by issuing a `git reset --hard HEAD` after each successful update,
        # but only when the working tree was already in sync with HEAD
        # (i.e. no real user-authored uncommitted work to clobber).
        old_main_sha = _git(cfg.repo_root, "rev-parse", cfg.git_base_branch).stdout.strip()
        branch_sha = _git(cfg.repo_root, "rev-parse", branch).stdout.strip()
        # Snapshot the cleanliness BEFORE the ref moves. If working tree
        # matches old HEAD now, refreshing to new HEAD afterwards is safe.
        # If working tree already differs, the user has live work and we
        # leave them alone.
        main_checkout_was_clean = (
            _git(cfg.repo_root, "diff", "--quiet", "HEAD").returncode == 0
            and not _git(cfg.repo_root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        )

        # Phase 1: fast-forward when branch is a strict descendant of main.
        is_ff = _git(
            cfg.repo_root, "merge-base", "--is-ancestor", old_main_sha, branch_sha
        ).returncode == 0
        if is_ff and old_main_sha != branch_sha:
            update = subprocess.run(
                ["git", "update-ref", f"refs/heads/{cfg.git_base_branch}",
                 branch_sha, old_main_sha],
                cwd=str(cfg.repo_root),
                capture_output=True, text=True, check=False,
            )
            if update.returncode == 0:
                refresh_note = _refresh_main_checkout(cfg.repo_root, main_checkout_was_clean)
                _emit(
                    client, cfg.slug, external_id, "dora-loop:merge",
                    (
                        f"fast-forwarded {cfg.git_base_branch} → {branch_sha}\n"
                        f"branch={branch}\n"
                        f"commit={commit_sha}\n"
                        f"{refresh_note}"
                    ),
                )
                context.log.info(f"FF main → {branch_sha[:12]} ({refresh_note})")
                context.add_output_metadata(
                    {
                        "merged": MetadataValue.bool(True),
                        "strategy": MetadataValue.text("fast-forward"),
                        "main_sha": MetadataValue.text(branch_sha),
                        "branch": MetadataValue.text(branch),
                    }
                )
                return {"merged": True, "strategy": "ff", "main_sha": branch_sha, "branch": branch}
            return _merge_failed(
                context, client, cfg, external_id, branch,
                "ff_update_ref_failed",
                (
                    f"FF update-ref rejected (main moved between read and write?):\n"
                    f"{update.stderr.strip()[:600]}"
                ),
            )

        # Phase 2: branch already contains main's history (e.g. ensure_worktree's
        # refresh-on-reuse merged main into the branch earlier). The branch tip
        # *is* the merged result; just advance main to point at it.
        already_merged = _git(
            cfg.repo_root, "merge-base", "--is-ancestor", old_main_sha, branch_sha
        ).returncode == 0
        if already_merged and old_main_sha != branch_sha:
            update = subprocess.run(
                ["git", "update-ref", f"refs/heads/{cfg.git_base_branch}",
                 branch_sha, old_main_sha],
                cwd=str(cfg.repo_root),
                capture_output=True, text=True, check=False,
            )
            if update.returncode == 0:
                refresh_note = _refresh_main_checkout(cfg.repo_root, main_checkout_was_clean)
                _emit(
                    client, cfg.slug, external_id, "dora-loop:merge",
                    (
                        f"advanced {cfg.git_base_branch} → {branch_sha} "
                        f"(branch already contained merge commit from "
                        f"ensure_worktree refresh-on-reuse)\n"
                        f"branch={branch}\ncommit={commit_sha}\n"
                        f"{refresh_note}"
                    ),
                )
                return {
                    "merged": True, "strategy": "advance",
                    "main_sha": branch_sha, "branch": branch,
                }

        # Phase 3: branch isn't a descendant of main → main moved after the
        # batch was last refreshed. Do a 3-way merge inside the batch worktree
        # (not the user's checkout): merge main INTO the branch, then advance
        # main via update-ref.
        if not worktree_path.exists():
            return _merge_failed(
                context, client, cfg, external_id, branch,
                "non_fast_forward",
                f"branch is not a descendant of {cfg.git_base_branch} and worktree missing at {worktree_path}.",
            )
        merge_proc = subprocess.run(
            ["git", "merge", "--no-ff", "-m",
             f"merge: {cfg.git_base_branch} into {branch}", cfg.git_base_branch],
            cwd=str(worktree_path),
            capture_output=True, text=True, check=False,
        )
        if merge_proc.returncode != 0:
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=str(worktree_path),
                capture_output=True, text=True, check=False,
            )
            return _merge_failed(
                context, client, cfg, external_id, branch,
                "conflict",
                (
                    f"3-way merge of {cfg.git_base_branch} into {branch} hit a "
                    f"conflict. Worktree restored via `git merge --abort`.\n"
                    f"git stderr:\n{merge_proc.stderr.strip()[:800]}\n\n"
                    f"Resolve manually:\n"
                    f"  cd {worktree_path}\n"
                    f"  git merge {cfg.git_base_branch}  # resolve, commit\n"
                    f"  git update-ref refs/heads/{cfg.git_base_branch} HEAD"
                ),
            )

        merge_sha = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
        update = subprocess.run(
            ["git", "update-ref", f"refs/heads/{cfg.git_base_branch}",
             merge_sha, old_main_sha],
            cwd=str(cfg.repo_root),
            capture_output=True, text=True, check=False,
        )
        if update.returncode != 0:
            return _merge_failed(
                context, client, cfg, external_id, branch,
                "update_ref_after_merge_failed",
                (
                    f"3-way merge succeeded but update-ref of "
                    f"{cfg.git_base_branch} failed:\n{update.stderr.strip()[:600]}"
                ),
            )

        refresh_note = _refresh_main_checkout(cfg.repo_root, main_checkout_was_clean)
        _emit(
            client, cfg.slug, external_id, "dora-loop:merge",
            (
                f"3-way merged {cfg.git_base_branch} ↔ {branch} → {merge_sha}\n"
                f"(main had diverged; merge commit recorded both histories)\n"
                f"commit={commit_sha}\n"
                f"{refresh_note}"
            ),
        )
        context.log.info(f"3-way merged → {merge_sha[:12]} ({refresh_note})")
        context.add_output_metadata(
            {
                "merged": MetadataValue.bool(True),
                "strategy": MetadataValue.text("3-way"),
                "main_sha": MetadataValue.text(merge_sha),
                "branch": MetadataValue.text(branch),
            }
        )
        return {"merged": True, "strategy": "3-way", "main_sha": merge_sha, "branch": branch}

    return merge_to_main


def _refresh_main_checkout(repo_root: Path, was_clean: bool) -> str:
    """After update-ref, drag the main checkout's working tree to the new HEAD.

    Only runs when `was_clean` is True (snapshot taken BEFORE the ref moved):
    that's our promise the user had no uncommitted work to clobber. Otherwise
    we leave drift visible so the user spots the conflict between their work
    and the auto-merge.

    Returns a one-line status string for the Plane comment.
    """
    if not was_clean:
        return (
            "main checkout had uncommitted changes — left as-is. After committing "
            "or stashing your work, run `git reset --hard HEAD` to pick up the "
            "merge."
        )
    reset = subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=str(repo_root),
        capture_output=True, text=True, check=False,
    )
    if reset.returncode != 0:
        return f"auto-refresh failed: {reset.stderr.strip()[:200]}"
    return "main checkout working tree refreshed to new HEAD"


def _merge_failed(context, client, cfg, external_id: str, branch: str,
                  reason: str, body: str) -> dict[str, Any]:
    """Shared failure path for `merge_to_main`: tag the issue `needs:input`,
    emit a Plane comment with the failure detail, return a structured dict.
    """
    client.add_label(cfg.slug, external_id, "needs:input")
    _emit(client, cfg.slug, external_id, "dora-loop:err:merge", body)
    context.log.warning(f"merge to {cfg.git_base_branch} failed ({reason}); needs:input added")
    context.add_output_metadata(
        {
            "merged": MetadataValue.bool(False),
            "reason": MetadataValue.text(reason),
            "branch": MetadataValue.text(branch),
            "detail": MetadataValue.md(f"```\n{body}\n```"),
        }
    )
    return {"merged": False, "reason": reason, "branch": branch}


def _pretty_stream_event(line: str) -> str | None:
    """Extract a one-line human summary from a claude/codex stream-json event.

    Returns None for events we want to suppress (thinking, system init,
    rate-limit pings) so the Dagster UI log doesn't drown.
    """
    line = line.strip()
    if not line:
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        # Codex emits some non-JSON banner lines; pass them through truncated.
        return f"[raw] {line[:200]}"
    t = e.get("type", "")
    if t == "assistant":
        out: list[str] = []
        for block in e.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "tool_use":
                inp = str(block.get("input", {}))
                out.append(f"[🔧 {block.get('name', '?')}] {inp[:200]}")
            elif bt == "text":
                txt = (block.get("text", "") or "").strip()
                if txt:
                    out.append(f"[💬] {txt[:200]}")
            # thinking blocks intentionally suppressed
        return "\n".join(out) if out else None
    if t == "user":
        for block in e.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                snippet = (str(content)[:120] if content else "(empty)").replace("\n", " ⏎ ")
                is_err = block.get("is_error")
                tag = "❌" if is_err else "📥"
                return f"[{tag}] {snippet}"
        return None
    if t == "result":
        sub = e.get("subtype", "")
        return f"[✅ result] {sub}" if sub else "[✅ result]"
    if t == "system":
        sub = e.get("subtype", "")
        if sub in ("init",):
            return f"[ℹ system] {sub}"
        return None  # suppress hook chatter
    if t in ("rate_limit_event", "thread.started", "turn.started"):
        return None  # noisy
    return None


def _render_executor_prompt(worktree: WorktreeInfo, body: str) -> str:
    """Wrap the Issue Packet body with directive framing for autonomous execution.

    Mirrors `dev_loop.sh`'s pattern: a short header asserts the run is
    autonomous and delegates decision authority to the agent, then the full
    Issue Packet follows. The agent has `--dangerously-skip-permissions`
    so it can use Edit / Write / Bash directly without escalation.
    """
    body = body if body.endswith("\n") else body + "\n"
    header = (
        "You are running unattended inside the dora orchestrator. There is no "
        "human in the loop on this run; act with full decision authority.\n\n"
        f"# Task to execute now\n"
        f"- **Plane Issue**: `{worktree.external_id}`\n"
        f"- **Batch**: `{worktree.batch_id}`\n"
        f"- **Branch / cwd**: `{worktree.branch}` (you are already inside the "
        f"worktree at `{worktree.worktree_path}`)\n"
        f"- **Run id**: `{worktree.run_id}`\n\n"
        "# Operating rules\n"
        "1. **Decide and act.** The Issue Packet below is the contract. Make "
        "the most reasonable assumption it supports and proceed; do not ask "
        "clarifying questions — there is no one to answer.\n"
        "2. **Use tools to materialize work.** Edit / Write / Bash (and any "
        "agent skills you have) are how the run produces a diff. A "
        "text-only response leaves the worktree clean and the task is "
        "marked unverified.\n"
        "3. **Stay in the worktree.** All edits go in `cwd`. Don't touch the "
        "parent dora checkout.\n"
        "4. **Don't commit.** The orchestrator stages and commits after you "
        "exit. Just leave clean edits in the worktree.\n"
        "5. **Stop conditions are real.** If the Issue Packet's Stop "
        "Conditions trigger, stop, document why in a TODO file in the "
        "worktree, and exit cleanly. Half-written placeholders beat zero "
        "output.\n"
        "6. **Acceptance is the success bar.** When you believe each "
        "Acceptance bullet is satisfied, exit. The orchestrator will run "
        "any declared verification commands automatically.\n\n"
        "# Skills you must NOT invoke\n\n"
        "The dora repo ships a `dora-plane` skill (under `.claude/skills/"
        "dora-plane/`) that was written for the dev_loop era's "
        "`dev_loop.sh` driver. It teaches agents to claim / heartbeat / "
        "release / comment via direct Plane API calls. **Do NOT invoke it "
        "on this run.** The orchestrator already owns those concerns:\n"
        "- Claim was done by the `claim` op before you started.\n"
        "- Release will be done by the `release` op after you exit.\n"
        "- Plane comments / labels / Pages are emitted by the orchestrator "
        "  with `dora-loop:*` markers — adding more from inside the agent "
        "  produces duplicates and risks state-machine drift.\n"
        "- The skill's `DOR-NN` task-id form does not match this batch's "
        "  external_id format anyway (`DORA-<PROG>-<BATCH>-T<NN>`); calling "
        "  it would either error or operate on the wrong issue.\n\n"
        "Just do the engineering work in the worktree and exit. "
        "Other skills (e.g. `superpowers:test-driven-development`, "
        "`superpowers:systematic-debugging`) are fine when contextually "
        "relevant.\n\n"
        "---\n\n"
        "# Issue Packet\n\n"
    )
    return header + body


def _refresh_batch_page(client, project_slug: str, batch_id: str) -> None:
    """Pull all tasks for `batch_id`, render a progress block, PATCH the Batch Page."""
    states: dict[str, int] = {}
    rows: list[tuple[str, str, str]] = []
    api = client.api
    path = (
        f"/api/v1/workspaces/{client.settings.workspace_slug}/projects/"
        f"{client.project_id}/issues/"
    )
    state_id_to_name = {sid: name for name, sid in client._states().items()}
    for issue in api.paginate_v1(path):
        eid = issue.get("external_id") or ""
        if batch_id not in eid:
            continue
        state_name = state_id_to_name.get(issue.get("state"), "?")
        states[state_name] = states.get(state_name, 0) + 1
        rows.append((eid, state_name, issue.get("name", "")))
    rows.sort()
    total = sum(states.values())
    summary_parts = [f"{states.get('Done', 0)}/{total} done"]
    summary_parts.extend(
        f"{n} {s}" for s, n in sorted(states.items()) if s != "Done"
    )
    summary = " · ".join(summary_parts)
    lines = [
        f"# Batch {batch_id}",
        "",
        f"Progress: {summary}",
        "",
        f"_Last refresh: orchestrator at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}_",
        "",
        "## Tasks",
        "",
    ]
    for eid, state, name in rows:
        lines.append(f"- [{state}] {eid}: {name}")
    client.update_page(
        project_slug,
        f"batch-{batch_id}",
        body="\n".join(lines) + "\n",
        match_substring=batch_id,
    )
