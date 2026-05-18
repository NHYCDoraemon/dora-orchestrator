"""Per-project QA collection job: run scenario catalog, update Plane.

Architecture (full design in dora repo `docs/quality/scenarios/` + the
seed_plane.py header docstring):

  scenarios live in repo at `<repo_root>/docs/quality/scenarios/*.yaml`
  long-lived Plane issues `DOR-QASCEN-NNN-<slug>` mirror them
  this op runs each scenario's prompt through the dora binary, captures
  stdout/stderr/events.json under `<repo_root>/var/qa-cycles/<ts>/`,
  posts a coarse-verdict comment to the matching Plane issue, switches
  status:* labels, writes a cycle summary page, and creates an empty
  synthesis epic placeholder if any scenarios failed.

What this op DOES NOT do (delegated to Claude Code judgment session):
  - Score 4-dim rubric (surface_handled / hidden_recognized / ux / safety)
  - Synthesize cross-scenario root-cause patterns
  - Draft fix batch tasks

Coarse verdicts emitted here:
  PASS      — exit 0, stdout non-empty, no panic in stderr
  FAIL      — timeout / non-zero exit / empty stdout / "panic:" in stderr
  DEFERRED  — scenario YAML declares `requires_harness:` (out of scope here)

Sentinel safety: every QA scenario has `depends_on: [DOR-QASCEN-PIN-FOREVER-ROOT]`
so plane_ready_task_sensor never picks them up as executable tasks. Confirmed
by reading `next_ready_issue` in plane_live.py — it filters by all-deps-Done.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dagster import (
    AssetExecutionContext,
    DagsterRunStatus,
    DefaultScheduleStatus,
    Definitions,
    MaterializeResult,
    MetadataValue,
    OpExecutionContext,
    Out,
    Output,
    RetryPolicy,
    RunRequest,
    RunsFilter,
    ScheduleEvaluationContext,
    SkipReason,
    job,
    op,
    schedule,
)

from .project_config import ProjectConfig

__all__ = ["build_qa_definitions"]


QA_SENTINEL_EXTERNAL_ID = "DOR-QASCEN-PIN-FOREVER-ROOT"
QA_CYCLE_NAME = "QA-Tracking"
QA_MODULE = "verification"
COARSE_PASS = "pass"
COARSE_FAIL = "fail"
COARSE_DEFERRED = "deferred"
STATUS_LABELS = {
    "backlog": "status:backlog",
    "passing": "status:passing",
    "failing": "status:failing",
    "regression": "status:regression",
    "fix_pending": "status:fix-pending",
    "fix_verifying": "status:fix-verifying",
    "deferred": "status:deferred-needs-harness",
}
ALL_STATUS_LABELS = set(STATUS_LABELS.values())
COMMENT_MARKER_PREFIX = "qa-cycle-"


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────
# YAML — no PyYAML; use the same trivial parser dora's run.py uses.
# ─────────────────────────────────────────────────────────────────────


def _parse_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML parser supporting block scalars and one-level nesting."""
    out: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        i = 1
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith(" ") or line.startswith("\t"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _sep, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "|":
            block, i = _read_block_scalar(lines, i + 1)
            out[key] = block
            continue
        if rest == "":
            value, i = _read_nested(lines, i + 1)
            out[key] = value
            continue
        out[key] = _parse_scalar(rest)
        i += 1
    return out


def _read_block_scalar(lines: list[str], i: int) -> tuple[str, int]:
    indent: int | None = None
    buf: list[str] = []
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            buf.append("")
            i += 1
            continue
        spaces = len(ln) - len(ln.lstrip(" "))
        if indent is None:
            indent = spaces
            if indent == 0:
                break
        if spaces < indent:
            break
        buf.append(ln[indent:])
        i += 1
    while buf and buf[-1] == "":
        buf.pop()
    return ("\n".join(buf), i)


def _read_nested(lines: list[str], i: int) -> tuple[Any, int]:
    items: list[Any] = []
    pairs: dict[str, Any] = {}
    indent: int | None = None
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("#"):
            i += 1
            continue
        spaces = len(ln) - len(ln.lstrip(" "))
        if indent is None:
            indent = spaces
            if indent == 0:
                break
        if spaces < indent:
            break
        body = ln[indent:]
        if body.startswith("- "):
            items.append(_parse_scalar(body[2:].strip()))
            i += 1
            continue
        if ":" in body:
            key, _, rest = body.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest == "|":
                block, i = _read_block_scalar(lines, i + 1)
                pairs[key] = block
                continue
            if rest == "":
                value, i = _read_nested(lines, i + 1)
                pairs[key] = value
                continue
            pairs[key] = _parse_scalar(rest)
            i += 1
            continue
        i += 1
    if items and not pairs:
        return items, i
    return pairs, i


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "[]":
        return []
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if s in ("null", "~"):
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


# ─────────────────────────────────────────────────────────────────────
# Scenario discovery + naming
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scenario:
    path: Path
    seq: int
    scen_id: str
    external_id: str
    prompt: str
    flags: dict[str, Any]
    timeout_seconds: int
    requires_harness: str | None
    raw: dict[str, Any]


def _derive_slug(scen_id: str) -> str:
    m = re.match(r"^(?:chat-)?[a-z-]+?-\d{3}-(.+)$", scen_id)
    if m:
        return m.group(1)
    return scen_id.replace("chat-", "").replace("_", "-")


def _load_scenarios(scenarios_dir: Path) -> list[Scenario]:
    files = sorted(scenarios_dir.glob("*.yaml"))
    out: list[Scenario] = []
    for idx, path in enumerate(files, start=1):
        raw = _parse_yaml(path.read_text(encoding="utf-8"))
        scen_id = str(raw.get("id") or path.stem)
        slug = _derive_slug(scen_id)
        external_id = f"DOR-QASCEN-{idx:03d}-{slug}"[:80].rstrip("-")
        out.append(
            Scenario(
                path=path,
                seq=idx,
                scen_id=scen_id,
                external_id=external_id,
                prompt=str(raw.get("prompt") or "").rstrip("\n"),
                flags=dict(raw.get("flags") or {}),
                timeout_seconds=int(raw.get("timeout_seconds") or 60),
                requires_harness=(
                    str(raw["requires_harness"]) if raw.get("requires_harness") else None
                ),
                raw=raw,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────
# Run scenario through dora binary
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RunResult:
    scenario: Scenario
    cmd: list[str]
    started_at: str
    ended_at: str
    wall_seconds: float
    exit_code: int
    timed_out: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_path: Path
    stderr_path: Path
    events_path: Path
    meta_path: Path
    dora_run_id: str
    events_count: int
    coarse_verdict: str  # COARSE_PASS / COARSE_FAIL / COARSE_DEFERRED


def _run_one_scenario(
    scen: Scenario,
    dora_binary: Path,
    cycle_dir: Path,
    repo_root: Path,
) -> RunResult:
    run_dir = cycle_dir / "runs" / scen.scen_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scenario.yaml").write_text(
        scen.path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    if scen.requires_harness:
        return _stub_deferred_result(scen, run_dir, dora_binary)

    cmd: list[str] = [str(dora_binary), "chat", "-p", scen.prompt]
    if scen.flags.get("output"):
        cmd += ["--output", str(scen.flags["output"])]
    if scen.flags.get("max_turns") is not None:
        cmd += ["--max-turns", str(scen.flags["max_turns"])]
    if scen.flags.get("permission_mode"):
        cmd += ["--permission-mode", str(scen.flags["permission_mode"])]
    if "progress" in scen.flags:
        cmd += [f"--progress={'true' if scen.flags['progress'] else 'false'}"]

    isolated_db = run_dir / "dora.db"
    env = os.environ.copy()
    env["DORA_DB"] = str(isolated_db)

    started = _now_iso()
    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=scen.timeout_seconds,
            cwd=str(repo_root),
            env=env,
        )
        wall = time.time() - t0
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        wall = float(scen.timeout_seconds)
        exit_code = -1
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        timed_out = True
        # Best-effort grandchild cleanup so a timed-out dora doesn't linger.
        subprocess.run(["pkill", "-f", str(dora_binary)], check=False)

    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

    events: list[dict] = []
    dora_run_id = ""
    db_candidates = [
        isolated_db,
        repo_root / "dora.db",
        Path.home() / ".dora" / "dora.db",
    ]
    chosen_db = next((c for c in db_candidates if c.exists()), None)
    if chosen_db is not None:
        try:
            conn = sqlite3.connect(str(chosen_db))
            cur = conn.cursor()
            cur.execute(
                "SELECT aggregate_id FROM events WHERE event_type='run.started' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                dora_run_id = row[0]
                cur.execute(
                    "SELECT seq, event_type, payload_json FROM events "
                    "WHERE aggregate_id=? ORDER BY seq",
                    (dora_run_id,),
                )
                for seq, etype, payload in cur.fetchall():
                    try:
                        payload_obj = json.loads(payload) if payload else None
                    except Exception:
                        payload_obj = payload
                    events.append({"seq": seq, "event_type": etype, "payload": payload_obj})
            conn.close()
        except Exception:
            # Trace harvest is best-effort; verdict remains based on stdout/exit.
            pass

    events_path = run_dir / "events.json"
    events_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    coarse = _classify_coarse(exit_code, timed_out, stdout, stderr)

    meta = {
        "scenario_id": scen.scen_id,
        "external_id": scen.external_id,
        "scenario_path": str(scen.path),
        "binary": str(dora_binary),
        "cmd": cmd,
        "started_at": started,
        "ended_at": _now_iso(),
        "wall_seconds": round(wall, 2),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "deferred": False,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "dora_run_id": dora_run_id,
        "events_count": len(events),
        "coarse_verdict": coarse,
    }
    meta_path = run_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunResult(
        scenario=scen,
        cmd=cmd,
        started_at=started,
        ended_at=meta["ended_at"],
        wall_seconds=round(wall, 2),
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        events_path=events_path,
        meta_path=meta_path,
        dora_run_id=dora_run_id,
        events_count=len(events),
        coarse_verdict=coarse,
    )


def _stub_deferred_result(scen: Scenario, run_dir: Path, dora_binary: Path) -> RunResult:
    started = _now_iso()
    msg = f"[harness] DEFERRED: scenario {scen.scen_id} requires harness '{scen.requires_harness}'\n"
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    events_path = run_dir / "events.json"
    meta_path = run_dir / "meta.json"
    stdout_path.write_bytes(b"")
    stderr_path.write_text(msg, encoding="utf-8")
    events_path.write_text("[]", encoding="utf-8")
    meta = {
        "scenario_id": scen.scen_id,
        "external_id": scen.external_id,
        "scenario_path": str(scen.path),
        "binary": str(dora_binary),
        "cmd": [],
        "started_at": started,
        "ended_at": _now_iso(),
        "wall_seconds": 0.0,
        "exit_code": 0,
        "timed_out": False,
        "deferred": True,
        "requires_harness": scen.requires_harness or "",
        "stdout_bytes": 0,
        "stderr_bytes": len(msg),
        "dora_run_id": "",
        "events_count": 0,
        "coarse_verdict": COARSE_DEFERRED,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunResult(
        scenario=scen,
        cmd=[],
        started_at=started,
        ended_at=meta["ended_at"],
        wall_seconds=0.0,
        exit_code=0,
        timed_out=False,
        stdout_bytes=0,
        stderr_bytes=len(msg),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        events_path=events_path,
        meta_path=meta_path,
        dora_run_id="",
        events_count=0,
        coarse_verdict=COARSE_DEFERRED,
    )


def _classify_coarse(exit_code: int, timed_out: bool, stdout: bytes, stderr: bytes) -> str:
    if timed_out or exit_code == -1:
        return COARSE_FAIL
    if b"panic:" in stderr or b"\nFATAL" in stderr:
        return COARSE_FAIL
    if exit_code != 0:
        return COARSE_FAIL
    if not stdout.strip():
        return COARSE_FAIL
    return COARSE_PASS


# ─────────────────────────────────────────────────────────────────────
# Plane interaction — read prior status, write verdict, switch labels
# ─────────────────────────────────────────────────────────────────────


def _scenario_label_status(client, slug: str, external_id: str) -> str | None:
    """Return one of STATUS_LABELS values currently on the issue, or None."""
    issue = client._resolve_issue_full(external_id)
    label_ids = issue.get("labels") or []
    label_lookup = {v["id"]: k for k, v in client._labels().items()}
    for lid in label_ids:
        name = label_lookup.get(lid)
        if name in ALL_STATUS_LABELS:
            return name
    return None


def _set_status_label(client, slug: str, external_id: str, new_label: str) -> None:
    """Replace any current status:* with new_label."""
    issue = client._resolve_issue_full(external_id)
    label_ids = list(issue.get("labels") or [])
    label_lookup = {v["id"]: k for k, v in client._labels().items()}
    for lid in list(label_ids):
        name = label_lookup.get(lid)
        if name in ALL_STATUS_LABELS and name != new_label:
            client.remove_label(slug, external_id, name)
    client.add_label(slug, external_id, new_label)


def _next_status_label(prior: str | None, coarse: str) -> str:
    """Decide new status label based on transition (prior, coarse)."""
    if coarse == COARSE_DEFERRED:
        return STATUS_LABELS["deferred"]
    if coarse == COARSE_PASS:
        if prior in (STATUS_LABELS["failing"], STATUS_LABELS["regression"]):
            return STATUS_LABELS["fix_verifying"]
        if prior == STATUS_LABELS["fix_verifying"]:
            return STATUS_LABELS["passing"]
        return STATUS_LABELS["passing"]
    # COARSE_FAIL
    if prior == STATUS_LABELS["passing"]:
        return STATUS_LABELS["regression"]
    return STATUS_LABELS["failing"]


def _format_verdict_comment(cycle_ts: str, result: RunResult, prior: str | None) -> str:
    lines = [
        f"## Cycle {cycle_ts}",
        "",
        f"- coarse verdict: **{result.coarse_verdict.upper()}**",
        f"- exit code: `{result.exit_code}`{' (TIMEOUT)' if result.timed_out else ''}",
        f"- wall time: {result.wall_seconds}s",
        f"- stdout: {result.stdout_bytes} bytes",
        f"- stderr: {result.stderr_bytes} bytes",
        f"- events captured: {result.events_count}",
    ]
    if result.dora_run_id:
        lines.append(f"- dora run id: `{result.dora_run_id}`")
    lines.append(f"- prior status: `{prior or '(none)'}`")
    lines.append(f"- trace dir: `{result.meta_path.parent}`")
    if result.coarse_verdict == COARSE_FAIL:
        snippet = ""
        try:
            stdout = result.stdout_path.read_bytes()
            stderr = result.stderr_path.read_bytes()
            snippet = (stdout[:400] + b"\n--stderr--\n" + stderr[:400]).decode("utf-8", "replace")
        except Exception:
            pass
        if snippet:
            lines.append("")
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Cycle page + synthesis epic
# ─────────────────────────────────────────────────────────────────────


def _render_cycle_page(cycle_ts: str, results: list[RunResult]) -> str:
    counts = {"pass": 0, "fail": 0, "deferred": 0}
    for r in results:
        counts[r.coarse_verdict] = counts.get(r.coarse_verdict, 0) + 1
    lines = [
        f"# QA Cycle {cycle_ts}",
        "",
        f"- scenarios run: {len(results)}",
        f"- pass: {counts['pass']}",
        f"- fail: {counts['fail']}",
        f"- deferred: {counts['deferred']}",
        "",
        "## Per-scenario summary",
        "",
        "| # | external_id | verdict | wall | exit | events |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.scenario.seq} | `{r.scenario.external_id}` | "
            f"**{r.coarse_verdict.upper()}** | {r.wall_seconds}s | "
            f"{r.exit_code}{' TIMEOUT' if r.timed_out else ''} | {r.events_count} |"
        )
    lines.append("")
    lines.append("Trace artifacts: see scenario issue comments for paths.")
    return "\n".join(lines)


def _render_synthesis_placeholder(cycle_ts: str, failed: list[RunResult]) -> str:
    lines = [
        f"# Synthesis — Cycle {cycle_ts}",
        "",
        f"**Status: awaiting Claude Code judgment**",
        "",
        f"{len(failed)} scenario(s) failed in cycle {cycle_ts}. Until a Claude",
        "Code session reviews the trace + posts per-scenario rubric verdicts +",
        "drafts a fix batch, this epic body is a placeholder.",
        "",
        "## Failed scenarios (raw coarse verdict only)",
        "",
    ]
    for r in failed:
        lines.append(
            f"- `{r.scenario.external_id}` — exit {r.exit_code}"
            f"{' TIMEOUT' if r.timed_out else ''}, "
            f"{r.stdout_bytes}b stdout, {r.stderr_bytes}b stderr"
        )
    lines.extend(
        [
            "",
            "## Workflow expectation",
            "",
            "1. Claude Code reads each scenario issue's latest comment + trace.",
            "2. Posts rubric verdict comment per scenario.",
            "3. Replaces this body with cross-scenario synthesis + prioritized",
            "   fix list + catalog growth additions.",
            "4. Drafts a `DOR-CHATFIX-<batch>` fix batch with each task",
            "   `relates_to:` the scenario(s) it aims to fix.",
            "5. Submits via orch audit/approve/live submit.",
        ]
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# The op
# ─────────────────────────────────────────────────────────────────────


def build_qa_collection_op(cfg: ProjectConfig):
    """One op per project. Reads scenarios from `<repo_root>/docs/quality/scenarios/`,
    runs each through `<repo_root>/dora`, posts verdicts to Plane.
    """
    op_name = f"{cfg.safe_name}_qa_collection_op"

    @op(name=op_name)
    def _qa_collection_op(context) -> dict:
        from orchestrator.plane_live import LivePlaneClient, LivePlaneSettings

        repo_root = Path(cfg.repo_root)
        scenarios_dir = repo_root / "docs" / "quality" / "scenarios"
        dora_binary = repo_root / "dora"

        if not scenarios_dir.exists():
            context.log.warning(f"qa: scenarios dir not found: {scenarios_dir} — skip")
            return {"scenario_count": 0, "skipped": True}
        if not dora_binary.exists():
            context.log.warning(f"qa: dora binary not found: {dora_binary} — skip")
            return {"scenario_count": 0, "skipped": True}

        cycle_ts = _now_ts()
        cycle_dir = repo_root / "var" / "qa-cycles" / cycle_ts
        cycle_dir.mkdir(parents=True, exist_ok=True)
        context.log.info(f"qa: cycle {cycle_ts} → {cycle_dir}")

        scenarios = _load_scenarios(scenarios_dir)
        context.log.info(f"qa: {len(scenarios)} scenarios discovered")

        from .plane_helpers import per_project_plane_client

        client = per_project_plane_client(cfg)
        results: list[RunResult] = []
        for s in scenarios:
            context.log.info(
                f"qa: [{s.seq}/{len(scenarios)}] {s.external_id} "
                f"(harness={s.requires_harness or 'native'})"
            )
            try:
                r = _run_one_scenario(s, dora_binary, cycle_dir, repo_root)
            except Exception as exc:  # noqa: BLE001
                context.log.error(f"qa: run failed for {s.external_id}: {exc}")
                continue
            results.append(r)
            try:
                prior = _scenario_label_status(client, cfg.slug, s.external_id)
                next_label = _next_status_label(prior, r.coarse_verdict)
                comment = _format_verdict_comment(cycle_ts, r, prior)
                client.add_comment(
                    cfg.slug,
                    s.external_id,
                    comment,
                    marker=f"{COMMENT_MARKER_PREFIX}{cycle_ts}",
                )
                _set_status_label(client, cfg.slug, s.external_id, next_label)
                context.log.info(
                    f"qa:   verdict={r.coarse_verdict} prior={prior} → {next_label}"
                )
            except Exception as exc:  # noqa: BLE001
                context.log.error(f"qa: Plane update failed for {s.external_id}: {exc}")

        # Cycle summary page
        page_slug = f"qa-cycle-{cycle_ts}"
        try:
            client.upsert_page(
                cfg.slug,
                page_slug,
                {
                    "title": f"QA Cycle {cycle_ts}",
                    "body": _render_cycle_page(cycle_ts, results),
                    "page_type": "qa_cycle",
                    "batch_id": cycle_ts,
                },
            )
            context.log.info(f"qa: cycle page upserted — {page_slug}")
        except Exception as exc:  # noqa: BLE001
            context.log.error(f"qa: cycle page upsert failed: {exc}")

        # Synthesis epic placeholder if any failures
        failed = [r for r in results if r.coarse_verdict == COARSE_FAIL]
        synthesis_external_id = None
        if failed:
            synthesis_external_id = f"DOR-QASYN-{cycle_ts}"
            try:
                client.upsert_issue(
                    cfg.slug,
                    synthesis_external_id,
                    {
                        "name": f"[QA Synthesis] Cycle {cycle_ts} — {len(failed)} failures",
                        "body": _render_synthesis_placeholder(cycle_ts, failed),
                        "issue_type": "task",
                        "parent_external_id": QA_SENTINEL_EXTERNAL_ID,
                        "cycle": QA_CYCLE_NAME,
                        "module": QA_MODULE,
                        "priority": "high",
                        "depends_on": [QA_SENTINEL_EXTERNAL_ID],
                        "source_hash": f"qasyn-{cycle_ts}-v1",
                        "agent_hint": "noop",
                        "risk": "low",
                        "acceptance": [],
                        "verification_level": [],
                    },
                )
                client.add_label(cfg.slug, synthesis_external_id, "status:awaiting-synthesis")
                context.log.info(
                    f"qa: synthesis epic created — {synthesis_external_id} (failed={len(failed)})"
                )
            except Exception as exc:  # noqa: BLE001
                context.log.error(f"qa: synthesis epic upsert failed: {exc}")

        # Inbox file — Claude Code's notification channel
        try:
            inbox_path = repo_root / "docs" / "quality" / "INBOX.md"
            inbox_path.parent.mkdir(parents=True, exist_ok=True)
            line = (
                f"- {cycle_ts}: cycle done, "
                f"{len([r for r in results if r.coarse_verdict == COARSE_PASS])} pass, "
                f"{len(failed)} fail, "
                f"{len([r for r in results if r.coarse_verdict == COARSE_DEFERRED])} deferred"
            )
            if synthesis_external_id:
                line += f" — synthesis: `{synthesis_external_id}`"
            line += "\n"
            existing = inbox_path.read_text(encoding="utf-8") if inbox_path.exists() else ""
            if not existing.startswith("# QA Inbox"):
                existing = "# QA Inbox\n\nMost-recent QA cycles. Claude Code processes these in order.\n\n" + existing
            inbox_path.write_text(line + existing, encoding="utf-8")
            context.log.info(f"qa: inbox updated — {inbox_path}")
        except Exception as exc:  # noqa: BLE001
            context.log.error(f"qa: inbox write failed: {exc}")

        return {
            "cycle_ts": cycle_ts,
            "scenario_count": len(results),
            "pass_count": len([r for r in results if r.coarse_verdict == COARSE_PASS]),
            "fail_count": len(failed),
            "deferred_count": len([r for r in results if r.coarse_verdict == COARSE_DEFERRED]),
            "synthesis_epic": synthesis_external_id or "",
            "cycle_dir": str(cycle_dir),
        }

    return _qa_collection_op


def build_qa_definitions(cfg: ProjectConfig) -> Definitions:
    """Build per-project QA-only Definitions to merge with run_ready_batch_task defs."""
    qa_op = build_qa_collection_op(cfg)
    op_retry = RetryPolicy(max_retries=1, delay=10)

    job_name = f"{cfg.safe_name}_qa_collection"
    schedule_name = f"{cfg.safe_name}_qa_daily"
    concurrency_key = f"qa_{cfg.safe_name}"

    @job(
        name=job_name,
        description=(
            f"QA collection for '{cfg.slug}': run all scenario YAMLs through dora, "
            f"post verdicts to Plane scenario issues, write cycle page + synthesis "
            f"placeholder. Coarse verdicts only — Claude Code judges rubric later."
        ),
        tags={
            "dagster/concurrency_key": concurrency_key,
            f"{cfg.slug}/job_kind": "qa_collection",
        },
    )
    def qa_job():
        qa_op.with_retry_policy(op_retry)()

    @schedule(
        name=schedule_name,
        cron_schedule="0 4 * * *",  # 04:00 every day in cfg.schedule_timezone
        job=qa_job,
        execution_timezone=cfg.schedule_timezone,
        default_status=DefaultScheduleStatus.STOPPED,
        description=(
            f"Daily QA collection at 04:00 ({cfg.schedule_timezone}). "
            f"Runs the full scenario catalog and updates Plane."
        ),
    )
    def qa_schedule(context: ScheduleEvaluationContext):
        active = context.instance.get_runs(
            filters=RunsFilter(
                job_name=job_name,
                statuses=[
                    DagsterRunStatus.QUEUED,
                    DagsterRunStatus.NOT_STARTED,
                    DagsterRunStatus.STARTING,
                    DagsterRunStatus.STARTED,
                ],
            ),
            limit=1,
        )
        if active:
            return SkipReason(f"{job_name} already running (id={active[0].run_id[:8]})")
        return RunRequest(run_key=None, tags={f"{cfg.slug}/triggered_by": "qa_daily"})

    return Definitions(jobs=[qa_job], schedules=[qa_schedule])
