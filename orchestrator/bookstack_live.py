"""Live BookStack backend for ``orchestrator publish-docs``.

Single-direction upsert (Shelf > Book > Page) keyed by slug. Mirrors the
style of :mod:`orchestrator.plane_live` — stdlib only (urllib + json),
3-layer env merge for credentials, throttle + 429 backoff.

Auth: ``Authorization: Token <BOOKSTACK_TOKEN_ID>:<BOOKSTACK_TOKEN_SECRET>``.

Endpoints used (BookStack REST v1):
    GET  /api/shelves?filter[slug]=...
    POST /api/shelves
    PUT  /api/shelves/{id}
    GET  /api/books?filter[slug]=...
    POST /api/books
    PUT  /api/books/{id}
    GET  /api/pages?filter[book_id]=...
    POST /api/pages
    PUT  /api/pages/{id}
    DELETE /api/pages/{id}
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BookStackSettings:
    base_url: str
    token_id: str
    token_secret: str
    throttle: float = 0.3

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "BookStackSettings":
        env = _load_bookstack_env(environ)
        missing = [
            key
            for key in ("BOOKSTACK_API_URL", "BOOKSTACK_TOKEN_ID", "BOOKSTACK_TOKEN_SECRET")
            if not env.get(key)
        ]
        if missing:
            raise ValueError(
                f"missing BookStack env var(s): {', '.join(missing)}. "
                f"Generate a Token ID + Secret in BookStack user settings, "
                f"then set them in ~/.dora/bookstack.env."
            )
        return cls(
            base_url=env["BOOKSTACK_API_URL"].rstrip("/"),
            token_id=env["BOOKSTACK_TOKEN_ID"],
            token_secret=env["BOOKSTACK_TOKEN_SECRET"],
            throttle=float(env.get("BOOKSTACK_THROTTLE_SECONDS", "0.3")),
        )


class BookStackClient:
    """BookStack REST client. Throttled, retries on 429."""

    def __init__(self, settings: BookStackSettings):
        self.settings = settings
        self._last_call = 0.0

    # ── shelf ────────────────────────────────────────────────────────

    def upsert_shelf(self, name: str, slug: str, description: str = "") -> dict[str, Any]:
        """Upsert a project shelf.

        BookStack installations may ignore submitted shelf slugs and generate
        one from the display name. In that case the stable lookup key becomes
        the managed description marker ``project=<slug>`` plus the shelf name.
        """
        existing = self._find_by_slug("/api/shelves", slug)
        managed = self._find_managed_shelves(name=name, project_slug=slug)
        if not existing and managed:
            existing = managed[0]
        duplicate_ids = {int(existing["id"])} if existing else set()
        duplicates = [
            row for row in managed
            if int(row.get("id", 0)) not in duplicate_ids
        ]
        payload = {"name": name, "description": description, "slug": slug}
        if existing:
            if duplicates:
                updated = self._merge_duplicate_shelves(existing, duplicates, payload)
            else:
                updated = self._req("PUT", f"/api/shelves/{existing['id']}", payload) or existing
            return updated
        return self._req("POST", "/api/shelves", payload)

    def get_shelf(self, shelf_id: int) -> dict[str, Any]:
        return self._req("GET", f"/api/shelves/{shelf_id}")

    def _attach_book_to_shelf(self, shelf_id: int, book_id: int) -> None:
        """Idempotent attach: read current books on shelf, add if missing, PUT."""
        shelf = self.get_shelf(shelf_id)
        current_ids = [int(b["id"]) for b in (shelf.get("books") or [])]
        if int(book_id) in current_ids:
            return
        current_ids.append(int(book_id))
        self._req("PUT", f"/api/shelves/{shelf_id}", {"books": current_ids})

    # ── book ─────────────────────────────────────────────────────────

    def upsert_book(
        self, *, shelf_id: int, name: str, description: str = ""
    ) -> dict[str, Any]:
        """Find-or-create a book by *name* scoped to *shelf_id*'s ``books[]``.

        BookStack silently ignores submitted slugs whose names contain non-ASCII
        characters (Chinese book names get auto-hashed slugs), so slug is NOT a
        stable lookup key. Scoping by shelf disambiguates same-named books
        across projects.
        """
        shelf = self.get_shelf(shelf_id)
        matches = [
            summary for summary in shelf.get("books") or []
            if summary.get("name") == name
        ]
        if matches:
            canonical = sorted(matches, key=lambda row: int(row["id"]))[0]
            duplicates = [
                row for row in matches
                if int(row["id"]) != int(canonical["id"])
            ]
            if duplicates:
                self._merge_duplicate_books(
                    shelf=shelf,
                    canonical=canonical,
                    duplicates=duplicates,
                )
            return self._req(
                "PUT",
                f"/api/books/{canonical['id']}",
                {"name": name, "description": description},
            ) or canonical
        created = self._req(
            "POST", "/api/books", {"name": name, "description": description}
        )
        self._attach_book_to_shelf(shelf_id, int(created["id"]))
        return created

    # ── page ─────────────────────────────────────────────────────────

    def upsert_page(
        self, *, book_id: int, name: str, markdown: str
    ) -> dict[str, Any]:
        """Find-or-create a page by *name* within *book_id*.

        Same caveat as books: page slug is BookStack-generated for non-ASCII
        names. We look up by ``(book_id, name)`` and the publish-docs flow
        guarantees one page per (book, display-name) tuple.
        """
        existing = self._find_page_by_name(book_id, name)
        payload = {
            "book_id": int(book_id),
            "name": name,
            "markdown": markdown,
        }
        if existing:
            return self._req("PUT", f"/api/pages/{existing['id']}", payload) or existing
        return self._req("POST", "/api/pages", payload)

    def list_pages_in_book(self, book_id: int) -> list[dict[str, Any]]:
        return self._paginate(
            "/api/pages", {"filter[book_id]": str(book_id), "count": "200"}
        )

    def get_page(self, page_id: int) -> dict[str, Any]:
        """Fetch a single page with full body (list_pages_in_book omits markdown)."""
        return self._req("GET", f"/api/pages/{page_id}")

    def delete_page(self, page_id: int) -> None:
        self._req("DELETE", f"/api/pages/{page_id}", ok_statuses=frozenset({204, 200}))

    def delete_book(self, book_id: int) -> None:
        self._req("DELETE", f"/api/books/{book_id}", ok_statuses=frozenset({204, 200}))

    # ── helpers ──────────────────────────────────────────────────────

    def _find_by_slug(self, path: str, slug: str) -> dict[str, Any] | None:
        rows = self._paginate(path, {"filter[slug]": slug, "count": "10"})
        for row in rows:
            if row.get("slug") == slug:
                return row
        return None

    def _find_managed_shelves(self, *, name: str, project_slug: str) -> list[dict[str, Any]]:
        rows = self._paginate("/api/shelves", {"query": name, "count": "100"})
        marker = f"project={project_slug}"
        matches = [
            row for row in rows
            if row.get("name") == name and marker in str(row.get("description") or "")
        ]
        return sorted(matches, key=lambda row: int(row.get("id", 0)))

    def _merge_duplicate_shelves(
        self,
        canonical: dict[str, Any],
        duplicates: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        canonical_id = int(canonical["id"])
        book_ids: list[int] = []
        for shelf_id in [canonical_id] + [int(row["id"]) for row in duplicates]:
            shelf = self.get_shelf(shelf_id)
            for book in shelf.get("books") or []:
                book_id = int(book["id"])
                if book_id not in book_ids:
                    book_ids.append(book_id)

        update_payload = dict(payload)
        if book_ids:
            update_payload["books"] = book_ids
        updated = self._req("PUT", f"/api/shelves/{canonical_id}", update_payload) or canonical
        for row in duplicates:
            self._req(
                "DELETE",
                f"/api/shelves/{int(row['id'])}",
                ok_statuses=frozenset({200, 202, 204}),
            )
        return updated

    def _merge_duplicate_books(
        self,
        *,
        shelf: dict[str, Any],
        canonical: dict[str, Any],
        duplicates: list[dict[str, Any]],
    ) -> None:
        canonical_id = int(canonical["id"])
        for duplicate in duplicates:
            duplicate_id = int(duplicate["id"])
            for page_summary in self.list_pages_in_book(duplicate_id):
                page = self.get_page(int(page_summary["id"]))
                moved = self.upsert_page(
                    book_id=canonical_id,
                    name=str(page["name"]),
                    markdown=str(page.get("markdown") or ""),
                )
                if int(moved["id"]) != int(page["id"]):
                    self.delete_page(int(page["id"]))

        duplicate_ids = {int(row["id"]) for row in duplicates}
        remaining_book_ids = [
            int(row["id"]) for row in shelf.get("books") or []
            if int(row["id"]) not in duplicate_ids
        ]
        self._req("PUT", f"/api/shelves/{int(shelf['id'])}", {"books": remaining_book_ids})
        for duplicate in duplicates:
            self.delete_book(int(duplicate["id"]))

    def _find_page_by_name(self, book_id: int, name: str) -> dict[str, Any] | None:
        for row in self.list_pages_in_book(book_id):
            if row.get("name") == name:
                return row
        return None

    def _paginate(self, path: str, query: dict[str, str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        page_size = int(query.get("count", "100"))
        while True:
            q = dict(query)
            q["offset"] = str(offset)
            data = self._req("GET", f"{path}?{urllib.parse.urlencode(q)}")
            rows = (data or {}).get("data") or []
            out.extend(rows)
            if len(rows) < page_size:
                break
            offset += len(rows)
        return out

    def _req(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        ok_statuses: frozenset[int] = frozenset(),
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.settings.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Token {self.settings.token_id}:{self.settings.token_secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        return self._open(req, ok_statuses=ok_statuses)

    def _open(
        self,
        req: urllib.request.Request,
        *,
        ok_statuses: frozenset[int],
    ) -> dict[str, Any]:
        max_tries = 6
        for tries in range(max_tries):
            self._wait()
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    body = response.read()
                    if not body:
                        return {}
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and tries < max_tries - 1:
                    time.sleep(min(60, 2 ** (tries + 1)))
                    continue
                if exc.code in ok_statuses:
                    return {}
                body = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(
                    f"BookStack HTTP {exc.code} on {req.method} {req.full_url}: {body}"
                ) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"BookStack unreachable: {exc.reason}") from exc
        raise RuntimeError("BookStack request retry exhausted")

    def _wait(self) -> None:
        gap = self.settings.throttle - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()


# ── env loading ──────────────────────────────────────────────────────


def _load_bookstack_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read BOOKSTACK_* keys from env + ~/.dora/bookstack.env.

    Process env (non-empty values) wins over file values, mirroring
    :func:`orchestrator.plane_live._load_plane_env`.
    """
    env = dict(environ or os.environ)
    merged: dict[str, str] = {}
    candidates = [
        Path.home() / ".dora" / "bookstack.env",
        Path.home() / "dagster" / ".env",
    ]
    if env.get("DORA_HOME"):
        candidates.append(Path(env["DORA_HOME"]) / "bookstack.env")
    for path in reversed(candidates):
        merged.update(_parse_env_file(path))
    merged.update(
        {key: value for key, value in env.items() if key.startswith("BOOKSTACK_") and value}
    )
    return merged


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key.strip()] = value
    return out
