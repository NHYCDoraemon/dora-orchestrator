"""Claude executor adapter.

Runs `claude --print` non-interactively against the worktree. Permissions are
bypassed via `--dangerously-skip-permissions` so the agent can edit / test
without blocking on confirmation. Output is streamed JSONL via Popen so the
event log file stays in sync line-by-line, and an optional `on_line` callback
on `TaskRunContext` mirrors each line into the caller's logger (Dagster UI).

Subprocess watchdog: a reader thread pushes lines into a queue; the main loop
polls the queue every second and checks for (a) idle timeout — no output for
`idle_timeout_seconds`, (b) hard timeout — total runtime exceeds
`hard_timeout_seconds`, (c) orphan grandchild holding the stdout pipe open after
the direct child exited. On any timeout the entire process group is killed
(SIGTERM → 3 s → SIGKILL) via `start_new_session=True`.
"""

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time

from orchestrator.executor_protocol import ExecutorResult, TaskRunContext

# Sentinel return codes so callers can distinguish kill reasons.
RC_IDLE_TIMEOUT = -99
RC_HARD_TIMEOUT = -98

# When the direct child process has exited but the stdout pipe is still open
# (grandchild inherited the fd), wait this many seconds before force-killing
# the process group.
_ORPHAN_GRACE_SECONDS = 30

# Heartbeat cadence — emit "still waiting on agent stdout, idle for Ns" every
# N seconds to on_line (Dagster UI) AND to the events.ndjson sink so the
# operator and post-mortem tooling can both see that the orchestrator is
# still alive while waiting on a slow agent. 60 s strikes a balance between
# noise and latency.
_HEARTBEAT_INTERVAL_SECONDS = 60


class ClaudeExecutor:
    def build_command(self, context: TaskRunContext) -> list[str]:
        prompt = (
            context.prompt_path.read_text(encoding="utf-8")
            if context.prompt_path.exists()
            else ""
        )
        return [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            prompt,
        ]

    def run(self, context: TaskRunContext) -> ExecutorResult:
        env = {
            **os.environ,
            "CI": "1",
            "TERM": "dumb",
            "DEBIAN_FRONTEND": "noninteractive",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONUNBUFFERED": "1",
            "CLAUDE_CODE_NON_INTERACTIVE": "1",
            "CLAUDE_CODE_DANGEROUSLY_SKIP_PERMISSIONS": "1",
        }
        rc = _stream_subprocess(
            self.build_command(context),
            cwd=str(context.repo_root),
            env=env,
            event_path=context.event_path,
            on_line=context.on_line,
            idle_timeout_seconds=context.idle_timeout_seconds,
            hard_timeout_seconds=context.hard_timeout_seconds,
        )
        outcome, summary = _classify_outcome(
            rc,
            context.event_path,
            context.idle_timeout_seconds,
            context.hard_timeout_seconds,
            agent_label="Claude",
        )
        return ExecutorResult(
            outcome=outcome,
            summary=summary,
            touched_files=[],
        )


def _claude_result_text(event_path) -> str:
    """Default result-text extractor for claude's stream-json output.

    Walks the events file backwards looking for the last ``type:"result"``
    event and returns its ``result`` string. Empty on any read or parse error.
    """
    try:
        if not (event_path and event_path.exists()):
            return ""
        with event_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "result":
            return str(evt.get("result", ""))
    return ""


def _classify_outcome(
    rc: int,
    event_path,
    idle_timeout_seconds: int = 1800,
    hard_timeout_seconds: int = 3600,
    *,
    agent_label: str = "Claude",
    result_text_extractor=None,
) -> tuple[str, str]:
    """Derive outcome and summary from exit code and the last result event.

    Shared by ClaudeExecutor and CodexExecutor. Pass ``agent_label`` for
    summary strings and ``result_text_extractor`` to plug in the agent's
    own event schema (defaults to claude's stream-json).

    Distinguishes five error classes so downstream ops (release, merge) can
    decide whether a failure is the agent's fault or an infrastructure problem:

    * ``agent_config_error`` — model name, API key, or auth → task should
      not be retried automatically; the operator must fix config.
    * ``agent_transient_error`` — rate limit, overloaded, timeout →
      safe to auto-retry.
    * ``agent_idle_timeout`` — no stdout output for `idle_timeout_seconds` s.
    * ``agent_hard_timeout`` — total runtime exceeded `hard_timeout_seconds` s.
    * ``agent_err:N`` — all other non-zero exits → genuine task failure.
    """
    if rc == 0:
        return "agent_done", f"{agent_label} exited with 0."
    if rc == RC_IDLE_TIMEOUT:
        return (
            "agent_idle_timeout",
            f"No output for {idle_timeout_seconds}s — killed process group.",
        )
    if rc == RC_HARD_TIMEOUT:
        return (
            "agent_hard_timeout",
            f"Hard timeout after {hard_timeout_seconds}s — killed process group.",
        )

    if result_text_extractor is None:
        result_text_extractor = _claude_result_text
    result_text = result_text_extractor(event_path) or ""

    # ── config errors ──────────────────────────────────────────────
    _config_patterns = [
        r"supported (?:API )?model names? are",
        r"invalid_request_error.*model",
        r"invalid.*api.?key",
        r"api.?key.*invalid",
        r"authentication.*failed",
        r"incorrect.*api.?key",
        r"401.*unauthorized",
        r"403.*forbidden",
    ]
    for pat in _config_patterns:
        if re.search(pat, result_text, re.IGNORECASE):
            return (
                "agent_config_error",
                f"Config error (rc={rc}): {result_text[:300]}",
            )

    # ── transient errors ───────────────────────────────────────────
    _transient_patterns = [
        r"rate.?limit",
        r"overloaded",
        r"529.*overloaded",
        r"timeout",
        r"timed.?out",
        r"connection.*reset",
        r"connection.*refused",
        r"temporary.*failure",
        r"service.*unavailable",
        r"503.*service",
        r"502.*bad.?gateway",
    ]
    for pat in _transient_patterns:
        if re.search(pat, result_text, re.IGNORECASE):
            return (
                "agent_transient_error",
                f"Transient error (rc={rc}): {result_text[:300]}",
            )

    # ── genuine failure ────────────────────────────────────────────
    return (
        f"agent_err:{rc}",
        f"{agent_label} exited with {rc}. {result_text[:300]}",
    )


def _stream_subprocess(
    cmd,
    *,
    cwd,
    env,
    event_path,
    on_line,
    stdin_text=None,
    idle_timeout_seconds=1800,
    hard_timeout_seconds=None,
    orphan_grace_seconds=_ORPHAN_GRACE_SECONDS,
):
    """Run *cmd*, stream stdout line-by-line to *event_path* and *on_line*.

    Uses a reader thread + ``queue.Queue`` so the main loop can detect idle
    hangs, hard-timeout expiry, and orphaned grandchild processes that keep the
    stdout pipe open after the direct child exits.  On any timeout the entire
    process group is killed (``start_new_session=True``).

    Returns the process exit code, ``RC_IDLE_TIMEOUT``, or ``RC_HARD_TIMEOUT``.
    """
    event_path.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
        finally:
            proc.stdin.close()

    # Reader thread: reads lines from proc.stdout, pushes (kind, payload)
    # tuples into a queue.  When the pipe closes it pushes ('eof', None).
    line_queue: queue.Queue = queue.Queue()

    def _reader():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_queue.put(("line", line))
        except (ValueError, OSError):
            pass
        finally:
            line_queue.put(("eof", None))

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    start_time = time.monotonic()
    last_output_at = start_time
    last_heartbeat_at = start_time
    final_rc: int | None = None

    try:
        with event_path.open("w", encoding="utf-8") as sink:
            while True:
                try:
                    kind, payload = line_queue.get(timeout=1.0)
                except queue.Empty:
                    poll_rc = proc.poll()
                    now = time.monotonic()

                    # Hard timeout — total wall clock exceeded.
                    if hard_timeout_seconds and (now - start_time) > hard_timeout_seconds:
                        _kill_process_group(proc)
                        final_rc = RC_HARD_TIMEOUT
                        break

                    # Direct child exited but pipe may be held open by an
                    # orphan grandchild.  Give it a grace period then kill
                    # the whole group.
                    if poll_rc is not None:
                        if (now - last_output_at) > orphan_grace_seconds:
                            _kill_process_group(proc)
                            final_rc = poll_rc
                            break
                        continue

                    # Heartbeat — surface "agent is silent but orchestrator
                    # is still watching" so Dagster UI / post-mortem tooling
                    # don't think the orchestrator itself is hung. Emitted
                    # to BOTH on_line (live UI) and events.ndjson (sink).
                    idle_for = now - last_output_at
                    if idle_for >= _HEARTBEAT_INTERVAL_SECONDS and (now - last_heartbeat_at) >= _HEARTBEAT_INTERVAL_SECONDS:
                        hb = {
                            "type": "orchestrator",
                            "subtype": "heartbeat",
                            "idle_for_seconds": round(idle_for, 1),
                            "idle_timeout_seconds": idle_timeout_seconds,
                            "ts": time.time(),
                        }
                        hb_line = json.dumps(hb)
                        sink.write(hb_line + "\n")
                        sink.flush()
                        if on_line is not None:
                            try:
                                on_line(f"[heartbeat] agent silent {int(idle_for)}s "
                                        f"(timeout at {idle_timeout_seconds}s)")
                            except Exception:  # pragma: no cover
                                pass
                        last_heartbeat_at = now

                    # Process alive but no output for too long.
                    if idle_for > idle_timeout_seconds:
                        _kill_process_group(proc)
                        final_rc = RC_IDLE_TIMEOUT
                        break
                    continue

                if kind == "eof":
                    final_rc = proc.wait()
                    break

                # kind == 'line'
                line = payload
                sink.write(line)
                sink.flush()
                if on_line is not None:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:  # pragma: no cover
                        pass
                last_output_at = time.monotonic()

            # Drain any lines that arrived between the break condition and now
            # (the kill signal causes the reader to flush buffered data).
            while True:
                try:
                    kind, payload = line_queue.get(timeout=0.3)
                except queue.Empty:
                    break
                if kind == "line":
                    sink.write(payload)
                    sink.flush()
                    if on_line is not None:
                        try:
                            on_line(payload.rstrip("\n"))
                        except Exception:  # pragma: no cover
                            pass
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
            if final_rc is None:
                final_rc = RC_IDLE_TIMEOUT

    reader.join(timeout=30)

    if final_rc is None:
        try:
            final_rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            final_rc = RC_IDLE_TIMEOUT

    return final_rc


def _kill_process_group(proc):
    """SIGTERM → 3 s → SIGKILL to the process group.  Reaps via ``poll()``."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    # Give processes a moment to shut down gracefully.
    time.sleep(3)
    if proc.poll() is not None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    time.sleep(2)
    proc.poll()  # reap
