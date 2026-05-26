# Acceptance Rigor Floor Design

- **Status**: Proposed
- **Date**: 2026-05-26
- **Owner**: orchestrator platform
- **Scope**: batch audit acceptance-rigor gate, structured `acceptance_checks` schema, generic acceptance runner, loop Done wiring, submit propagation, live-backend serialization
- **Refines**: `docs/specs/2026-05-01-dora-orchestration-design.md`

---

## Problem Statement

The orchestration control plane is sound: one repo → one Plane Project, batch
immutability, hash-frozen approval, packet completeness audit, claim/release
state machine, delivery pipeline, circuit breaker. The audit gate is strong on
*input completeness* (packet sections present, source docs exist) and was
recently tightened further by the source-context submission boundary.

But the loop closes only *structurally*, not *semantically*. The definition of
"Done" in `run_ready_task.py:422-455` is:

```
Done  ⟺  agent exit 0  ∧  verification_commands all pass  ∧  source_evidence.ok
```

`verification_commands` is author-supplied freeform shell with **no rigor
floor**. Doc/audit batches routinely declare a single trivially-satisfiable
command. Empirically, every task in batch `20260516D` (a 7-task system audit)
used only:

```yaml
verification_commands:
  - test -s docs/audit/<file>.md
```

`test -s` passes for any non-empty file. An agent that writes a skeleton
document (headings + TODO placeholders) passes verification, is marked **Done**,
and is auto-merged (`ops.py:132 auto_merge=True`). This is the mechanism by
which framework / hollow output reaches the business repo while dashboards stay
green.

The audit guarantees the *task packet* is not framework. Nothing guarantees the
*delivered output* is not framework. The missing edge is an **output-side
acceptance boundary**.

## Non-goals

- Do not retroactively re-audit or fail already-submitted issues. Submitted
  issues are immutable execution facts.
- Do not introduce an independent reviewer edge into the loop (that is a
  separate, larger direction).
- Do not change L1–L5 `verification_level` semantics — they remain decorative
  for now; differentiated L-level execution is out of scope.
- Do not change the `auto_merge` gate.
- Do not replace `verification_commands` — it remains as a fallback for
  already-submitted issues.

## Core Decisions

| Concern | Decision |
|---|---|
| Floor philosophy | Module-aware *positive* floor: each task must declare a "strong" check appropriate to its module. |
| Assertion carrier | New structured `acceptance_checks` schema in task frontmatter, classifiable by audit. |
| Backward compatibility | New field coexists with `verification_commands`. Loop runs `acceptance_checks` when present, else falls back to `verification_commands`. Rigor floor is enforced at audit for **new batches only**. |
| Done wiring | Reuse the existing verification → outcome path. No new terminal state. |
| Live serialization | Reuse the existing live JSON metadata block: add `acceptance_checks` to `_DORA_METADATA_KEYS`. It round-trips as native JSON exactly like `source_queries`. |

## Schema: `acceptance_checks`

A task's frontmatter may declare `acceptance_checks`, a list of check objects.
It coexists with `verification_commands`.

```yaml
acceptance_checks:
  - kind: contains_sections
    path: docs/audit/2026-05-16-path-and-seams.md
    headings: ["## A.", "## B. Integration seam catalog", "## C."]
  - kind: min_matches
    path: docs/audit/2026-05-16-path-and-seams.md
    pattern: '^\| .+ \| .+ \|'   # markdown table rows
    min: 10
  - kind: shell
    cmd: "go test ./internal/storage/sqlite/... -race -run Seq"
```

### Check kinds

| kind | params | category |
|---|---|---|
| `file_exists` | `path` | existence (weak) |
| `file_min_bytes` | `path`, `min` | existence (weak) |
| `contains_sections` | `path`, `headings: list[str]` | **content (strong)** |
| `min_matches` | `path`, `pattern`, `min` | **content (strong)** |
| `shell` | `cmd` | **behavioral (strong\*)** |

\* A `shell` check whose `cmd` matches the trivial-command deny-list is demoted
to *existence* (weak). Deny-list (whole-command, after trimming): `test -s`,
`test -f`, `test -e`, `test` (bare), `true`, `:`, and a bare `ls`/`cat`/`echo`
invocation. The deny-list matches the leading command token plus the `test -X`
forms; anything else (including `go test`, `grep -E '<pattern>' file`) is
treated as behavioral-strong.

### Semantics of each kind

- `file_exists`: path exists and is a regular file.
- `file_min_bytes`: path exists and byte size ≥ `min`.
- `contains_sections`: file is readable and contains every string in `headings`
  as a line (after strip). All headings must be present.
- `min_matches`: file is readable and the count of lines matching `pattern`
  (Python `re`, `re.MULTILINE`) is ≥ `min`.
- `shell`: run `cmd` via shell in `repo_root`, expect exit 0 (reuses the
  existing subprocess + 120s timeout + `extra_env` behavior).

## Module-Aware Rigor Floor

Audit enforces, for each task in a **new** batch, at least one "strong" check
matching its module:

| module | floor requirement |
|---|---|
| `implementation`, `operations` | ≥1 `shell` check that is **not** demoted by the deny-list (ideally build + test). |
| `product`, `architecture`, `planning`, `verification` | ≥1 **content** check (`contains_sections` or `min_matches`). These modules produce documents whose correctness is structural; a passing shell alone is insufficient. |
| `governance` | ≥1 content check **or** ≥1 non-trivial `shell` check. |

A task that satisfies its module floor passes. A task that declares only weak
(existence-category, or deny-listed shell) checks — or declares no
`acceptance_checks` at all — fails the floor.

Failure produces an audit finding `code="acceptance_rigor"` and the batch audit
result is `FAIL` (cannot approve or submit). Batch `20260516D`'s all-`test -s`
shape is rejected.

## Components

### `orchestrator/acceptance.py` (new)

Generic runner, output shape identical to `_run_verification_commands` so it
feeds the existing outcome logic unchanged.

```python
def run_acceptance_checks(
    checks: list[dict],
    repo_root: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict:
    # → {"pass": bool, "skipped": bool, "results": [{"kind", "ok", "detail"}, ...]}
```

- Empty/absent `checks` → `{"pass": True, "skipped": True, "results": []}`
  (caller decides fallback; "skipped" is not "passed work").
- Each check dispatched by `kind`. Unknown kind or malformed params at loop time
  → that check `ok=False` (conservative), never raises.
- `shell` reuses the existing subprocess+timeout helper (extracted/shared with
  `_run_verification_commands`).

It also exposes classification helpers used by audit:

```python
def classify_check(check: dict) -> str        # "existence" | "content" | "behavioral"
def is_trivial_shell(cmd: str) -> bool         # deny-list match
def module_floor_satisfied(module: str, checks: list[dict]) -> bool
```

### `orchestrator/batch_audit.py` (modify)

After existing source/preflight validation, for each task:

1. Parse `acceptance_checks` (frontmatter). If present, validate structure:
   each item has a known `kind` and required params; `min_matches.pattern`
   compiles (`re.compile`). Invalid structure → `acceptance_rigor` FAIL.
2. Evaluate `module_floor_satisfied(task.module, checks)`. If not satisfied →
   `AuditFinding(code="acceptance_rigor", message=..., path=task_path)` and the
   batch audit fails.

### `orchestrator/batch_submit.py` (modify)

In the `upsert_issue` payload (near `verification_commands`, ~line 118) add
`acceptance_checks` sourced from task metadata, alongside the existing fields.
It is a native list of dicts; the live backend round-trips it via its existing
JSON metadata block (see below).

### `orchestrator/run_ready_task.py` (modify)

At the verification step (`~402-407`):

```python
acceptance_checks = _issue_acceptance_checks(claimed)   # parses dict or *_json
if acceptance_checks:
    verification = run_acceptance_checks(acceptance_checks, repo_root, extra_env=...)
else:
    verification = _run_verification_commands(
        list(claimed.get("verification_commands") or []), repo_root, extra_env=...)
```

`verification["pass"]` flows into the existing outcome logic (`422-455`)
unchanged:

- `agent_done` ∧ pass → `agent_done` → **Done**
- `agent_done` ∧ ¬pass → `agent_unverified` → **Partial** (retry ≤ 3, then
  Needs Input)

No new terminal state. Hollow output now fails content assertions and lands as
Partial instead of Done, reusing the existing retry/circuit-breaker machinery.

`_issue_acceptance_checks` reads the structured list from the issue dict
(memory/local) or parses `acceptance_checks_json` (live).

### Plane backend round-trip

- **memory / local**: store/return `acceptance_checks` as a native list.
- **live** (`plane_live.py`): reuse the existing JSON metadata block. The
  backend already serializes nested structures (`source_docs`, `source_tables`,
  `source_queries`) into a `<!-- dora:metadata ... -->` block via
  `json.dumps`/`json.loads` (`_append_metadata_block`/`_extract_metadata_block`,
  keyed by `_DORA_METADATA_KEYS`). Add `"acceptance_checks"` to
  `_DORA_METADATA_KEYS`; it round-trips as native JSON exactly like
  `source_queries`. No new serialize/parse helpers, no special scalar.

## Data Flow

```
tasks/*.md  acceptance_checks
      │
      ▼
batch_audit ── module_floor_satisfied? ──no──► FAIL (acceptance_rigor)
      │ yes
      ▼
batch_submit ── upsert_issue(acceptance_checks | acceptance_checks_json)
      │
      ▼
Plane issue
      │
      ▼
run_ready_batch_task ── acceptance_checks present? ──► run_acceptance_checks
      │                                       else  ──► _run_verification_commands (fallback)
      ▼
verification["pass"] ─► existing outcome logic ─► Done | Partial(retry) | Needs Input
```

## Error Handling

| Failure | Behavior |
|---|---|
| `contains_sections`/`min_matches` path missing or unreadable | check `ok=False`, counts as fail → Partial. No exception. |
| Malformed `acceptance_checks` (bad JSON / unknown kind) at audit | `acceptance_rigor` FAIL — author must fix. |
| Malformed `acceptance_checks` at loop time (dirty data past audit) | treat as `pass=False`, do not crash. |
| Illegal `min_matches.pattern` | audit compiles patterns; illegal → FAIL early. |
| Empty `acceptance_checks: []` | treated as "not declared" → `verification_commands` fallback (not "zero checks passed"). |

## Testing

`unittest`, existing tree style (`dataclass(frozen=True)`, `pathlib.Path`, no
pytest).

- `tests/test_acceptance.py` (new): each kind pass/fail; deny-list demotion;
  illegal regex; missing file; empty list → skipped.
- `tests/test_batch_audit.py`: `verification` module with only `file_exists` →
  FAIL; with `contains_sections` → PASS; `implementation` with only trivial
  `shell` → FAIL; with `go test` shell → PASS.
- `tests/test_batch_submit.py`: `acceptance_checks` propagated into issue;
  live `acceptance_checks_json` serialization.
- `tests/test_run_ready_batch_task.py`: issue with `acceptance_checks` runs the
  new runner; without it falls back to `verification_commands`; content
  assertion failure → `agent_unverified` → Partial.
- `tests/test_live_plane.py`: `acceptance_checks_json` round-trip.

## Acceptance Criteria

1. A new batch whose task declares only `test -s`/existence checks fails audit
   with `acceptance_rigor`.
2. A new `verification`-module task with a `contains_sections` or `min_matches`
   check passes the floor.
3. A new `implementation`-module task with a non-trivial `shell` check passes;
   with only a deny-listed shell fails.
4. At loop time, an issue with `acceptance_checks` is gated by
   `run_acceptance_checks`; an issue without it falls back to
   `verification_commands`.
5. A task whose content assertion fails after `agent_done` becomes Partial (not
   Done) and reuses the existing retry path.
6. `acceptance_checks` round-trips through the live backend via the existing
   JSON metadata block (`_DORA_METADATA_KEYS`).
7. Already-submitted issues are untouched; no retroactive failure.
