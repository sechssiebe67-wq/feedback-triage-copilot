"""Single-file JSON workspace with an append-only correction history.

A database is not required: one workspace is one JSON document. Writes are
atomic (temp file + rename) so a crash cannot corrupt the workspace.
"""

from __future__ import annotations

import glob as glob_module
import json
import os
import uuid
from typing import Dict, List, Optional, Sequence

from .cluster import cluster_items
from .ingest import load_csv_file
from .models import (
    ACTIVE,
    DISCARDED,
    Cluster,
    EditEvent,
    FeedbackItem,
    IngestRun,
    Reviewer,
    SourceConfig,
    Workspace,
    now_iso,
)


class TriageStore:
    """In-memory workspace with optional JSON persistence."""

    def __init__(
        self,
        path: Optional[str] = None,
        workspace: Optional[Workspace] = None,
    ) -> None:
        self.path = path
        self.workspace = workspace or Workspace()

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "TriageStore":
        if not os.path.exists(path):
            return cls(path=path)
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(path=path, workspace=Workspace.from_dict(data))

    def save(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self.workspace.to_dict(), handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, self.path)

    # -- queries ---------------------------------------------------------
    @property
    def items(self) -> List[FeedbackItem]:
        return self.workspace.items

    @property
    def clusters(self) -> List[Cluster]:
        return self.workspace.clusters

    @property
    def history(self) -> List[EditEvent]:
        return self.workspace.history

    @property
    def reviewers(self) -> List[Reviewer]:
        return self.workspace.reviewers

    @property
    def sources(self) -> List[SourceConfig]:
        return self.workspace.sources

    @property
    def ingest_runs(self) -> List[IngestRun]:
        return self.workspace.ingest_runs

    def items_by_id(self) -> Dict[str, FeedbackItem]:
        return {item.id: item for item in self.workspace.items}

    # -- reviewers -------------------------------------------------------
    def add_reviewer(self, name: str) -> Reviewer:
        """Add a reviewer identity and return it.

        Names are unique case-insensitively: re-adding an existing name returns
        the stored reviewer instead of creating a duplicate.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("reviewer name is required")
        for existing in self.workspace.reviewers:
            if existing.name.lower() == cleaned.lower():
                return existing
        reviewer = Reviewer(id="r" + uuid.uuid4().hex[:10], name=cleaned)
        self.workspace.reviewers.append(reviewer)
        self.save()
        return reviewer

    def list_reviewers(self) -> List[Reviewer]:
        return list(self.workspace.reviewers)

    def get_reviewer(self, reviewer_id: str) -> Reviewer:
        for reviewer in self.workspace.reviewers:
            if reviewer.id == reviewer_id:
                return reviewer
        raise KeyError("Unknown reviewer: %s" % reviewer_id)

    def reviewer_name(self, value: str) -> str:
        """Resolve a reviewer id to a name, or return the raw value.

        Decisions may be recorded with either an id (dashboard) or a name
        (CLI), so unknown values pass through unchanged.
        """
        if not value:
            return ""
        for reviewer in self.workspace.reviewers:
            if reviewer.id == value:
                return reviewer.name
        return value

    # -- sources ---------------------------------------------------------
    def add_source(
        self,
        name: str,
        path: str,
        glob: str = "",
        source_label: str = "",
        segment_default: str = "",
        enabled: bool = True,
    ) -> SourceConfig:
        """Register a named source and return it.

        ``name`` and ``path`` are required; the path may point at a single file
        or a directory. ``source_label`` defaults to the source name and is used
        as the feedback item ``source`` when a CSV row does not provide one.
        """
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("source name is required")
        cleaned_path = (path or "").strip()
        if not cleaned_path:
            raise ValueError("source path is required")
        source = SourceConfig(
            id="src" + uuid.uuid4().hex[:10],
            name=cleaned_name,
            path=cleaned_path,
            glob=(glob or "").strip(),
            source_label=(source_label or "").strip() or cleaned_name,
            segment_default=(segment_default or "").strip(),
            enabled=bool(enabled),
        )
        self.workspace.sources.append(source)
        self.save()
        return source

    def list_sources(self) -> List[SourceConfig]:
        return list(self.workspace.sources)

    def get_source(self, source_id: str) -> SourceConfig:
        for source in self.workspace.sources:
            if source.id == source_id:
                return source
        raise KeyError("Unknown source: %s" % source_id)

    def set_source_enabled(self, source_id: str, enabled: bool) -> SourceConfig:
        source = self.get_source(source_id)
        source.enabled = bool(enabled)
        self.save()
        return source

    def remove_source(self, source_id: str) -> SourceConfig:
        source = self.get_source(source_id)
        self.workspace.sources.remove(source)
        self.save()
        return source

    def _resolve_source_files(self, source: SourceConfig) -> List[str]:
        """Return the files a source resolves to, honouring dir + glob.

        A directory is searched recursively; without an explicit glob only
        ``*.csv`` files are picked up. A missing path (or a glob with no
        matches) raises :class:`ValueError` so callers can report a bad source
        instead of silently ingesting nothing.
        """
        path = source.path
        pattern = source.glob or ""
        if os.path.isdir(path):
            if pattern:
                matches = glob_module.glob(
                    os.path.join(path, "**", pattern), recursive=True
                )
            else:
                matches = glob_module.glob(
                    os.path.join(path, "**", "*.csv"), recursive=True
                )
            return sorted(m for m in matches if os.path.isfile(m))
        if os.path.isfile(path):
            return [path]
        if any(char in path for char in "*?["):
            matches = glob_module.glob(path, recursive=True)
            return sorted(m for m in matches if os.path.isfile(m))
        raise ValueError("source path not found: %s" % path)

    def run_source(
        self, source_id: str, reviewer: str = "", note: str = ""
    ) -> IngestRun:
        """Run one source and append exactly one :class:`IngestRun`.

        Items already present in the workspace are skipped, so running the same
        files twice never duplicates data. A file that fails to parse is
        recorded as an error entry and the remaining files still run.
        """
        source = self.get_source(source_id)
        files = self._resolve_source_files(source)
        existing = {item.id for item in self.workspace.items}
        file_names: List[str] = []
        errors: List[Dict[str, str]] = []
        added = 0
        skipped = 0
        for file_path in files:
            file_name = os.path.basename(file_path)
            file_names.append(file_name)
            try:
                items = load_csv_file(file_path, source=source.source_label)
            except Exception as exc:  # per-file isolation
                errors.append({"file": file_name, "error": str(exc)})
                continue
            for item in items:
                if source.segment_default and item.segment in ("", "unknown"):
                    item.segment = source.segment_default
                if item.id in existing:
                    skipped += 1
                    continue
                self.workspace.items.append(item)
                existing.add(item.id)
                added += 1
        run = IngestRun(
            id="run" + uuid.uuid4().hex[:10],
            timestamp=now_iso(),
            source_id=source.id,
            source_name=source.name,
            files=file_names,
            added=added,
            skipped=skipped,
            errors=errors,
            reviewer=reviewer or "",
            note=note or "",
        )
        self.workspace.ingest_runs.append(run)
        source.last_run_at = run.timestamp
        source.last_added = added
        source.last_skipped = skipped
        self._record(
            "ingest",
            {
                "source_id": source.id,
                "source_name": source.name,
                "files": file_names,
                "added": added,
                "skipped": skipped,
                "errors": len(errors),
            },
            reviewer=reviewer,
            note=note,
        )
        if added:
            self.recluster(record=False)
        self.save()
        return run

    def run_all_sources(
        self, reviewer: str = "", note: str = ""
    ) -> List[IngestRun]:
        """Run every enabled source, isolating per-source failures.

        A source whose path is missing is recorded as a failed run (with an
        error entry) while the remaining sources still run, so a scheduled job
        produces a complete log and exits successfully.
        """
        runs: List[IngestRun] = []
        for source in list(self.workspace.sources):
            if not source.enabled:
                continue
            try:
                runs.append(self.run_source(source.id, reviewer=reviewer, note=note))
            except Exception as exc:
                run = IngestRun(
                    id="run" + uuid.uuid4().hex[:10],
                    timestamp=now_iso(),
                    source_id=source.id,
                    source_name=source.name,
                    files=[],
                    added=0,
                    skipped=0,
                    errors=[{"file": source.path, "error": str(exc)}],
                    reviewer=reviewer or "",
                    note=note or "",
                )
                self.workspace.ingest_runs.append(run)
                source.last_run_at = run.timestamp
                source.last_added = 0
                source.last_skipped = 0
                self._record(
                    "ingest",
                    {
                        "source_id": source.id,
                        "source_name": source.name,
                        "files": [],
                        "added": 0,
                        "skipped": 0,
                        "errors": 1,
                    },
                    reviewer=reviewer,
                    note=note,
                )
                self.save()
                runs.append(run)
        return runs

    def active_clusters(self) -> List[Cluster]:
        return [c for c in self.workspace.clusters if c.status == ACTIVE]

    def get_cluster(self, cluster_id: str) -> Cluster:
        for cluster in self.workspace.clusters:
            if cluster.id == cluster_id:
                return cluster
        raise KeyError("Unknown cluster: %s" % cluster_id)

    @property
    def settings(self) -> Dict[str, object]:
        return self.workspace.settings

    def update_settings(self, **values: object) -> None:
        self.workspace.settings.update(values)

    # -- ingestion -------------------------------------------------------
    def add_items(
        self,
        items: Sequence[FeedbackItem],
        recluster: bool = True,
        reviewer: str = "",
        note: str = "",
    ) -> int:
        existing = {item.id for item in self.workspace.items}
        added = 0
        for item in items:
            if item.id in existing:
                continue
            self.workspace.items.append(item)
            existing.add(item.id)
            added += 1
        self._record(
            "import",
            {"added": added, "source": _sources(items)},
            reviewer=reviewer,
            note=note,
        )
        if recluster and added:
            self.recluster(record=False)
        if added:
            self.save()
        return added

    # -- clustering ------------------------------------------------------
    def recluster(
        self, threshold: Optional[float] = None, min_support: Optional[int] = None,
        record: bool = True, reviewer: str = "", note: str = "",
    ) -> List[Cluster]:
        threshold = float(
            threshold if threshold is not None else self.settings["similarity_threshold"]
        )
        min_support = int(
            min_support if min_support is not None else self.settings["min_support"]
        )
        clusters = cluster_items(self.workspace.items, threshold, min_support)
        self.workspace.clusters = clusters
        if record:
            self._record(
                "recluster",
                {"clusters": len(clusters), "threshold": threshold,
                 "min_support": min_support},
                reviewer=reviewer,
                note=note,
            )
            self.save()
        return clusters

    # -- corrections -----------------------------------------------------
    def merge(
        self,
        cluster_ids: Sequence[str],
        label: Optional[str] = None,
        reviewer: str = "",
        note: str = "",
    ) -> Cluster:
        sources = [self.get_cluster(cid) for cid in cluster_ids]
        item_ids: List[str] = []
        for cluster in sources:
            item_ids.extend(cluster.item_ids)
        if not item_ids:
            raise ValueError("Cannot merge empty clusters")
        for cluster in sources:
            cluster.status = DISCARDED
            cluster.updated_at = now_iso()
        merged = Cluster(
            id="m" + uuid.uuid4().hex[:10],
            label=label or " / ".join(c.label for c in sources if c.label)[:80]
            or "Merged cluster",
            item_ids=item_ids,
            origin="manual",
            parent_ids=[c.id for c in sources],
        )
        self.workspace.clusters.append(merged)
        self._record(
            "merge",
            {"cluster_id": merged.id, "sources": [c.id for c in sources]},
            reviewer=reviewer,
            note=note,
        )
        self.save()
        return merged

    def split(
        self,
        cluster_id: str,
        item_ids: Sequence[str],
        label: Optional[str] = None,
        reviewer: str = "",
        note: str = "",
    ) -> Cluster:
        source = self.get_cluster(cluster_id)
        moving = [i for i in item_ids if i in source.item_ids]
        if not moving:
            raise ValueError("No items selected for split")
        remaining = [i for i in source.item_ids if i not in moving]
        source.item_ids = remaining
        source.updated_at = now_iso()
        if not remaining:
            source.status = DISCARDED
        new_cluster = Cluster(
            id="s" + uuid.uuid4().hex[:10],
            label=label or ("Split of " + source.label)[:80],
            item_ids=moving,
            origin="manual",
            parent_ids=[source.id],
        )
        self.workspace.clusters.append(new_cluster)
        self._record(
            "split",
            {"cluster_id": new_cluster.id, "source": source.id, "moved": moving},
            reviewer=reviewer,
            note=note,
        )
        self.save()
        return new_cluster

    def discard(self, cluster_id: str, reviewer: str = "", note: str = "") -> Cluster:
        cluster = self.get_cluster(cluster_id)
        cluster.status = DISCARDED
        cluster.updated_at = now_iso()
        self._record(
            "discard", {"cluster_id": cluster_id}, reviewer=reviewer, note=note
        )
        self.save()
        return cluster

    def restore(self, cluster_id: str, reviewer: str = "", note: str = "") -> Cluster:
        cluster = self.get_cluster(cluster_id)
        cluster.status = ACTIVE
        cluster.updated_at = now_iso()
        self._record(
            "restore", {"cluster_id": cluster_id}, reviewer=reviewer, note=note
        )
        self.save()
        return cluster

    def rename(
        self, cluster_id: str, label: str, reviewer: str = "", note: str = ""
    ) -> Cluster:
        cluster = self.get_cluster(cluster_id)
        old = cluster.label
        cluster.label = label
        cluster.updated_at = now_iso()
        self._record(
            "rename",
            {"cluster_id": cluster_id, "from": old, "to": label},
            reviewer=reviewer,
            note=note,
        )
        self.save()
        return cluster

    # -- helpers ---------------------------------------------------------
    def _record(
        self,
        action: str,
        details: Dict[str, object],
        reviewer: str = "",
        note: str = "",
    ) -> EditEvent:
        event = EditEvent(
            id=uuid.uuid4().hex[:10],
            timestamp=now_iso(),
            action=action,
            details=details,
            reviewer=reviewer or "",
            note=note or "",
        )
        self.workspace.history.append(event)
        return event


def _sources(items: Sequence[FeedbackItem]) -> List[str]:
    return sorted({item.source for item in items})
