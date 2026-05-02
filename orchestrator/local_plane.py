"""File-backed Plane-compatible client for local orchestration dry-runs."""

import json
from pathlib import Path
from typing import Any

from .in_memory_plane import InMemoryPlaneClient


class LocalPlaneClient(InMemoryPlaneClient):
    def __init__(self, state_path: Path):
        self.state_path = state_path
        super().__init__()
        self._load()

    def upsert_project(self, slug: str, title: str) -> dict[str, Any]:
        result = super().upsert_project(slug, title)
        self._save()
        return result

    def upsert_cycle(self, project_slug: str, name: str) -> dict[str, Any]:
        result = super().upsert_cycle(project_slug, name)
        self._save()
        return result

    def upsert_module(self, project_slug: str, name: str) -> dict[str, Any]:
        result = super().upsert_module(project_slug, name)
        self._save()
        return result

    def upsert_page(self, project_slug: str, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().upsert_page(project_slug, slug, payload)
        self._save()
        return result

    def upsert_issue(self, project_slug: str, external_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().upsert_issue(project_slug, external_id, payload)
        self._save()
        return result

    def claim_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        result = super().claim_issue(project_slug, external_id, run_id)
        self._save()
        return result

    def release_issue(self, project_slug: str, external_id: str, state: str) -> dict[str, Any]:
        result = super().release_issue(project_slug, external_id, state)
        self._save()
        return result

    def publish_run_report(self, project_slug: str, external_id: str, report: dict[str, Any]) -> dict[str, Any]:
        result = super().publish_run_report(project_slug, external_id, report)
        self._save()
        return result

    def add_comment(self, project_slug: str, external_id: str, body: str, *, marker: str | None = None, raw_html: bool = False) -> dict[str, Any]:
        result = super().add_comment(project_slug, external_id, body, marker=marker, raw_html=raw_html)
        self._save()
        return result

    def heartbeat_issue(self, project_slug: str, external_id: str, run_id: str) -> dict[str, Any]:
        result = super().heartbeat_issue(project_slug, external_id, run_id)
        self._save()
        return result

    def add_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        result = super().add_label(project_slug, external_id, label_name)
        self._save()
        return result

    def remove_label(self, project_slug: str, external_id: str, label_name: str) -> dict[str, Any]:
        result = super().remove_label(project_slug, external_id, label_name)
        self._save()
        return result

    def update_page(self, project_slug: str, slug: str, *, body: str | None = None, title: str | None = None, match_substring: str | None = None) -> dict[str, Any]:
        result = super().update_page(project_slug, slug, body=body, title=title, match_substring=match_substring)
        self._save()
        return result

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.projects = dict(data.get("projects", {}))
        self.cycles = {
            (item["project_slug"], item["name"]): item["value"]
            for item in data.get("cycles", [])
        }
        self.modules = {
            (item["project_slug"], item["name"]): item["value"]
            for item in data.get("modules", [])
        }
        self.pages = {
            (item["project_slug"], item["slug"]): item["value"]
            for item in data.get("pages", [])
        }
        self.issues = {
            (item["project_slug"], item["external_id"]): item["value"]
            for item in data.get("issues", [])
        }
        self.reports = list(data.get("reports", []))
        self.labels = {
            (item["project_slug"], item["name"]): item["value"]
            for item in data.get("labels", [])
        }
        self.comments = list(data.get("comments", []))
        self.heartbeats = list(data.get("heartbeats", []))

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": self.projects,
            "cycles": [
                {"project_slug": project_slug, "name": name, "value": value}
                for (project_slug, name), value in sorted(self.cycles.items())
            ],
            "modules": [
                {"project_slug": project_slug, "name": name, "value": value}
                for (project_slug, name), value in sorted(self.modules.items())
            ],
            "pages": [
                {"project_slug": project_slug, "slug": slug, "value": value}
                for (project_slug, slug), value in sorted(self.pages.items())
            ],
            "issues": [
                {"project_slug": project_slug, "external_id": external_id, "value": value}
                for (project_slug, external_id), value in sorted(self.issues.items())
            ],
            "reports": self.reports,
            "labels": [
                {"project_slug": project_slug, "name": name, "value": value}
                for (project_slug, name), value in sorted(self.labels.items())
            ],
            "comments": self.comments,
            "heartbeats": self.heartbeats,
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
