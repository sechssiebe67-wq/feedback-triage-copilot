"""Domain models for Feedback Triage Copilot.

Everything here is plain Python data so the workspace can be serialised to a
single JSON file without any third-party dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


ACTIVE = "active"
DISCARDED = "discarded"


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class FeedbackItem:
    """A single piece of feedback from one source channel."""

    id: str
    text: str
    source: str = "manual"
    segment: str = "unknown"
    url: str = ""
    created_at: str = ""
    rating: Optional[float] = None
    language: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeedbackItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Cluster:
    """A thematic cluster of feedback items.

    Clusters are editable: ``status`` can be ``active`` or ``discarded`` and the
    ``item_ids`` may be modified by merge/split corrections.
    """

    id: str
    label: str
    item_ids: List[str] = field(default_factory=list)
    status: str = ACTIVE
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    parent_ids: List[str] = field(default_factory=list)
    uncertainty: List[str] = field(default_factory=list)
    cohesion: float = 0.0
    separation: float = 1.0
    origin: str = "auto"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Cluster":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Reviewer:
    """A named identity that can be attributed to triage decisions.

    The MVP deliberately has no authentication: a reviewer is a label chosen
    in the UI or supplied via the API/CLI. Keeping reviewers as workspace data
    preserves the database-free design.
    """

    id: str
    name: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Reviewer":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EditEvent:
    """An append-only audit record for a user correction.

    ``reviewer`` and ``note`` are optional: workspaces written before reviewer
    attribution existed load with empty strings, so the history stays backward
    compatible while every new decision can carry an identity and a rationale.
    """

    id: str
    timestamp: str
    action: str
    details: Dict[str, Any] = field(default_factory=dict)
    reviewer: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EditEvent":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Digest:
    """A derived weekly digest of triage activity and reviewer load.

    The digest is never persisted in the workspace: it is rebuilt from the
    append-only history and the current items/clusters, which keeps it
    auditable and lets old workspaces (without any digest fields) load
    unchanged and still produce a zeroed digest.
    """

    window_start: str
    window_end: str
    since: str = ""
    generated_at: str = field(default_factory=now_iso)
    days: int = 7
    total_items: int = 0
    new_items: int = 0
    new_clusters: int = 0
    discarded_clusters: int = 0
    decisions: int = 0
    reviewer_load: Dict[str, Dict[str, int]] = field(default_factory=dict)
    decisions_by_action: Dict[str, int] = field(default_factory=dict)
    unreviewed: int = 0
    untouched_topics: List[Dict[str, Any]] = field(default_factory=list)
    changed_topics: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Digest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Workspace:
    """The complete persisted state of one tenant workspace."""

    items: List[FeedbackItem] = field(default_factory=list)
    clusters: List[Cluster] = field(default_factory=list)
    history: List[EditEvent] = field(default_factory=list)
    reviewers: List[Reviewer] = field(default_factory=list)
    version: int = 1
    settings: Dict[str, Any] = field(
        default_factory=lambda: {
            "similarity_threshold": 0.14,
            "min_support": 2,
        }
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "settings": self.settings,
            "items": [i.to_dict() for i in self.items],
            "clusters": [c.to_dict() for c in self.clusters],
            "history": [h.to_dict() for h in self.history],
            "reviewers": [r.to_dict() for r in self.reviewers],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workspace":
        return cls(
            items=[FeedbackItem.from_dict(i) for i in data.get("items", [])],
            clusters=[Cluster.from_dict(c) for c in data.get("clusters", [])],
            history=[EditEvent.from_dict(h) for h in data.get("history", [])],
            reviewers=[Reviewer.from_dict(r) for r in data.get("reviewers", [])],
            version=int(data.get("version", 1)),
            settings=data.get("settings")
            or {"similarity_threshold": 0.14, "min_support": 2},
        )
