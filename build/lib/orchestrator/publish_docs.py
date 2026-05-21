"""`orchestrator publish-docs` — one-way push of `docs/dora/**.md` to BookStack.

Pipeline per file:

    read -> is_english? translate_to_chinese (writes back to disk) ->
    demote_headings (h1->h3, h2->h4) -> lint (must be h3+) ->
    upsert_page (PUT existing, POST new).

Each pushed page gets a managed-by HTML comment appended so ``--prune``
can identify orphans without ever touching user-created pages.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bookstack_live import BookStackClient, BookStackSettings
from .dagster_defs.loader import load_project_configs
from .dagster_defs.project_config import ProjectConfig
from .translate import (
    TranslationError,
    demote_headings,
    find_h1_or_h2,
    is_english,
    translate_to_chinese,
)


# ── target mapping ──────────────────────────────────────────────────


@dataclass(frozen=True)
class PageTarget:
    book_slug: str
    book_display: str
    page_slug: str
    page_display: str


# Paths are relative to the project's docs/ directory.
# The 13 scaffolded dora/* files keep curated Chinese display names so the
# generated structure stays prescriptive even after we widen the scan.
PAGE_TARGETS: dict[str, PageTarget] = {
    "dora/product/vision.md":          PageTarget("product",      "产品", "vision",         "愿景"),
    "dora/product/requirements.md":    PageTarget("product",      "产品", "requirements",   "需求"),
    "dora/product/glossary.md":        PageTarget("product",      "产品", "glossary",       "词汇表"),
    "dora/architecture/overview.md":   PageTarget("architecture", "架构", "overview",       "概述"),
    "dora/architecture/decisions.md":  PageTarget("architecture", "架构", "decisions",      "决策索引"),
    "dora/planning/roadmap.md":        PageTarget("planning",     "规划", "roadmap",        "路线图"),
    "dora/planning/implementation.md": PageTarget("planning",     "规划", "implementation", "实现计划"),
    "dora/quality/acceptance.md":      PageTarget("quality",      "质量", "acceptance",     "验收策略"),
    "dora/quality/risk-register.md":   PageTarget("quality",      "质量", "risk-register",  "风险登记"),
    "dora/operations/runbook.md":      PageTarget("operations",   "运维", "runbook",        "运维 Runbook"),
    "dora/governance/policy.md":       PageTarget("governance",   "治理", "policy",         "治理政策"),
    "dora/index.md":                   PageTarget("overview",     "概览", "index",          "索引"),
    "dora/modules.md":                 PageTarget("overview",     "概览", "modules",        "模块说明"),
}


# Display name for top-level directories under docs/ (everything OTHER than dora/).
# Unmapped top-level dirs fall back to their literal name.
TOP_LEVEL_DISPLAY: dict[str, str] = {
    "audit":         "审计",
    "conventions":   "工程约定",
    "decisions":     "架构决策",
    "guides":        "操作指南",
    "observability": "可观测性",
    "qa":            "质量保证",
    "runbook":       "运维手册",
    "summaries":     "工作汇总",
}


# Files directly under docs/ go into one shared "概览" book.
ROOT_BOOK_SLUG = "_root_overview"
ROOT_BOOK_DISPLAY = "项目概览"


def resolve_target(rel_path: str) -> PageTarget:
    """Map a docs/-relative path to BookStack coordinates.

    Resolution order:
    1. Exact match in :data:`PAGE_TARGETS` (curated dora scaffold names).
    2. ``docs/<page>.md`` (no subdir) → root overview book.
    3. ``docs/<top>/.../<page>.md`` → Book named after *top* (Chinese if mapped),
       Page display = ``<sub>/.../<stem>`` (flattened breadcrumb for deeper paths).

    Internal *book_slug* is our stable Python-side identity used to deduplicate
    book lookups within one publish run; BookStack auto-generates its own slug
    for any non-ASCII display name.
    """
    if rel_path in PAGE_TARGETS:
        return PAGE_TARGETS[rel_path]

    parts = Path(rel_path).parts

    # docs/<page>.md
    if len(parts) == 1:
        stem = Path(parts[0]).stem
        return PageTarget(
            book_slug=ROOT_BOOK_SLUG,
            book_display=ROOT_BOOK_DISPLAY,
            page_slug=stem,
            page_display=stem,
        )

    top = parts[0]
    book_display = TOP_LEVEL_DISPLAY.get(top, top)
    stem = Path(parts[-1]).stem

    # docs/<top>/<page>.md
    if len(parts) == 2:
        return PageTarget(
            book_slug=top, book_display=book_display,
            page_slug=stem, page_display=stem,
        )

    # docs/<top>/<sub>/.../<page>.md — flatten the in-between dirs into the
    # page display so the book stays one level deep. (BookStack chapters
    # would be nicer; deferred until we need them.)
    flat_name = "/".join(list(parts[1:-1]) + [stem])
    return PageTarget(
        book_slug=top, book_display=book_display,
        page_slug=stem, page_display=flat_name,
    )


# ── managed-by marker ───────────────────────────────────────────────

_MARKER_RE = re.compile(
    r"<!--\s*managed-by:\s*orchestrator publish-docs\s*"
    r"·\s*project:\s*(?P<project>[^\s·]+)\s*"
    r"·\s*src:\s*(?P<src>[^\s·]+)"
    r".*?-->",
    re.DOTALL,
)


def build_marker(project_slug: str, rel_path: str, git_sha: str) -> str:
    ts = _dt.datetime.now(_dt.timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    return (
        f"<!-- managed-by: orchestrator publish-docs"
        f" · project: {project_slug}"
        f" · src: {rel_path}"
        f" · git_sha: {git_sha}"
        f" · ts: {ts} -->"
    )


def strip_existing_marker(text: str) -> str:
    return _MARKER_RE.sub("", text).rstrip() + "\n"


def parse_marker(body: str) -> dict[str, str] | None:
    m = _MARKER_RE.search(body or "")
    return m.groupdict() if m else None


# ── cross-doc link rewriting ────────────────────────────────────────


# `[anchor text](target)` where target is *not* an absolute URL / mailto / pure fragment.
# Greedy on the text portion is fine; markdown forbids nested `]` without escapes.
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")


def rewrite_cross_doc_links(
    text: str,
    *,
    current_rel: str,
    docs_root: Path,
    url_map: dict[str, str],
) -> str:
    """Replace ``[label](./other.md)`` with ``[label](<BookStack URL>)``.

    *current_rel* is the page's path relative to ``docs_root``; *url_map* maps
    rel-paths to their freshly-published BookStack URLs. Links to ``.md`` files
    that aren't in the map (e.g. excluded by EXCLUDED_PREFIXES, or external)
    are left untouched. Image links (``![alt](...)``) are also left alone —
    BookStack image hosting is a separate concern not implemented here.
    Code fences are respected so ``` ``` blocks with markdown samples don't
    get rewritten.
    """
    lines = text.splitlines(keepends=True)
    in_fence = False
    fence_marker: str | None = None
    out: list[str] = []
    current_parent = (docs_root / current_rel).parent

    def _resolve(href: str) -> str | None:
        # Strip optional title: `path "Title"` or `path 'Title'`.
        bare = href.split()[0] if href.split() else href
        # Strip fragment for path resolution.
        anchor = ""
        if "#" in bare:
            bare, anchor = bare.split("#", 1)
        if not bare.endswith(".md"):
            return None
        # Resolve relative to current page's parent.
        try:
            target = (current_parent / bare).resolve()
            key = str(target.relative_to(docs_root))
        except (ValueError, OSError):
            return None
        new_url = url_map.get(key)
        if not new_url:
            return None
        return f"{new_url}#{anchor}" if anchor else new_url

    def _rewrite_match(m: re.Match) -> str:
        full = m.group(0)
        # Skip image syntax: the `!` prefix is OUTSIDE the bracket so re.Match
        # doesn't see it. Check the byte just before group(0).
        start = m.start()
        if start > 0 and m.string[start - 1] == "!":
            return full
        href = m.group(2).strip()
        if href.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            return full
        new_url = _resolve(href)
        if new_url is None:
            return full
        return f"[{m.group(1)}]({new_url})"

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            ticks = stripped[: len(stripped) - len(stripped.lstrip("`"))]
            if not in_fence:
                in_fence = True
                fence_marker = ticks
            elif fence_marker and stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = None
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        out.append(_LINK_RE.sub(_rewrite_match, line))
    return "".join(out)


def short_git_sha(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or "nogit"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "nogit"


# ── core flow ───────────────────────────────────────────────────────


def _project_config(slug: str) -> ProjectConfig:
    cfgs = {c.slug: c for c in load_project_configs()}
    if slug not in cfgs:
        raise SystemExit(
            f"project '{slug}' not found in ~/.dora/orchestrator/projects/. "
            f"Run `orchestrator scaffold` first."
        )
    return cfgs[slug]


# Path prefixes to skip when walking docs/. Compared as a tuple-of-path-parts
# prefix against the rel-to-docs path so substring matches (e.g. an `audit`
# dir under `dora/`) don't accidentally exclude unrelated trees.
_EXCLUDED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("dora", "batches"),   # Plane batch artifacts; not design docs.
    ("superpowers",),       # Tooling specs / metadata; not project docs.
    ("summaries",),         # Auto-generated per-task summaries; tracked in git, not for BookStack.
)


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    for prefix in _EXCLUDED_PREFIXES:
        if len(rel_parts) >= len(prefix) and rel_parts[: len(prefix)] == prefix:
            return True
    return False


def _scan_files(docs_root: Path, files_glob: str | None) -> list[Path]:
    """Walk ``docs/`` for project-readable markdown, recursively.

    Skips :data:`_EXCLUDED_PREFIXES` — currently ``docs/dora/batches/**``
    (Plane batch artifacts: batch.md / audit-report.md / submit-preview.md
    / per-task Issue Packets) and ``docs/superpowers/**`` (tooling specs).
    """
    if not docs_root.exists():
        return []
    candidates = sorted(docs_root.rglob(files_glob or "*.md"))
    return [
        p for p in candidates
        if not _is_excluded(p.relative_to(docs_root).parts)
    ]


def publish_docs(
    project_slug: str,
    *,
    dry_run: bool = False,
    no_publish: bool = False,
    no_translate: bool = False,
    files_glob: str | None = None,
    prune: bool = False,
    yes: bool = False,
) -> dict[str, Any]:
    cfg = _project_config(project_slug)
    docs_root = cfg.repo_root / "docs"
    files = _scan_files(docs_root, files_glob)
    if not files:
        print(f"no .md files under {docs_root}")
        return {"published": [], "translated": [], "skipped": [], "pruned": []}

    sha = short_git_sha(cfg.repo_root)
    client = BookStackClient(BookStackSettings.from_env()) if not dry_run and not no_publish else None
    shelf = _ensure_shelf(client, cfg) if client else None

    published: list[dict[str, str]] = []
    translated: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []   # files that errored mid-run; non-fatal
    seen_books: dict[str, dict[str, Any]] = {}  # book_slug -> book dict
    url_map: dict[str, str] = {}                 # rel_str -> BookStack URL
    # Pass-1 records keep enough state to re-PUT in pass 2 without re-reading or
    # re-translating files. body_no_marker is the demoted Chinese content
    # without the managed-by marker appended.
    pass1_records: list[dict[str, Any]] = []

    # ── Pass 1: translate / demote / lint / upsert ─────────────────────
    for f in files:
        rel = f.relative_to(cfg.repo_root)
        rel_str = str(f.relative_to(docs_root))
        target = resolve_target(rel_str)

        text = f.read_text(encoding="utf-8")
        # Strip any prior marker before re-evaluating language; otherwise the
        # marker's own English keywords (`managed-by`, `orchestrator`, ...)
        # could keep an otherwise-Chinese file marked as English.
        text_for_check = strip_existing_marker(text)

        if is_english(text_for_check):
            if no_translate:
                print(f"ABORT: {rel} is English and --no-translate set", file=sys.stderr)
                raise SystemExit(2)
            print(f"translating {rel} ...", flush=True)
            try:
                translated_text = translate_to_chinese(text_for_check)
            except TranslationError as exc:
                # Per-file translation failure (codex network blip, transient
                # auth issue, etc.) — log, record, continue. The file stays
                # English on disk so the next publish run re-tries it.
                msg = str(exc).splitlines()[0][:200]
                print(f"  ⚠ skip {rel}: translation failed: {msg}", file=sys.stderr)
                failed.append({"file": str(rel), "stage": "translate", "error": msg})
                continue
            if translated_text is None:
                # Codex decided no translation needed — source was bilingual
                # / Chinese-enough that is_english was a false positive.
                # Continue with original text; nothing to write back.
                print(f"  ({rel}: already Chinese-enough, skipping translation)")
            else:
                text = translated_text
                translated.append(str(rel))
                if not dry_run:
                    f.write_text(text, encoding="utf-8")

        body = demote_headings(strip_existing_marker(text))
        violations = find_h1_or_h2(body)
        if violations:
            line, content = violations[0]
            print(
                f"ABORT: {rel}:{line} has h1/h2 after demote: {content!r} "
                f"(this means the heading was inside ambiguous markdown "
                f"or codex injected it post-translation)",
                file=sys.stderr,
            )
            raise SystemExit(4)

        marker = build_marker(project_slug, rel_str, sha)
        page_body = body.rstrip() + "\n\n" + marker + "\n"

        if dry_run or no_publish or client is None:
            verb = "[dry-run]" if dry_run else "[no-publish]"
            print(
                f"{verb} would push {rel} -> shelf={project_slug} "
                f"book={target.book_slug} page={target.page_slug}"
            )
            skipped.append(str(rel))
            continue

        book = seen_books.get(target.book_slug)
        if book is None:
            assert shelf is not None
            book = client.upsert_book(
                shelf_id=int(shelf["id"]),
                name=target.book_display,
            )
            seen_books[target.book_slug] = book

        page = client.upsert_page(
            book_id=int(book["id"]),
            name=target.page_display,
            markdown=page_body,
        )
        url = _page_url(client.settings.base_url, book, page)
        published.append({"file": str(rel), "url": url})
        url_map[rel_str] = url
        pass1_records.append({
            "rel_str": rel_str,
            "rel_display": str(rel),
            "body_no_marker": body,
            "book_id": int(book["id"]),
            "page_id": int(page["id"]),
            "page_name": target.page_display,
        })
        print(f"  {rel} -> {url}")

    # ── Pass 2: rewrite cross-doc links; PUT only if body changed ─────
    link_rewrites: list[str] = []
    if client is not None and pass1_records:
        print(f"\nlink rewrite pass over {len(pass1_records)} page(s)...")
        for rec in pass1_records:
            rewritten = rewrite_cross_doc_links(
                rec["body_no_marker"],
                current_rel=rec["rel_str"],
                docs_root=docs_root,
                url_map=url_map,
            )
            if rewritten == rec["body_no_marker"]:
                continue
            marker = build_marker(project_slug, rec["rel_str"], sha)
            final_body = rewritten.rstrip() + "\n\n" + marker + "\n"
            client.upsert_page(
                book_id=rec["book_id"],
                name=rec["page_name"],
                markdown=final_body,
            )
            link_rewrites.append(rec["rel_display"])
            print(f"  ↳ rewrote links in {rec['rel_display']}")
        if not link_rewrites:
            print("  (no cross-doc links found)")

    pruned: list[str] = []
    if prune and client is not None and shelf is not None:
        alive_srcs = {str(f.relative_to(docs_root)) for f in files}
        pruned = _prune_orphans(
            client=client,
            shelf=shelf,
            project_slug=project_slug,
            alive_srcs=alive_srcs,
            yes=yes,
        )

    return {
        "published": published,
        "translated": translated,
        "skipped": skipped,
        "failed": failed,
        "link_rewrites": link_rewrites,
        "pruned": pruned,
    }


def _ensure_shelf(client: BookStackClient, cfg: ProjectConfig) -> dict[str, Any]:
    return client.upsert_shelf(
        name=cfg.title,
        slug=cfg.slug,
        description=f"Design docs synced from {cfg.repo_root}/docs/dora (project={cfg.slug})",
    )


def _page_url(base_url: str, book: dict[str, Any], page: dict[str, Any]) -> str:
    """Derive a human-visitable URL from book + page response payloads.

    BookStack auto-generates page slugs from name when name is non-ASCII;
    that slug is in ``page['slug']``. The book slug comes from the
    upsert_book response so we don't depend on the page payload echoing it.
    """
    book_slug = book.get("slug") or ""
    page_slug = page.get("slug") or ""
    return f"{base_url}/books/{book_slug}/page/{page_slug}"


def _prune_orphans(
    *,
    client: BookStackClient,
    shelf: dict[str, Any],
    project_slug: str,
    alive_srcs: set[str],
    yes: bool,
) -> list[str]:
    """Walk all pages in the shelf's books, surface managed-by orphans.

    A page is an orphan iff:
      - its body contains our managed-by marker, AND
      - the marker's ``project`` matches *project_slug*, AND
      - the marker's ``src`` (relative-to-docs_root path) is not in
        *alive_srcs*.

    Pages without the marker (user-created) are never touched. BookStack's
    auto-generated slugs are NOT used for matching — non-ASCII page/book
    names get hashed slugs, so the marker's ``src`` is the only stable
    identity we control.
    """
    shelf_books = shelf.get("books") or []
    if not shelf_books:
        shelf = client.get_shelf(int(shelf["id"]))
        shelf_books = shelf.get("books") or []

    orphans: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for book_summary in shelf_books:
        book_id = int(book_summary["id"])
        for page_summary in client.list_pages_in_book(book_id):
            page = client.get_page(int(page_summary["id"]))
            marker = parse_marker(page.get("markdown") or page.get("html") or "")
            if not marker:
                continue
            if marker.get("project") != project_slug:
                continue
            src = marker.get("src") or ""
            if src in alive_srcs:
                continue
            orphans.append((book_summary, page, src))

    if not orphans:
        print("no orphan pages")
        return []

    print(f"\nfound {len(orphans)} orphan page(s):")
    for book_summary, page, src in orphans:
        print(
            f"  - {book_summary.get('name')} / {page.get('name')}  "
            f"(src={src!r}, page_id={page['id']})"
        )

    if not yes:
        try:
            ans = input("delete all? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("prune aborted")
            return []

    deleted: list[str] = []
    for book_summary, page, src in orphans:
        client.delete_page(int(page["id"]))
        label = f"{book_summary.get('name')}/{page.get('name')} ({src})"
        print(f"  deleted {label}")
        deleted.append(src)
    return deleted


# ── CLI ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish docs/dora/**.md to BookStack as Shelf > Book > Page.",
    )
    parser.add_argument("--project-slug", required=True)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only: don't write files, don't push to BookStack.",
    )
    parser.add_argument(
        "--no-publish", action="store_true",
        help="Translate + write files but skip BookStack upsert (review-first flow).",
    )
    parser.add_argument(
        "--no-translate", action="store_true",
        help="Refuse to translate; abort if any file is detected as English.",
    )
    parser.add_argument(
        "--files", default=None, metavar="GLOB",
        help="Restrict to files matching this glob under docs/dora/ (e.g. product/*.md).",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="After publish, offer to delete managed-by pages whose source files are gone.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="With --prune: skip interactive y/N confirmation.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable summary at end.",
    )
    args = parser.parse_args(argv)

    result = publish_docs(
        args.project_slug,
        dry_run=args.dry_run,
        no_publish=args.no_publish,
        no_translate=args.no_translate,
        files_glob=args.files,
        prune=args.prune,
        yes=args.yes,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"\nsummary: published={len(result['published'])} "
            f"translated={len(result['translated'])} "
            f"link_rewrites={len(result.get('link_rewrites', []))} "
            f"skipped={len(result['skipped'])} "
            f"failed={len(result.get('failed', []))} "
            f"pruned={len(result['pruned'])}"
        )
        if result.get("failed"):
            print("\nfailures (re-run publish-docs to retry):")
            for f in result["failed"]:
                print(f"  - {f['file']} ({f['stage']}): {f['error']}")
    # Non-zero exit if any file failed, so CI / wrappers notice.
    return 5 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
