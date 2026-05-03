"""Executor registry."""

import logging
import os

from .noop import NoopExecutor

_logger = logging.getLogger(__name__)

# Codex was retired internally; tasks whose Plane frontmatter still says
# ``agent_hint: codex`` would otherwise spawn a subprocess that either
# fails to find the binary OR hangs waiting on auth — both surface as
# `agent_idle_timeout` and demote the task to Partial. Alias codex →
# claude so the existing backlog runs cleanly while we migrate the
# frontmatter via plane updates. Set DORA_ALLOW_CODEX=1 to opt back in
# (e.g. for testing the codex CLI directly).
_CODEX_RETIRED = os.environ.get("DORA_ALLOW_CODEX", "0") != "1"


def get_executor(name: str):
    if name == "noop":
        return NoopExecutor()
    if name == "codex":
        if _CODEX_RETIRED:
            _logger.warning(
                "executor 'codex' is retired; falling back to 'claude'. "
                "Update Plane frontmatter (agent_hint: claude) to silence this warning. "
                "Set DORA_ALLOW_CODEX=1 to keep using codex."
            )
            from .claude import ClaudeExecutor

            return ClaudeExecutor()
        from .codex import CodexExecutor

        return CodexExecutor()
    if name == "claude":
        from .claude import ClaudeExecutor

        return ClaudeExecutor()
    raise ValueError(f"unsupported executor: {name}")

