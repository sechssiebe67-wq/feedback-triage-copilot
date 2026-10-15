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
class EditEvent:
    """An append-only audit record for a user correction."""

    id: str
    timestamp: str
    action: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EditEvent":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Workspace:
    """The complete persisted state of one tenant workspace."""

    items: List[FeedbackItem] = field(default_factory=list)
    clusters: List[Cluster] = field(default_factory=list)
    history: List[EditEvent] = field(default_factory=list)
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
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workspace":
        return cls(
            items=[FeedbackItem.from_dict(i) for i in data.get("items", [])],
            clusters=[Cluster.from_dict(c) for c in data.get("clusters", [])],
            history=[EditEvent.from_dict(h) for h in data.get("history", [])],
            version=int(data.get("version", 1)),
            settings=data.get("settings")
            or {"similarity_threshold": 0.14, "min_support": 2},
        )
