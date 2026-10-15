"""Per-reviewer weekly digest and workload balancing.

Everything here is pure and read-only: the functions inspect a
:class:`~feedback_triage.store.TriageStore` and return plain dictionaries or
strings, and nothing mutates the workspace.

The workspace has no explicit assignment field (and stays backward compatible),
so responsibility is inferred from the append-only decision log: the reviewer
attributed to the most recent decision that touched a cluster owns it. Clusters
with no attributed decision have no reviewer activity and become candidates for
balancing.

The balancing rule is intentionally simple and explainable: walk the active,
undecided clusters (largest first for stability) and assign each to the
reviewer who currently has the fewest open clusters. Ties are broken by
reviewer name and then id, so the result is deterministic and reproducible.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence

from .models import Cluster, EditEvent, Reviewer, now_iso
from .report import DECISION_LIMIT, _affected_labels, _csv_safe, _esc


# -- public API ----------------------------------------------------------
def build_digest(store, reviewer: str = "") -> Dict[str, object]:
    """Build the per-reviewer digest for a store.

    ``reviewer`` filters the digest to a single reviewer, matched by id or by
    name (case-insensitive). An unknown reviewer yields an empty digest rather
    than an error. Without a filter the digest covers every reviewer plus the
    clusters that have no reviewer activity.
    """
    active = sorted(
        store.active_clusters(), key=lambda c: (c.label.lower(), c.id)
    )
    reviewers = sorted(
        store.list_reviewers(),
        key=lambda r: (r.name.lower(), r.name, r.id),
    )
    owners = _owners(store)
    all_decisions = _recent_decisions(store)

    assignments: List[Dict[str, object]] = []
    unassigned: List[Dict[str, object]] = []
    for cluster in active:
        owner = owners.get(cluster.id)
        if owner:
            reviewer_id, reviewer_name = _resolve_reviewer(store, owner)
            assignments.append({
                "cluster_id": cluster.id,
                "cluster_label": cluster.label,
                "reviewer_id": reviewer_id,
                "reviewer_name": reviewer_name,
                "open_items": len(cluster.item_ids),
            })
        else:
            unassigned.append({
                "cluster_id": cluster.id,
                "cluster_label": cluster.label,
                "open_items": len(cluster.item_ids),
            })

    suggested = suggest_assignments(
        store, clusters=[_find_cluster(active, entry["cluster_id"])
                         for entry in unassigned]
    ) if unassigned else []

    recent_decisions = all_decisions
    if reviewer:
        assignments = [
            entry for entry in assignments
            if _matches(reviewer, entry["reviewer_id"], entry["reviewer_name"])
        ]
        recent_decisions = [
            decision for decision in all_decisions
            if _matches(reviewer, decision["reviewer_id"], decision["reviewer"])
        ]
        unassigned = []
        suggested = []

    summaries: List[Dict[str, object]] = []
    for candidate in reviewers:
        owned = [
            entry for entry in assignments
            if _belongs(entry["reviewer_id"], entry["reviewer_name"], candidate)
        ]
        decisions = [
            decision for decision in recent_decisions
            if _belongs(decision["reviewer_id"], decision["reviewer"], candidate)
        ]
        summaries.append({
            "id": candidate.id,
            "name": candidate.name,
            "assignment_count": len(owned),
            "open_items": sum(int(entry["open_items"]) for entry in owned),
            "assigned_clusters": [entry["cluster_id"] for entry in owned],
            "recent_decisions": decisions,
        })
    if reviewer:
        summaries = [
            summary for summary in summaries
            if _matches(reviewer, summary["id"], summary["name"])
        ]

    totals = {
        "reviewers": len(summaries) if reviewer else len(reviewers),
        "reviewers_with_activity": sum(
            1 for summary in summaries
            if summary["assignment_count"] or summary["recent_decisions"]
        ),
        "active_clusters": len(assignments) + len(unassigned),
        "assigned_clusters": len(assignments),
        "unassigned_clusters": len(unassigned),
        "open_items": (
            sum(int(entry["open_items"]) for entry in assignments)
            + sum(int(entry["open_items"]) for entry in unassigned)
        ),
        "recent_decisions": len(recent_decisions),
    }

    return {
        "generated_at": now_iso(),
        "reviewer": reviewer or "",
        "reviewers": summaries,
        "assignments": assignments,
        "unassigned_clusters": unassigned,
        "recent_decisions": recent_decisions,
        "suggested_assignments": suggested,
        "totals": totals,
    }


def suggest_assignments(store, clusters: Optional[Sequence[Cluster]] = None
                        ) -> List[Dict[str, object]]:
    """Return a least-loaded-first balancing suggestion.

    Each entry has ``cluster_id``, ``cluster_label``, ``reviewer_id``,
    ``reviewer_name`` and ``open_items``. The store is never modified.

    ``clusters`` defaults to the active clusters that no reviewer owns yet.
    Existing ownership counts towards a reviewer's starting load, so reviewers
    who already carry clusters receive fewer new ones.
    """
    reviewers = sorted(
        store.list_reviewers(),
        key=lambda r: (r.name.lower(), r.name, r.id),
    )
    if not reviewers:
        return []

    owners = _owners(store)
    if clusters is None:
        candidates = [
            cluster for cluster in store.active_clusters()
            if cluster.id not in owners
        ]
    else:
        candidates = [cluster for cluster in clusters if cluster is not None]
    candidates = sorted(
        candidates,
        key=lambda c: (-len(c.item_ids), c.label.lower(), c.id),
    )

    load: Dict[str, int] = {r.id: 0 for r in reviewers}
    for value in owners.values():
        owner = _find_reviewer(store, value)
        if owner is not None:
            load[owner.id] += 1

    assignments: List[Dict[str, object]] = []
    for cluster in candidates:
        chosen = min(
            reviewers,
            key=lambda r: (load[r.id], r.name.lower(), r.name, r.id),
        )
        load[chosen.id] += 1
        assignments.append({
            "cluster_id": cluster.id,
            "cluster_label": cluster.label,
            "reviewer_id": chosen.id,
            "reviewer_name": chosen.name,
            "open_items": len(cluster.item_ids),
        })
    return assignments


# -- renderers -----------------------------------------------------------
def digest_to_json(digest: Dict[str, object]) -> str:
    """Serialise the digest to pretty-printed JSON (UTF-8 friendly)."""
    return json.dumps(digest, ensure_ascii=False, indent=2)


def digest_to_text(digest: Dict[str, object]) -> str:
    """Render the digest as plain text.

    Untrusted values are flattened onto one line and neutralised against
    spreadsheet formula injection with the same :func:`_csv_safe` rule used by
    the CSV export.
    """
    totals = digest.get("totals", {}) or {}
    lines: List[str] = [
        "Per-reviewer Feedback Triage Digest",
        "Generated %s" % _text_safe(digest.get("generated_at", "")),
    ]
    if digest.get("reviewer"):
        lines.append("Filtered to reviewer: %s" % _text_safe(digest["reviewer"]))
    lines.append(
        "Totals: %d reviewers, %d active clusters, %d assigned, %d unassigned, "
        "%d open items, %d decisions"
        % (
            totals.get("reviewers", 0),
            totals.get("active_clusters", 0),
            totals.get("assigned_clusters", 0),
            totals.get("unassigned_clusters", 0),
            totals.get("open_items", 0),
            totals.get("recent_decisions", 0),
        )
    )

    lines.append("")
    lines.append("Reviewers:")
    reviewers = digest.get("reviewers", []) or []
    if reviewers:
        for reviewer in reviewers:
            lines.append(
                "  %s (%s): %d assigned, %d open items"
                % (
                    _text_safe(reviewer.get("name", "")),
                    _text_safe(reviewer.get("id", "")),
                    reviewer.get("assignment_count", 0),
                    reviewer.get("open_items", 0),
                )
            )
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Assignments:")
    assignments = digest.get("assignments", []) or []
    if assignments:
        for entry in assignments:
            lines.append(
                "  %s -> %s (%d open items)"
                % (
                    _text_safe(entry.get("cluster_label", "")),
                    _text_safe(entry.get("reviewer_name", "") or "unassigned"),
                    entry.get("open_items", 0),
                )
            )
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Clusters with no reviewer activity:")
    unassigned = digest.get("unassigned_clusters", []) or []
    if unassigned:
        for entry in unassigned:
            lines.append(
                "  %s (%d open items)"
                % (
                    _text_safe(entry.get("cluster_label", "")),
                    entry.get("open_items", 0),
                )
            )
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Suggested balancing order (least-loaded first):")
    suggested = digest.get("suggested_assignments", []) or []
    if suggested:
        for entry in suggested:
            lines.append(
                "  %s -> %s (%d open items)"
                % (
                    _text_safe(entry.get("cluster_label", "")),
                    _text_safe(entry.get("reviewer_name", "")),
                    entry.get("open_items", 0),
                )
            )
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Recent decisions:")
    decisions = digest.get("recent_decisions", []) or []
    if decisions:
        for decision in decisions:
            line = "  %s  %s  by %s" % (
                _text_safe(decision.get("timestamp", "")),
                _text_safe(decision.get("action", "")),
                _text_safe(decision.get("reviewer", "") or "unreviewed"),
            )
            clusters = decision.get("clusters") or []
            if clusters:
                line += "  [%s]" % _text_safe(", ".join(clusters))
            if decision.get("note"):
                line += "  note: %s" % _text_safe(decision["note"])
            lines.append(line)
    else:
        lines.append("  none")

    return "\n".join(lines) + "\n"


def digest_to_html(digest: Dict[str, object]) -> str:
    """Render the digest as a standalone HTML document.

    Every untrusted value is escaped with :func:`_esc` so reviewer names and
    notes cannot inject markup; the digest carries no links.
    """
    totals = digest.get("totals", {}) or {}
    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Per-reviewer Feedback Digest</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;",
        "margin:0;background:#f5f7fa;color:#1f2933;line-height:1.5}",
        "main{max-width:900px;margin:0 auto;padding:24px}",
        "h1{font-size:1.6rem}h2{font-size:1.05rem;margin:0 0 6px}",
        ".card{background:#fff;border:1px solid #e4e7eb;border-radius:10px;",
        "padding:16px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}",
        ".metrics{color:#52606d;font-size:.85rem;margin:4px 0}",
        "ul{margin:6px 0;padding-left:20px}",
        "footer{color:#7b8794;font-size:.8rem;padding:24px 0}",
        "</style></head><body><main>",
        "<h1>Per-reviewer Feedback Digest</h1>",
        '<p class="metrics">Generated %s</p>' % _esc(digest.get("generated_at", "")),
    ]
    if digest.get("reviewer"):
        parts.append(
            '<p class="metrics">Filtered to reviewer: %s</p>'
            % _esc(digest["reviewer"])
        )
    parts.append(
        '<p class="metrics">%d reviewers &middot; %d active clusters '
        "&middot; %d assigned &middot; %d unassigned &middot; %d decisions</p>"
        % (
            totals.get("reviewers", 0),
            totals.get("active_clusters", 0),
            totals.get("assigned_clusters", 0),
            totals.get("unassigned_clusters", 0),
            totals.get("recent_decisions", 0),
        )
    )
    parts.append(
        '<p class="metrics"><a href="/api/digest">JSON</a> &middot; '
        '<a href="/digest.txt">text</a></p>'
    )

    parts.append('<section class="card"><h2>Reviewers</h2>')
    reviewers = digest.get("reviewers", []) or []
    if reviewers:
        for reviewer in reviewers:
            parts.append(
                "<p><strong>%s</strong> "
                '<span class="metrics">(%s) &middot; %d assigned &middot; '
                "%d open items</span></p>"
                % (
                    _esc(reviewer.get("name", "")),
                    _esc(reviewer.get("id", "")),
                    reviewer.get("assignment_count", 0),
                    reviewer.get("open_items", 0),
                )
            )
    else:
        parts.append('<p class="metrics">No reviewers.</p>')
    parts.append("</section>")

    parts.append('<section class="card"><h2>Assignments</h2>')
    assignments = digest.get("assignments", []) or []
    if assignments:
        parts.append("<ul>")
        for entry in assignments:
            parts.append(
                "<li>%s &rarr; %s "
                '<span class="metrics">(%d open items)</span></li>'
                % (
                    _esc(entry.get("cluster_label", "")),
                    _esc(entry.get("reviewer_name", "") or "unassigned"),
                    entry.get("open_items", 0),
                )
            )
        parts.append("</ul>")
    else:
        parts.append('<p class="metrics">No assigned clusters.</p>')
    parts.append("</section>")

    parts.append(
        '<section class="card"><h2>Clusters with no reviewer activity</h2>'
    )
    unassigned = digest.get("unassigned_clusters", []) or []
    if unassigned:
        parts.append("<ul>")
        for entry in unassigned:
            parts.append(
                "<li>%s "
                '<span class="metrics">(%d open items)</span></li>'
                % (_esc(entry.get("cluster_label", "")), entry.get("open_items", 0))
            )
        parts.append("</ul>")
    else:
        parts.append('<p class="metrics">None.</p>')
    parts.append("</section>")

    parts.append(
        '<section class="card"><h2>Suggested balancing order</h2>'
        '<p class="metrics">Least-loaded reviewer first; ties by name then id. '
        "Advisory only &mdash; applying it never writes to the workspace.</p>"
    )
    suggested = digest.get("suggested_assignments", []) or []
    if suggested:
        parts.append("<ul>")
        for entry in suggested:
            parts.append(
                "<li>%s &rarr; %s "
                '<span class="metrics">(%d open items)</span></li>'
                % (
                    _esc(entry.get("cluster_label", "")),
                    _esc(entry.get("reviewer_name", "")),
                    entry.get("open_items", 0),
                )
            )
        parts.append("</ul>")
    else:
        parts.append('<p class="metrics">None.</p>')
    parts.append("</section>")

    parts.append('<section class="card"><h2>Recent decisions</h2>')
    decisions = digest.get("recent_decisions", []) or []
    if decisions:
        for decision in decisions:
            parts.append(
                '<p class="metrics"><strong>%s</strong> &middot; %s &middot; %s</p>'
                % (
                    _esc(decision.get("action", "")),
                    _esc(decision.get("reviewer", "") or "unreviewed"),
                    _esc(decision.get("timestamp", "")),
                )
            )
            clusters = decision.get("clusters") or []
            if clusters:
                parts.append(
                    '<p class="metrics">Clusters: %s</p>'
                    % _esc(", ".join(clusters))
                )
            if decision.get("note"):
                parts.append("<p>%s</p>" % _esc(decision["note"]))
    else:
        parts.append('<p class="metrics">None.</p>')
    parts.append("</section>")

    parts.append(
        "<footer>Generated by Feedback Triage Copilot. Read-only digest; "
        "the workspace is never modified.</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts)


# -- internals -----------------------------------------------------------
def _find_reviewer(store, value: str) -> Optional[Reviewer]:
    """Resolve a reviewer id or name (case-insensitive) to a stored reviewer."""
    if not value:
        return None
    for candidate in store.list_reviewers():
        if candidate.id == value:
            return candidate
    lowered = value.lower()
    for candidate in store.list_reviewers():
        if candidate.name.lower() == lowered:
            return candidate
    return None


def _resolve_reviewer(store, value: str) -> tuple:
    reviewer = _find_reviewer(store, value)
    if reviewer is not None:
        return reviewer.id, reviewer.name
    return "", value


def _belongs(reviewer_id: str, reviewer_name: str, candidate: Reviewer) -> bool:
    if reviewer_id and reviewer_id == candidate.id:
        return True
    return bool(reviewer_name) and reviewer_name.lower() == candidate.name.lower()


def _matches(filter_value: str, reviewer_id: str, reviewer_name: str) -> bool:
    if not filter_value:
        return True
    lowered = filter_value.lower()
    return (
        bool(reviewer_id) and reviewer_id.lower() == lowered
    ) or (
        bool(reviewer_name) and reviewer_name.lower() == lowered
    )


def _event_cluster_ids(event: EditEvent) -> List[str]:
    details = event.details or {}
    cluster_ids: List[str] = []
    if details.get("cluster_id"):
        cluster_ids.append(str(details["cluster_id"]))
    if details.get("source"):
        cluster_ids.append(str(details["source"]))
    for source_id in details.get("sources", []) or []:
        cluster_ids.append(str(source_id))
    return cluster_ids


def _owners(store) -> Dict[str, str]:
    """Map active cluster id to the reviewer of its most recent decision."""
    active_ids = {cluster.id for cluster in store.active_clusters()}
    owners: Dict[str, str] = {}
    for event in store.history:
        if not event.reviewer:
            continue
        for cluster_id in _event_cluster_ids(event):
            if cluster_id in active_ids:
                owners[cluster_id] = event.reviewer
    return owners


def _recent_decisions(store) -> List[Dict[str, object]]:
    labels_by_id = {cluster.id: cluster.label for cluster in store.clusters}
    decisions: List[Dict[str, object]] = []
    for event in reversed(list(store.history)):
        reviewer_id, reviewer_name = _resolve_reviewer(store, event.reviewer or "")
        decisions.append({
            "action": event.action,
            "timestamp": event.timestamp,
            "reviewer_id": reviewer_id,
            "reviewer": reviewer_name,
            "note": event.note or "",
            "clusters": _affected_labels(event, labels_by_id),
        })
        if len(decisions) >= DECISION_LIMIT:
            break
    return decisions


def _find_cluster(clusters: Sequence[Cluster], cluster_id: object
                  ) -> Optional[Cluster]:
    for cluster in clusters:
        if cluster.id == cluster_id:
            return cluster
    return None


def _text_safe(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return str(_csv_safe(text))
