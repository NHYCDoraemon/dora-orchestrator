"""Run one ready task through the orchestrator protocol."""

import subprocess
from pathlib import Path

from .config import OrchestratorConfig
from .executor_protocol import TaskRunContext
from .executors import get_executor
from .in_memory_plane import InMemoryPlaneClient
from .local_artifacts import create_run_artifacts
from .plane_backends import create_plane_client
from .plane_provisioner import provision_project
from .spec_loader import load_project_spec
from .task_graph import build_task_graph


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
) -> dict[str, object]:
    """Run ready batch-submitted tasks in a self-driven loop.

    Picks the next ready Issue from Plane, runs it, releases it, then
    immediately checks for the next ready task. Loops until no more ready
    tasks or ``max_loops`` iterations are exhausted.

    Unlike `run_ready_task`, this does not load a JSON spec; it picks issues
    (`issue_type == "task"`) from the Plane backend, runs the configured
    executor, and runs `verification_commands` before deciding the terminal
    state.

    Returns a summary dict with ``runs`` (list of per-task results) and
    ``loop_count``.
    """
    if not config.project_slug:
        raise ValueError("ORCHESTRATOR_PROJECT_SLUG is required for run_ready_batch_task")
    if plane_client is None:
        plane_client = create_plane_client(config)

    runs: list[dict[str, object]] = []
    loop = 0
    while loop < max_loops:
        loop += 1
        loop_run_id = run_id if loop == 1 else f"{run_id}-{loop}"
        result = _execute_one_task(config, plane_client, loop_run_id)
        runs.append(result)
        if result["outcome"] == "no_ready":
            break

    return {
        "outcome": runs[0]["outcome"] if runs else "no_ready",
        "run_id": run_id,
        "loop_count": loop,
        "runs": runs,
    }


def _execute_one_task(
    config: OrchestratorConfig,
    plane_client,
    run_id: str,
) -> dict[str, object]:
    """Execute a single ready task: claim → run → verify → release."""
    issue = plane_client.next_ready_issue(config.project_slug)
    if issue is None:
        return {"outcome": "no_ready", "run_id": run_id}

    external_id = str(issue["external_id"])
    claimed = plane_client.claim_issue(config.project_slug, external_id, run_id)
    _emit(plane_client, config.project_slug, external_id, "dora-loop:claim",
          f"claimed by run_id={run_id}")

    artifacts = create_run_artifacts(config.target_repo, run_id)
    artifacts.prompt_path.write_text(_render_batch_prompt(claimed), encoding="utf-8")

    executor_name = config.executor or str(claimed.get("agent_hint") or "noop")
    executor = get_executor(executor_name)
    context = TaskRunContext(
        run_id=run_id,
        issue_key=str(claimed["key"]),
        external_id=external_id,
        project_slug=config.project_slug,
        repo_root=config.target_repo,
        branch=f"orchestrator/{executor_name}/{claimed['key']}",
        agent=executor_name,
        prompt_path=artifacts.prompt_path,
        event_path=artifacts.event_path,
        verification_level=list(claimed.get("verification_level") or []),
    )
    result = executor.run(context)
    _emit(plane_client, config.project_slug, external_id,
          f"dora-loop:tool:{executor_name}",
          f"outcome={result.outcome}\nsummary={result.summary}\n"
          f"touched_files={[str(p) for p in result.touched_files]}")

    verification = _run_verification_commands(
        list(claimed.get("verification_commands") or []),
        config.target_repo,
    )
    _emit(plane_client, config.project_slug, external_id, "dora-loop:verify",
          _format_verification(verification))

    if result.outcome == "agent_done" and not verification["pass"]:
        outcome = "agent_unverified"
    else:
        outcome = result.outcome
    terminal_state = "Done" if outcome == "agent_done" else "Partial"

    if outcome != "agent_done" and hasattr(plane_client, "add_label"):
        label = "needs:review" if outcome == "agent_unverified" else "needs:input"
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

    batch_id = _extract_batch_id(external_id)
    if batch_id and hasattr(plane_client, "update_page"):
        try:
            _refresh_batch_page(plane_client, config.project_slug, batch_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            _emit(plane_client, config.project_slug, external_id, "dora-loop:page",
                  f"batch page refresh skipped: {exc}")

    return {
        "outcome": outcome,
        "state": terminal_state,
        "run_id": run_id,
        "issue": claimed["key"],
        "external_id": external_id,
        "event_path": str(artifacts.event_path),
        "verification": verification,
        "touched_files": [str(path) for path in result.touched_files],
    }


def _emit(plane_client, project_slug, external_id, marker, body):
    if not hasattr(plane_client, "add_comment"):
        return
    try:
        plane_client.add_comment(project_slug, external_id, body, marker=marker)
    except (RuntimeError, KeyError):
        pass


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
