"""Single-file JSON workspace with an append-only correction history.

A database is not required: one workspace is one JSON document. Writes are
atomic (temp file + rename) so a crash cannot corrupt the workspace.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Dict, List, Optional, Sequence

from .cluster import cluster_items
from .models import (
    ACTIVE,
    DISCARDED,
    Cluster,
    EditEvent,
    FeedbackItem,
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

    def items_by_id(self) -> Dict[str, FeedbackItem]:
        return {item.id: item for item in self.workspace.items}

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
    def add_items(self, items: Sequence[FeedbackItem], recluster: bool = True) -> int:
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
        )
        if recluster and added:
            self.recluster(record=False)
        if added:
            self.save()
        return added

    # -- clustering ------------------------------------------------------
    def recluster(
        self, threshold: Optional[float] = None, min_support: Optional[int] = None,
        record: bool = True,
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
            )
            self.save()
        return clusters

    # -- corrections -----------------------------------------------------
    def merge(
        self, cluster_ids: Sequence[str], label: Optional[str] = None
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
        )
        self.save()
        return merged

    def split(
        self, cluster_id: str, item_ids: Sequence[str], label: Optional[str] = None
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
        )
        self.save()
        return new_cluster

    def discard(self, cluster_id: str) -> Cluster:
        cluster = self.get_cluster(cluster_id)
        cluster.status = DISCARDED
        cluster.updated_at = now_iso()
        self._record("discard", {"cluster_id": cluster_id})
        self.save()
        return cluster

    def restore(self, cluster_id: str) -> Cluster:
        cluster = self.get_cluster(cluster_id)
        cluster.status = ACTIVE
        cluster.updated_at = now_iso()
        self._record("restore", {"cluster_id": cluster_id})
        self.save()
        return cluster

    def rename(self, cluster_id: str, label: str) -> Cluster:
        cluster = self.get_cluster(cluster_id)
        old = cluster.label
        cluster.label = label
        cluster.updated_at = now_iso()
        self._record("rename", {"cluster_id": cluster_id, "from": old, "to": label})
        self.save()
        return cluster

    # -- helpers ---------------------------------------------------------
    def _record(self, action: str, details: Dict[str, object]) -> EditEvent:
        event = EditEvent(
            id=uuid.uuid4().hex[:10],
            timestamp=now_iso(),
            action=action,
            details=details,
        )
        self.workspace.history.append(event)
        return event


def _sources(items: Sequence[FeedbackItem]) -> List[str]:
    return sorted({item.source for item in items})
