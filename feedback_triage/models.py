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

CLAIMED = "claimed"
RESOLVED = "resolved"


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
class SourceConfig:
    """A declared, repeatedly-importable feedback source.

    A source points at a CSV file or a directory (optionally filtered by a
    ``glob``) and remembers the outcome of its last run so a scheduled job can
    be monitored without a database. ``source_label`` and ``segment_default``
    are applied to items whose CSV does not supply a source or segment.
    """

    id: str
    name: str
    path: str
    glob: str = ""
    source_label: str = ""
    segment_default: str = ""
    enabled: bool = True
    created_at: str = field(default_factory=now_iso)
    last_run_at: str = ""
    last_added: int = 0
    last_skipped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class IngestRun:
    """One append-only record of running one source.

    ``files`` lists the file names that were considered, ``added``/``skipped``
    are the idempotency counters (skipped counts ids already in the workspace)
    and ``errors`` holds a ``{"file", "error"}`` entry per file that failed to
    parse, so a single bad file never aborts the whole run.
    """

    id: str
    timestamp: str
    source_id: str
    source_name: str
    files: List[str] = field(default_factory=list)
    added: int = 0
    skipped: int = 0
    errors: List[Dict[str, str]] = field(default_factory=list)
    reviewer: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IngestRun":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Claim:
    """A reviewer's ownership of one uncertain cluster.

    Claims are the database-free review queue: a reviewer *claims* a cluster
    (``status == "claimed"``) so teammates can see it is taken, then *resolves*
    it (``status == "resolved"``) or *releases* it back to the queue. A released
    claim is removed from the workspace; a resolved claim is kept so the decision
    log and the "clusters resolved per reviewer" metric stay complete.
    """

    id: str
    cluster_id: str
    reviewer: str
    status: str = CLAIMED
    claimed_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Claim":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Workspace:
    """The complete persisted state of one tenant workspace."""

    items: List[FeedbackItem] = field(default_factory=list)
    clusters: List[Cluster] = field(default_factory=list)
    history: List[EditEvent] = field(default_factory=list)
    reviewers: List[Reviewer] = field(default_factory=list)
    sources: List[SourceConfig] = field(default_factory=list)
    ingest_runs: List[IngestRun] = field(default_factory=list)
    claims: List[Claim] = field(default_factory=list)
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
            "sources": [s.to_dict() for s in self.sources],
            "ingest_runs": [r.to_dict() for r in self.ingest_runs],
            "claims": [c.to_dict() for c in self.claims],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workspace":
        return cls(
            items=[FeedbackItem.from_dict(i) for i in data.get("items", [])],
            clusters=[Cluster.from_dict(c) for c in data.get("clusters", [])],
            history=[EditEvent.from_dict(h) for h in data.get("history", [])],
            reviewers=[Reviewer.from_dict(r) for r in data.get("reviewers", [])],
            sources=[SourceConfig.from_dict(s) for s in data.get("sources", [])],
            ingest_runs=[
                IngestRun.from_dict(r) for r in data.get("ingest_runs", [])
            ],
            claims=[Claim.from_dict(c) for c in data.get("claims", [])],
            version=int(data.get("version", 1)),
            settings=data.get("settings")
            or {"similarity_threshold": 0.14, "min_support": 2},
        )
