"""Run one ready task through the orchestrator protocol."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import OrchestratorConfig
from .executor_protocol import TaskRunContext
from .executors import get_executor
from .in_memory_plane import InMemoryPlaneClient
from .local_artifacts import create_run_artifacts
from .plane_backends import create_plane_client
from .plane_provisioner import provision_project
from .spec_loader import load_project_spec
from .task_graph import build_task_graph

if TYPE_CHECKING:
    from .delivery import DeliveryConfig


def run_ready_task(
    config: OrchestratorConfig,
    *,
    plane_client: InMemoryPlaneClient | None = None,
    run_id: str,
) -> dict[str, object]:
    if plane_client is None:
        plane_client = create_plane_client(config)
    spec = load_project_spec(config.spec_path)
    graph = build_task_graph(spec)
    provision_project(plane_client, graph)

    issue = plane_client.next_ready_issue(spec.project_slug)
    if issue is None:
        return {"outcome": "no_ready", "run_id": run_id}

    claimed = plane_client.claim_issue(spec.project_slug, issue["external_id"], run_id)
    task = next(compiled.spec for compiled in graph.tasks if compiled.spec.external_id == issue["external_id"])
    artifacts = create_run_artifacts(config.target_repo, run_id)
    artifacts.prompt_path.write_text(_render_prompt(task.title, task.acceptance), encoding="utf-8")

    executor_name = config.executor or task.agent_hint
    executor = get_executor(executor_name)
    context = TaskRunContext(
        run_id=run_id,
        issue_key=claimed["key"],
        external_id=task.external_id,
        project_slug=task.project_slug,
        repo_root=config.target_repo,
        branch=f"dora-agent/{executor_name}/{claimed['key']}",
        agent=executor_name,
        prompt_path=artifacts.prompt_path,
        event_path=artifacts.event_path,
        verification_level=task.verification_level,
        extra_env=config.executor_env or {},
    )
    result = executor.run(context)
    terminal_state = "Done" if result.outcome == "agent_done" else "Partial"
    plane_client.publish_run_report(
        spec.project_slug,
        task.external_id,
        {
            "run_id": run_id,
            "outcome": result.outcome,
            "summary": result.summary,
            "event_path": str(artifacts.event_path),
        },
    )
    plane_client.release_issue(spec.project_slug, task.external_id, terminal_state)
    return {
        "outcome": result.outcome,
        "state": terminal_state,
        "run_id": run_id,
        "issue": claimed["key"],
        "event_path": str(artifacts.event_path),
        "touched_files": [str(path) for path in result.touched_files],
    }


def run_ready_task_from_paths(
    spec_path: Path,
    target_repo: Path,
    *,
    plane_client: InMemoryPlaneClient,
    run_id: str,
    executor: str = "noop",
) -> dict[str, object]:
    return run_ready_task(
        OrchestratorConfig(
            spec_path=spec_path.resolve(),
            target_repo=target_repo.resolve(),
            executor=executor,
        ),
        plane_client=plane_client,
        run_id=run_id,
    )


def _render_prompt(title: str, acceptance: list[str]) -> str:
    lines = [f"# {title}", "", "## Acceptance"]
    lines.extend(f"- {item}" for item in acceptance)
    return "\n".join(lines) + "\n"


def run_ready_batch_task(
    config: OrchestratorConfig,
    *,
    plane_client=None,
    run_id: str,
    max_loops: int = 10,
    max_no_progress_streak: int = 2,
    delivery: DeliveryConfig | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
) -> dict[str, object]:
    """Run ready batch-submitted tasks in a self-driven loop.

    Picks the next ready Issue from Plane, runs it, releases it, then
    immediately checks for the next ready task. Loops until no more ready
    tasks, ``max_loops`` iterations are exhausted, OR the circuit-breaker
    trips on ``max_no_progress_streak`` consecutive zero-progress runs
    (last line of defence against the 2026-05-05 runaway: a single
    Dagster run claiming the same broken task and re-feeding it to claude
    up to max_loops times).

    A "zero-progress" iteration is one where the executor returned
    something other than ``agent_done`` AND no commit landed via the
    delivery pipeline. The streak resets on any successful or commit-
    producing iteration, so genuinely partial-but-progressing runs
    (e.g. agent_done on task A, agent_idle_timeout on task B with a
    half-finished commit) keep going.

    Unlike `run_ready_task`, this does not load a JSON spec; it picks issues
    (`issue_type == "task"`) from the Plane backend, runs the configured
    executor, and runs `verification_commands` before deciding the terminal
    state.

    If *delivery* is provided, each task also goes through the git delivery
    pipeline (worktree, commit, push, PR, merge) after release.

    Returns a summary dict with ``runs`` (list of per-task results),
    ``loop_count``, and (when tripped) ``circuit_breaker_open``.
    """
    if not config.project_slug:
        raise ValueError("ORCHESTRATOR_PROJECT_SLUG is required for run_ready_batch_task")
    if plane_client is None:
        plane_client = create_plane_client(config)

    # Recover any issues left In Progress by a previous (crashed) attempt
    # of this same run_id. Without this, a Dagster retry would claim a
    # second task while the first one stays stuck In Progress forever.
    _recover_run_claims(plane_client, config.project_slug, run_id)

    runs: list[dict[str, object]] = []
    attempted_ids: set[str] = set()
    loop = 0
    no_progress_streak = 0
    circuit_breaker_open = False
    while loop < max_loops:
        loop += 1
        loop_run_id = run_id if loop == 1 else f"{run_id}-{loop}"
        result = _execute_one_task(config, plane_client, loop_run_id, attempted_ids, delivery=delivery, on_progress=on_progress)
        runs.append(result)
        ext_id = result.get("external_id")
        if ext_id:
            attempted_ids.add(str(ext_id))
        outcome = str(result.get("outcome", ""))
        if outcome == "no_ready":
            break

        # Circuit breaker: a "zero-progress" iteration is one where the
        # executor produced no commit AND did not return agent_done.
        # Examples: agent_idle_timeout with no commits, agent_unverified,
        # agent_err:N, repeated stale-reclaim of the same broken task.
        # Reset the streak on any commit-producing or agent_done run.
        commit_sha = str(result.get("commit_sha") or "")
        produced_commit = bool(commit_sha)
        is_progress = outcome == "agent_done" or produced_commit
        if is_progress:
            no_progress_streak = 0
        else:
            no_progress_streak += 1
            if no_progress_streak >= max_no_progress_streak:
                circuit_breaker_open = True
                if on_progress:
                    on_progress("circuit_breaker_open", {
                        "streak": no_progress_streak,
                        "last_outcome": outcome,
                        "last_external_id": str(ext_id or ""),
                    })
                break

    summary = {
        "outcome": runs[0]["outcome"] if runs else "no_ready",
        "run_id": run_id,
        "loop_count": loop,
        "runs": runs,
    }
    if circuit_breaker_open:
        summary["circuit_breaker_open"] = True
    return summary


def _execute_one_task(
    config: OrchestratorConfig,
    plane_client,
    run_id: str,
    attempted_ids: set[str] | None = None,
    *,
    delivery: DeliveryConfig | None = None,
    on_progress: Callable[[str, dict], None] | None = None,
) -> dict[str, object]:
    """Execute a single ready task: claim -> run -> verify -> release.

    If *delivery* is provided, git delivery steps (worktree, commit, push,
    PR, merge) run after release, controlled by the config flags.

    If *on_progress* is provided, it is called with ``(event, data)`` at
    key transitions so the caller can forward progress to a UI logger.
    """
    issue = plane_client.next_ready_issue(config.project_slug, exclude=attempted_ids or None)
    if issue is None:
        if on_progress:
            on_progress("no_ready", {"run_id": run_id})
        return {"outcome": "no_ready", "run_id": run_id}

    external_id = str(issue["external_id"])
    name = str(issue.get("name", ""))
    if on_progress:
        on_progress("picked", {"external_id": external_id, "name": name, "priority": str(issue.get("priority", ""))})

    claimed = plane_client.claim_issue(config.project_slug, external_id, run_id)
    _emit(plane_client, config.project_slug, external_id, "dora-loop:claim",
          f"claimed by run_id={run_id}")
    if on_progress:
        on_progress("claimed", {"external_id": external_id, "run_id": run_id})

    _released = False
    try:
        executor_name = config.executor or str(claimed.get("agent_hint") or "noop")

        # --- worktree / repo setup -----------------------------------------
        batch_id = _extract_batch_id(external_id)
        if delivery is not None and delivery.enable_commit:
            from .delivery import ensure_worktree

            wt_branch = f"{_batch_branch_prefix(delivery)}/{batch_id}" if batch_id else ""
            wt_path = delivery.worktree_root / delivery.project_slug / batch_id
            wt_created, wt_refreshed = ensure_worktree(
                delivery.repo_root, wt_path, wt_branch, delivery.base_branch,
                auto_clean=True,
            )
            repo_root = wt_path
            branch = wt_branch
            if wt_created:
                _emit(plane_client, config.project_slug, external_id,
                      "dora-loop:branch",
                      f"worktree={wt_path}\nbranch={branch}\ncreated=True")
            if wt_refreshed:
                _emit(plane_client, config.project_slug, external_id,
                      "dora-loop:branch:refreshed",
                      f"refreshed {branch} from {delivery.base_branch}")
        else:
            repo_root = config.target_repo
            branch = f"orchestrator/{executor_name}/{claimed['key']}"

        artifacts = create_run_artifacts(repo_root, run_id)
        if delivery is not None and delivery.enable_commit:
            prompt_text = _render_executor_prompt(
                claimed, batch_id=_extract_batch_id(external_id),
                branch=branch, worktree_path=delivery.worktree_path,
            )
        else:
            prompt_text = _render_batch_prompt(claimed)
        artifacts.prompt_path.write_text(prompt_text, encoding="utf-8")

        executor = get_executor(executor_name)
        if on_progress:
            on_progress("executor_started", {"external_id": external_id, "agent": executor_name})

        def _on_executor_line(line: str) -> None:
            if on_progress:
                summary = _format_stream_line(line)
                if summary:
                    on_progress("executor_output", {"external_id": external_id, "summary": summary})

        context = TaskRunContext(
            run_id=run_id,
            issue_key=str(claimed["key"]),
            external_id=external_id,
            project_slug=config.project_slug,
            repo_root=repo_root,
            branch=branch,
            agent=executor_name,
            prompt_path=artifacts.prompt_path,
            event_path=artifacts.event_path,
            verification_level=list(claimed.get("verification_level") or []),
            idle_timeout_seconds=delivery.idle_timeout_seconds if delivery else 600,
            hard_timeout_seconds=delivery.hard_timeout_seconds if delivery else 3600,
            extra_env=config.executor_env or {},
            on_line=_on_executor_line,
        )
        result = executor.run(context)
        if on_progress:
            on_progress("executor_done", {"external_id": external_id, "outcome": result.outcome,
                          "touched_files": len(result.touched_files)})
        _emit(plane_client, config.project_slug, external_id,
              f"dora-loop:tool:{executor_name}",
              f"outcome={result.outcome}\nsummary={result.summary}\n"
              f"touched_files={[str(p) for p in result.touched_files]}")

        verification = _run_verification_commands(
            list(claimed.get("verification_commands") or []),
            repo_root,
        )
        _emit(plane_client, config.project_slug, external_id, "dora-loop:verify",
              _format_verification(verification))
        if on_progress:
            on_progress("verification", {"external_id": external_id, "pass": verification["pass"],
                         "skipped": verification.get("skipped", False)})

        if result.outcome == "agent_done" and not verification["pass"]:
            outcome = "agent_unverified"
        else:
            outcome = result.outcome

        retry_count = _read_retry_count(claimed)
        if outcome == "agent_done":
            terminal_state = "Done"
        elif retry_count >= 3:
            outcome = "agent_unverified"
            terminal_state = "Needs Input"
        else:
            terminal_state = "Partial"
            retry_count += 1
            _write_retry_count(plane_client, config.project_slug, external_id, retry_count)

        if outcome != "agent_done" and hasattr(plane_client, "add_label"):
            label = _classify_label(outcome, terminal_state)
            plane_client.add_label(config.project_slug, external_id, label)

        plane_client.publish_run_report(
            config.project_slug,
            external_id,
            {
                "run_id": run_id,
                "outcome": outcome,
                "summary": result.summary,
                "event_path": str(artifacts.event_path),
                "verification": verification,
            },
        )
        plane_client.release_issue(config.project_slug, external_id, terminal_state)
        _released = True
        if on_progress:
            on_progress("released", {"external_id": external_id, "state": terminal_state, "outcome": outcome})
    except BaseException:
        if on_progress:
            on_progress("crash", {"external_id": external_id, "run_id": run_id})
        raise
    finally:
        if not _released:
            try:
                plane_client.release_issue(config.project_slug, external_id, "Partial")
                if on_progress:
                    on_progress("emergency_release", {"external_id": external_id, "state": "Partial"})
                _emit(plane_client, config.project_slug, external_id,
                      "dora-loop:emergency-release",
                      f"crashed run {run_id} — released to Partial")
            except Exception:
                pass

    # --- optional git delivery ----------------------------------------
    delivery_result = None
    if delivery is not None:
        from .delivery import run_delivery

        delivery_result = run_delivery(
            delivery,
            worktree_path=repo_root if delivery.enable_commit else delivery.worktree_path,
            branch=branch,
            batch_id=batch_id,
            external_id=external_id,
            task_title=str(claimed.get("name") or external_id),
            outcome=outcome,
            verification_pass=verification["pass"],
            verification_results=verification.get("results", []),
            run_id=run_id,
        )
        # Reclassify outcome based on delivery results.
        if outcome == "agent_done" and not delivery_result.commit_sha:
            outcome = "agent_no_changes"
        elif (
            outcome.startswith("agent_err")
            and delivery_result.commit_sha
            and verification["pass"]
        ):
            outcome = "agent_done"
        if delivery_result.commit_sha:
            _emit(plane_client, config.project_slug, external_id,
                  "dora-loop:commit",
                  f"sha={delivery_result.commit_sha}\n"
                  f"branch={branch}\n"
                  f"is_wip={delivery_result.is_wip}")
        if delivery_result.pushed:
            _emit(plane_client, config.project_slug, external_id,
                  "dora-loop:push",
                  f"pushed {branch}")
        if delivery_result.pr_url:
            _emit(plane_client, config.project_slug, external_id,
                  "dora-loop:pr",
                  f"PR: {delivery_result.pr_url}")
        if delivery_result.merged:
            _emit(plane_client, config.project_slug, external_id,
                  "dora-loop:merge",
                  f"merged via {delivery_result.merge_strategy}")
        if on_progress:
            on_progress("delivery", {"external_id": external_id,
                         "commit_sha": delivery_result.commit_sha,
                         "pushed": delivery_result.pushed,
                         "pr_url": delivery_result.pr_url,
                         "merged": delivery_result.merged})

    batch_id = _extract_batch_id(external_id)
    if batch_id and hasattr(plane_client, "update_page"):
        try:
            _refresh_batch_page(plane_client, config.project_slug, batch_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            _emit(plane_client, config.project_slug, external_id, "dora-loop:page",
                  f"batch page refresh skipped: {exc}")

    ret: dict[str, object] = {
        "outcome": outcome,
        "state": terminal_state,
        "run_id": run_id,
        "issue": claimed["key"],
        "external_id": external_id,
        "event_path": str(artifacts.event_path),
        "verification": verification,
        "touched_files": [str(path) for path in result.touched_files],
    }
    if delivery_result is not None:
        ret["delivery"] = {
            "commit_sha": delivery_result.commit_sha,
            "is_wip": delivery_result.is_wip,
            "pushed": delivery_result.pushed,
            "pr_url": delivery_result.pr_url,
            "merged": delivery_result.merged,
            "merge_strategy": delivery_result.merge_strategy,
        }
    return ret


def _recover_run_claims(plane_client, project_slug: str, run_id: str) -> int:
    """Release any issues stuck In Progress from a previous attempt of *run_id*.

    Returns the number of recovered issues.
    """
    recovered = 0
    # InMemory / Local backends: iterate the issues dict directly.
    if hasattr(plane_client, "issues") and isinstance(plane_client.issues, dict):
        for (slug, eid), issue in list(plane_client.issues.items()):
            if slug != project_slug:
                continue
            if issue.get("state") != "In Progress":
                continue
            assignee = str(issue.get("assignee") or "")
            if assignee and assignee.startswith(run_id):
                try:
                    plane_client.release_issue(project_slug, eid, "Backlog")
                    recovered += 1
                except (RuntimeError, KeyError):
                    pass
        return recovered
    # Live backend: use the API to find and release stale claims.
    if hasattr(plane_client, "_recover_run_claims"):
        return plane_client._recover_run_claims(project_slug, run_id)
    return recovered


def _classify_label(outcome: str, terminal_state: str) -> str:
    """Map executor outcome to a human-actionable Plane label."""
    if outcome == "agent_config_error":
        return "needs:config"
    if outcome == "agent_transient_error":
        return "needs:auto-retry"
    if outcome in (
        "agent_unverified", "agent_no_changes",
        "agent_idle_timeout", "agent_hard_timeout",
    ):
        return "needs:review"
    if terminal_state == "Partial":
        return "needs:review"
    return "needs:input"


def _emit(plane_client, project_slug, external_id, marker, body):
    if not hasattr(plane_client, "add_comment"):
        return
    try:
        plane_client.add_comment(project_slug, external_id, body, marker=marker)
    except (RuntimeError, KeyError):
        pass


def _format_stream_line(line: str) -> str | None:
    """Parse one Claude stream-json line into a single-line human-readable summary.

    Tool-use events show the concrete target (file path, command, pattern).
    Tool-result events show the *last* non-empty output line — typically more
    informative than the first (which for files is often ``package foo``).
    """
    try:
        evt = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        raw = str(line).strip()
        return f"∙ {raw}" if raw else None

    evt_type = evt.get("type", "")
    subtype = evt.get("subtype", "")

    # --- Codex exec JSON events ------------------------------------------
    if evt_type in ("thread.started", "turn.started"):
        return f"◈ {evt_type}"

    if evt_type in ("item.started", "item.completed"):
        item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
        item_type = item.get("type", "")

        if item_type == "agent_message":
            text = str(item.get("text", ""))
            stripped = text.strip()
            if stripped:
                return f"▸ {stripped}"
            return None

        if item_type == "command_execution":
            command = str(item.get("command", "")).strip()
            if evt_type == "item.started":
                return f"▶ command  |  {command}" if command else "▶ command"

            output = str(item.get("aggregated_output", "") or "")
            rc = item.get("exit_code")
            status = item.get("status", "")
            rc_label = f"rc={rc}" if rc is not None else str(status or "done")
            stripped_output = output.rstrip()
            if stripped_output:
                return f"✓ command {rc_label}\n{stripped_output}"
            return f"✓ command {rc_label}\n{command}" if command else f"✓ command {rc_label}"

        if item_type:
            item_id = str(item.get("id", ""))[:12]
            verb = "▶" if evt_type == "item.started" else "✓"
            return f"{verb} {item_type} {item_id}".rstrip()

    # --- system events ---------------------------------------------------
    if evt_type == "system":
        if subtype == "init":
            model = evt.get("model", "?")
            sid = evt.get("session_id", "")[:8]
            return f"◈  model={model}  session={sid}"
        if subtype in ("hook_started", "hook_response"):
            name = evt.get("hook_name", "?")
            event = evt.get("hook_event", "")
            outcome = evt.get("outcome", "")
            label = f"{name}:{event}"
            if outcome:
                label += f"  outcome={outcome}"
            return f"⚡ hook  {label}"
        return None

    # --- assistant message ------------------------------------------------
    if evt_type == "assistant":
        msg = evt.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for block in content:
            ct = block.get("type", "")
            if ct == "thinking":
                text = str(block.get("thinking") or block.get("text") or "")
                for tline in text.split("\n"):
                    stripped = tline.strip()
                    if stripped and not stripped.startswith("{"):
                        parts.append(f"◉  {stripped[:200]}")
                        break
            elif ct == "text":
                text = str(block.get("text", ""))
                first = text.strip().split("\n")[0][:200]
                if first:
                    parts.append(f"▸ {first}")
            elif ct == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
                target = _tool_target(name, inp)
                if target:
                    parts.append(f"▶ {name}  |  {target}")
                else:
                    parts.append(f"▶ {name}")
        return "\n".join(parts) if parts else None

    # --- user message (tool results) -------------------------------------
    if evt_type == "user":
        msg = evt.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            ct = block.get("type", "")
            if ct != "tool_result":
                continue
            raw = block.get("content", "")
            tool_id = str(block.get("tool_use_id", ""))[:12]
            if isinstance(raw, str):
                non_empty = [l for l in raw.split("\n") if l.strip()]
                if non_empty:
                    # Last non-empty line is usually the most informative:
                    # for files it's actual code, for commands it's the result.
                    last = non_empty[-1].strip()[:200]
                    if len(non_empty) == 1:
                        parts.append(f"✓ {tool_id}  |  {last}")
                    else:
                        parts.append(f"✓ {tool_id}  |  {len(raw)}c {len(non_empty)}L  |  {last}")
                elif raw.strip():
                    parts.append(f"✓ {tool_id}  |  {len(raw)} chars")
            elif isinstance(raw, list):
                n = len(raw)
                preview = ""
                if n > 0:
                    preview = str(raw[-1])[:120] if isinstance(raw[-1], str) else str(raw[-1])[:120]
                parts.append(f"✓ {tool_id}  |  list[{n}]  {preview}")
            elif isinstance(raw, dict):
                parts.append(f"✓ {tool_id}  |  dict keys={list(raw.keys())[:5]}")
        return "\n".join(parts) if parts else None

    # --- result -----------------------------------------------------------
    if evt_type == "result":
        is_error = evt.get("is_error", False)
        res_text = str(evt.get("result", "") or "")
        summary = ""
        for rline in res_text.split("\n"):
            s = rline.strip()
            if s:
                summary = s[:200]
                break
        if is_error:
            return f"✗ {subtype}  |  {summary}"
        return f"★ {subtype}  |  {summary}"

    return None


def _tool_target(name: str, inp: dict) -> str:
    """Extract the most informative target string from a tool-use input dict."""
    # Read / Edit / Write → file path (tail portion for readability)
    fp = inp.get("file_path", "")
    if fp:
        parts = fp.replace("\\", "/").split("/")
        # Show last 2-3 segments so the file is recognisable but not a mile long
        if len(parts) >= 3:
            short = "/".join(parts[-3:])
        else:
            short = fp
        return short[:120]

    # Bash → command
    cmd = inp.get("command", "")
    if cmd:
        return cmd[:150]

    # Grep / Glob → pattern
    pattern = inp.get("pattern", "")
    if pattern:
        p = str(pattern)[:120]
        path = inp.get("path", "")
        if path:
            p += f"  in {str(path)[:60]}"
        return p

    # Fallback: description
    desc = inp.get("description", "")
    if desc:
        return str(desc)[:150]

    return ""


def _format_verification(verification: dict) -> str:
    if verification.get("skipped"):
        return "verification: skipped (no commands declared)"
    lines = [f"verification: {'pass' if verification['pass'] else 'fail'}"]
    for r in verification.get("results", []):
        lines.append(f"  - rc={r['returncode']} {'ok' if r['ok'] else 'FAIL'}: {r['command']}")
    return "\n".join(lines)


def _extract_batch_id(external_id: str) -> str:
    parts = external_id.split("-")
    return parts[2] if len(parts) >= 3 else ""


def _batch_branch_prefix(delivery) -> str:
    """Return the git branch prefix from delivery config, or a sensible default."""
    if hasattr(delivery, "git_branch_prefix") and delivery.git_branch_prefix:
        return delivery.git_branch_prefix
    return "orchestrator"


def _refresh_batch_page(plane_client, project_slug: str, batch_id: str) -> None:
    """Recompute progress + recent activity, PATCH the Batch Page body."""
    if not hasattr(plane_client, "issues") or not isinstance(plane_client.issues, dict):
        return
    states: dict[str, int] = {}
    rows = []
    for (slug, eid), issue in plane_client.issues.items():
        if slug != project_slug or batch_id not in eid:
            continue
        state = str(issue.get("state") or "")
        states[state] = states.get(state, 0) + 1
        rows.append((eid, state, issue.get("name", "")))
    rows.sort()
    total = sum(states.values())
    summary = f"Progress: {states.get('Done', 0)}/{total} done · " + " · ".join(
        f"{n} {s}" for s, n in sorted(states.items()) if s != "Done"
    )
    lines = [
        f"# Batch {batch_id}",
        "",
        summary,
        "",
        "## Tasks",
        "",
    ]
    for eid, state, name in rows:
        lines.append(f"- [{state}] {eid}: {name}")
    plane_client.update_page(project_slug, f"batch-{batch_id}", body="\n".join(lines) + "\n")


def _render_batch_prompt(issue: dict) -> str:
    import re
    from html import unescape

    # Plane stores the issue body as description_html.
    raw = str(issue.get("description_html") or issue.get("body") or "")
    if raw:
        body = unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if body:
            return body if body.endswith("\n") else body + "\n"
    name = str(issue.get("name") or issue.get("external_id") or "")
    return f"# {name}\n"


def _render_executor_prompt(
    issue: dict,
    *,
    batch_id: str,
    branch: str,
    worktree_path,
) -> str:
    """Wrap the issue body with autonomous-execution framing.

    Mirrors the old ops.py header: asserts the run is unattended, provides
    worktree context, declares operating rules, and prohibits the dora-plane
    skill so the agent does NOT try to claim/heartbeat/release on its own.
    """
    from pathlib import Path

    body = _render_batch_prompt(issue)
    wt_path = str(worktree_path)
    ext_id = str(issue.get("external_id") or "")
    issue_key = str(issue.get("key") or ext_id)
    required_skills = _required_skills_for_issue(issue)
    required_skills_section = _render_required_skills_section(required_skills)
    suggested_skills = _skills_for_issue(issue, "suggested_skills")
    suggested_skills_section = _render_suggested_skills_section(suggested_skills)
    forbidden_skills = _skills_for_issue(issue, "forbidden_skills")
    forbidden_skills_section = _render_forbidden_skills_section(forbidden_skills)
    header = (
        "You are running unattended inside the dora orchestrator. There is no "
        "human in the loop on this run; act with full decision authority.\n\n"
        f"# Task to execute now\n"
        f"- **Plane Issue**: `{ext_id}`\n"
        f"- **Batch**: `{batch_id}`\n"
        f"- **Branch / cwd**: `{branch}` (you are already inside the "
        f"worktree at `{wt_path}`)\n\n"
        f"{required_skills_section}"
        f"{suggested_skills_section}"
        f"{forbidden_skills_section}"
        "# Operating rules\n"
        "1. **Decide and act.** The Issue Packet below is the contract. Make "
        "the most reasonable assumption it supports and proceed; do not ask "
        "clarifying questions -- there is no one to answer.\n"
        "2. **Use tools to materialize work.** Edit / Write / Bash (and any "
        "agent skills you have) are how the run produces a diff. A "
        "text-only response leaves the worktree clean and the task is "
        "marked unverified.\n"
        "3. **Stay in the worktree.** All edits go in `cwd`. Don't touch the "
        "parent repo checkout.\n"
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
        "The business repo may ship a `dora-plane` skill that teaches agents "
        "to claim / heartbeat / release / comment via direct Plane API calls. "
        "**Do NOT invoke it on this run.** The orchestrator already owns "
        "those concerns:\n"
        "- Claim was done by the orchestrator before you started.\n"
        "- Release will be done by the orchestrator after you exit.\n"
        "- Plane comments / labels / Pages are emitted by the orchestrator "
        "  with `dora-loop:*` markers -- adding more from inside the agent "
        "  produces duplicates and risks state-machine drift.\n\n"
        "Just do the engineering work in the worktree and exit. "
        "Other skills (e.g. `superpowers:test-driven-development`, "
        "`superpowers:systematic-debugging`) are fine when contextually "
        "relevant.\n\n"
        "---\n\n"
        "# Issue Packet\n\n"
    )
    return header + body


def _render_required_skills_section(required_skills: list[str]) -> str:
    if not required_skills:
        return ""
    lines = [
        "# Required Skills",
        "",
        "Load and use these skills before starting engineering work:",
    ]
    lines.extend(f"- REQUIRED SKILL: {skill}" for skill in required_skills)
    lines.extend(
        [
            "",
            "If this skill is unavailable, stop and report the missing skill.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_suggested_skills_section(suggested_skills: list[str]) -> str:
    if not suggested_skills:
        return ""
    lines = [
        "# Suggested Skills",
        "",
        "Consider using these skills when they fit the concrete work:",
    ]
    lines.extend(f"- SUGGESTED SKILL: {skill}" for skill in suggested_skills)
    lines.append("")
    return "\n".join(lines)


def _render_forbidden_skills_section(forbidden_skills: list[str]) -> str:
    if not forbidden_skills:
        return ""
    lines = [
        "# Forbidden Skills",
        "",
        "Do not invoke these skills in this run:",
    ]
    lines.extend(f"- FORBIDDEN SKILL: {skill}" for skill in forbidden_skills)
    lines.append("")
    return "\n".join(lines)


def _required_skills_for_issue(issue: dict) -> list[str]:
    return _skills_for_issue(issue, "required_skills")


def _skills_for_issue(issue: dict, key: str) -> list[str]:
    skills = _list_prompt_values(issue.get(key))
    if skills:
        return skills
    description = str(issue.get("description_html") or "")
    if not description:
        return []
    from .plane_live import _extract_frontmatter_list

    return _extract_frontmatter_list(description, key)


def _list_prompt_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


MAX_RETRIES = 3


def _read_retry_count(issue: dict) -> int:
    """Read `dora_retry_count` from issue frontmatter or dict key."""
    if "dora_retry_count" in issue:
        try:
            return int(issue["dora_retry_count"])
        except (ValueError, TypeError):
            pass
    html = str(issue.get("description_html") or "")
    count_str = _extract_frontmatter_value(html, "dora_retry_count")
    if count_str:
        try:
            return int(count_str.strip())
        except (ValueError, TypeError):
            pass
    return 0


def _write_retry_count(plane_client, project_slug: str, external_id: str, count: int) -> None:
    """Persist the retry count to the issue, if the backend supports it."""
    if hasattr(plane_client, "set_retry_count"):
        plane_client.set_retry_count(project_slug, external_id, count)


def _extract_frontmatter_value(description_html: str, key: str) -> str | None:
    import re as _re
    from html import unescape as _unescape
    text = _unescape(description_html)
    if "<pre>" in text and "</pre>" in text:
        text = text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    m = _re.search(rf"^{key}:\s*(.+)$", text, _re.MULTILINE)
    return m.group(1).strip() if m else None


def _run_verification_commands(commands: list[str], repo_root: Path) -> dict[str, object]:
    if not commands:
        return {"pass": True, "skipped": True, "results": []}
    results = []
    all_pass = True
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=str(repo_root),
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            ok = proc.returncode == 0
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            ok = False
            returncode = None
        all_pass = all_pass and ok
        results.append({"command": command, "ok": ok, "returncode": returncode})
    return {"pass": all_pass, "skipped": False, "results": results}
