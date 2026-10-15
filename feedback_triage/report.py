"""Weekly report aggregation and CSV / HTML / PDF rendering.

The report is intentionally descriptive, not prescriptive: it shows frequencies,
segment/source distributions, evidence and uncertainty, but never an opaque
priority score.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Sequence

from .cluster import build_tfidf, representative_quotes, tokenize
from .models import ACTIVE, Cluster, EditEvent, FeedbackItem

#: How many of the most recent decisions the exported log keeps.
DECISION_LIMIT = 50


def _distribution(values: Sequence[str]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for value in values:
        key = value or "unknown"
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items(), key=lambda kv: kv[1], reverse=True))


def build_report(
    store, reviewer: str = "", note: str = ""
) -> Dict[str, object]:
    """Build the weekly report dictionary from a :class:`TriageStore`.

    ``reviewer`` and ``note`` are optional provenance for the report itself
    (e.g. the CLI ``report --reviewer`` header); they do not modify history.
    """
    items = store.items
    items_by_id = store.items_by_id()
    vectors = {
        item.id: vector
        for item, vector in zip(items, build_tfidf([tokenize(i.text) for i in items]))
    }
    active = [c for c in store.clusters if c.status == ACTIVE]
    active.sort(key=lambda c: len(c.item_ids), reverse=True)

    total_items = len(items)
    cluster_entries: List[Dict[str, object]] = []
    uncertain = 0
    for cluster in active:
        if cluster.uncertainty:
            uncertain += 1
        cluster_entries.append(_cluster_entry(
            cluster, items_by_id, vectors, total_items
        ))

    labels_by_id = {c.id: c.label for c in store.clusters}
    reviewers_by_id = {r.id: r.name for r in getattr(store, "reviewers", [])}

    report: Dict[str, object] = {
        "generated_at": _now(),
        "reviewer": reviewer or "",
        "note": note or "",
        "totals": {
            "items": total_items,
            "clusters": len(active),
            "discarded_clusters": len(store.clusters) - len(active),
            "uncertain_clusters": uncertain,
            "sources": _distribution([i.source for i in items]),
            "segments": _distribution([i.segment for i in items]),
        },
        "clusters": cluster_entries,
        "decisions": _decision_log(store.history, reviewers_by_id, labels_by_id),
    }
    return report


def _decision_log(
    history: Sequence[EditEvent],
    reviewers_by_id: Dict[str, str],
    labels_by_id: Dict[str, str],
) -> List[Dict[str, object]]:
    """Return the most recent decisions, newest first."""
    decisions: List[Dict[str, object]] = []
    for event in reversed(list(history)):
        reviewer = event.reviewer or ""
        if reviewer in reviewers_by_id:
            reviewer = reviewers_by_id[reviewer]
        decisions.append({
            "action": event.action,
            "timestamp": event.timestamp,
            "reviewer": reviewer,
            "note": event.note or "",
            "clusters": _affected_labels(event, labels_by_id),
        })
        if len(decisions) >= DECISION_LIMIT:
            break
    return decisions


def _affected_labels(
    event: EditEvent, labels_by_id: Dict[str, str]
) -> List[str]:
    details = event.details or {}
    cluster_ids: List[str] = []
    if details.get("cluster_id"):
        cluster_ids.append(str(details["cluster_id"]))
    if details.get("source"):
        cluster_ids.append(str(details["source"]))
    for source_id in details.get("sources", []) or []:
        cluster_ids.append(str(source_id))
    labels: List[str] = []
    for cluster_id in cluster_ids:
        label = labels_by_id.get(cluster_id)
        if label and label not in labels:
            labels.append(label)
    if event.action == "rename":
        new_label = details.get("to")
        if new_label and new_label not in labels:
            labels.append(str(new_label))
    return labels


def _now() -> str:
    from .models import now_iso

    return now_iso()


def _cluster_entry(
    cluster: Cluster,
    items_by_id: Dict[str, FeedbackItem],
    vectors: Dict[str, Dict[str, float]],
    total_items: int,
) -> Dict[str, object]:
    member_items = [items_by_id[i] for i in cluster.item_ids if i in items_by_id]
    size = len(member_items)
    quotes = representative_quotes(cluster.item_ids, items_by_id, vectors, limit=3)
    dates = sorted(i.created_at for i in member_items if i.created_at)
    return {
        "id": cluster.id,
        "label": cluster.label,
        "status": cluster.status,
        "origin": cluster.origin,
        "size": size,
        "share": round(size / total_items, 4) if total_items else 0.0,
        "cohesion": cluster.cohesion,
        "separation": cluster.separation,
        "uncertainty": list(cluster.uncertainty),
        "segments": _distribution([i.segment for i in member_items]),
        "sources": _distribution([i.source for i in member_items]),
        "first_seen": dates[0] if dates else "",
        "last_seen": dates[-1] if dates else "",
        "quotes": [
            {
                "id": q.id,
                "text": q.text,
                "source": q.source,
                "segment": q.segment,
                "url": q.url,
                "created_at": q.created_at,
            }
            for q in quotes
        ],
    }


def report_to_csv(report: Dict[str, object]) -> str:
    """One row per evidence quote, with cluster-level context repeated.

    Untrusted cells are neutralised against spreadsheet formula injection:
    a value starting with ``= + - @`` (or a leading tab/CR) is prefixed with
    ``'`` so Excel/Sheets/LibreOffice treat it as text.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "cluster_id", "cluster_label", "cluster_size", "cluster_share",
        "uncertainty", "cohesion", "separation", "quote_id", "quote_text",
        "quote_source", "quote_segment", "quote_url", "quote_created_at",
        "decision_timestamp", "decision_action", "decision_reviewer",
        "decision_note", "decision_clusters",
    ])
    empty_decision = ["", "", "", "", ""]
    for cluster in report["clusters"]:  # type: ignore[index]
        quotes = cluster["quotes"] or [{}]  # type: ignore[index]
        for quote in quotes:
            row = [
                cluster["id"], cluster["label"], cluster["size"],
                cluster["share"], "|".join(cluster["uncertainty"]),  # type: ignore[arg-type]
                cluster["cohesion"], cluster["separation"],
                quote.get("id", ""), quote.get("text", ""),
                quote.get("source", ""), quote.get("segment", ""),
                quote.get("url", ""), quote.get("created_at", ""),
            ] + empty_decision
            writer.writerow([_csv_safe(cell) for cell in row])
    for decision in report.get("decisions", []):  # type: ignore[union-attr]
        row = ["", "", "", "", "", "", "", "", "", "", "", "", ""] + [
            decision.get("timestamp", ""),
            decision.get("action", ""),
            decision.get("reviewer", ""),
            decision.get("note", ""),
            "|".join(decision.get("clusters", [])),  # type: ignore[arg-type]
        ]
        writer.writerow([_csv_safe(cell) for cell in row])
    return buffer.getvalue()


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Prefix formula-like strings so spreadsheets do not execute them."""
    if isinstance(value, str) and value[:1] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def report_to_html(report: Dict[str, object]) -> str:
    totals = report["totals"]  # type: ignore[index]
    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Weekly Feedback Triage Report</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;",
        "margin:0;background:#f5f7fa;color:#1f2933;line-height:1.5}",
        "main{max-width:900px;margin:0 auto;padding:24px}",
        "h1{font-size:1.6rem}h2{font-size:1.05rem;margin:0 0 6px}",
        ".card{background:#fff;border:1px solid #e4e7eb;border-radius:10px;",
        "padding:16px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}",
        ".metrics{color:#52606d;font-size:.85rem;margin:4px 0}",
        ".flag{display:inline-block;background:#fff3cd;color:#8a6100;",
        "border-radius:999px;padding:1px 8px;font-size:.75rem;margin-right:4px}",
        "blockquote{margin:8px 0;padding:8px 12px;background:#f0f4f8;",
        "border-left:3px solid #9aa5b1;border-radius:0 6px 6px 0}",
        "a{color:#2563eb;word-break:break-all}",
        ".decision{border-left:3px solid #2563eb;padding:6px 12px;margin:8px 0;",
        "background:#f0f4f8;border-radius:0 6px 6px 0}",
        "footer{color:#7b8794;font-size:.8rem;padding:24px 0}",
        "</style></head><body><main>",
        "<h1>Weekly Feedback Triage Report</h1>",
        '<p class="metrics">Generated %s</p>' % _esc(report["generated_at"]),  # type: ignore[index]
        '<p class="metrics">%d items &middot; %d topics &middot; '
        "%d uncertain &middot; %d discarded</p>"
        % (
            totals["items"], totals["clusters"],  # type: ignore[index]
            totals["uncertain_clusters"], totals["discarded_clusters"],  # type: ignore[index]
        ),
    ]
    if report.get("reviewer"):  # type: ignore[union-attr]
        parts.append(
            '<p class="metrics">Prepared by %s</p>' % _esc(report["reviewer"])  # type: ignore[index]
        )
    if report.get("note"):  # type: ignore[union-attr]
        parts.append('<p class="metrics">%s</p>' % _esc(report["note"]))  # type: ignore[index]
    for cluster in report["clusters"]:  # type: ignore[index]
        parts.append('<section class="card">')
        parts.append("<h2>%s</h2>" % _esc(cluster["label"]))
        flags = "".join(
            '<span class="flag">%s</span>' % _esc(flag)
            for flag in cluster["uncertainty"]  # type: ignore[union-attr]
        )
        if flags:
            parts.append('<p class="metrics">%s</p>' % flags)
        parts.append(
            '<p class="metrics">%d quotes (%.0f%%) &middot; cohesion %.2f '
            "&middot; separation %.2f</p>"
            % (
                cluster["size"], cluster["share"] * 100,
                cluster["cohesion"], cluster["separation"],
            )
        )
        parts.append(
            '<p class="metrics">Segments: %s &middot; Sources: %s</p>'
            % (
                _esc(_fmt_dist(cluster["segments"])),
                _esc(_fmt_dist(cluster["sources"])),
            )
        )
        for quote in cluster["quotes"]:  # type: ignore[union-attr]
            url = _safe_url(quote.get("url"))
            link = (
                ' <a href="%s" rel="noopener noreferrer">source</a>' % _esc(url)
                if url
                else ""
            )
            parts.append(
                "<blockquote>%s<br><small>%s &middot; %s%s</small></blockquote>"
                % (
                    _esc(quote["text"]), _esc(quote.get("source", "")),
                    _esc(quote.get("segment", "")), link,
                )
            )
        parts.append("</section>")
    parts.extend(_decision_log_html(report.get("decisions", [])))  # type: ignore[arg-type]
    parts.append(
        "<footer>Generated by Feedback Triage Copilot. Evidence is mandatory; "
        "no opaque priority score is applied.</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def _decision_log_html(decisions: Sequence[Dict[str, object]]) -> List[str]:
    if not decisions:
        return []
    parts: List[str] = ["<section class=\"card\"><h2>Decision log</h2>"]
    for decision in decisions:
        reviewer = _esc(decision.get("reviewer") or "unreviewed")
        clusters = decision.get("clusters") or []
        cluster_line = (
            "<br><small>Clusters: %s</small>" % _esc(", ".join(clusters))
            if clusters
            else ""
        )
        note = decision.get("note")
        note_line = "<br>%s" % _esc(note) if note else ""
        parts.append(
            '<div class="decision"><strong>%s</strong> &middot; %s'
            "<br><small>%s</small>%s%s</div>"
            % (
                _esc(decision.get("action")),
                reviewer,
                _esc(decision.get("timestamp")),
                cluster_line,
                note_line,
            )
        )
    parts.append("</section>")
    return parts


def _fmt_dist(dist: Dict[str, int]) -> str:
    return ", ".join("%s: %d" % (k, v) for k, v in dist.items()) or "none"


def _esc(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _safe_url(value: object) -> str:
    """Return ``value`` only when it is an http(s) URL, else an empty string.

    Untrusted feedback may supply ``javascript:`` or ``data:`` links; dropping
    them prevents script URLs in rendered anchors.
    """
    text = "" if value is None else str(value).strip()
    return text if _SAFE_URL_RE.match(text) else ""


def report_to_pdf(report: Dict[str, object]) -> bytes:
    """Render the report as a minimal, valid PDF (Helvetica, A4)."""
    lines = _pdf_lines(report)
    return SimplePdf().render(lines)


def _pdf_lines(report: Dict[str, object]) -> List[tuple]:
    totals = report["totals"]  # type: ignore[index]
    styles: List[tuple] = [
        ("Feedback Triage Copilot", 18, True),
        ("Weekly Feedback Report - %s" % report["generated_at"], 10, False),  # type: ignore[index]
        (
            "%d items, %d topics, %d uncertain, %d discarded"
            % (
                totals["items"], totals["clusters"],  # type: ignore[index]
                totals["uncertain_clusters"], totals["discarded_clusters"],  # type: ignore[index]
            ),
            10,
            False,
        ),
        ("", 10, False),
    ]
    for cluster in report["clusters"]:  # type: ignore[index]
        styles.append(
            (
                "%s  (%d quotes, %.0f%%, cohesion %.2f, separation %.2f)"
                % (
                    cluster["label"], cluster["size"], cluster["share"] * 100,
                    cluster["cohesion"], cluster["separation"],
                ),
                13,
                True,
            )
        )
        if cluster["uncertainty"]:  # type: ignore[union-attr]
            styles.append(
                (
                    "Flags: %s" % ", ".join(cluster["uncertainty"]),  # type: ignore[arg-type]
                    9,
                    False,
                )
            )
        styles.append(
            (
                "Segments: %s | Sources: %s"
                % (_fmt_dist(cluster["segments"]), _fmt_dist(cluster["sources"])),
                9,
                False,
            )
        )
        for quote in cluster["quotes"]:  # type: ignore[union-attr]
            styles.append(('"%s"' % quote["text"], 10, False))
            meta = "  - %s, %s" % (quote.get("source", ""), quote.get("segment", ""))
            if quote.get("url"):
                meta += " (%s)" % quote["url"]
            styles.append((meta, 9, False))
        styles.append(("", 10, False))
    decisions = report.get("decisions", [])  # type: ignore[union-attr]
    if decisions:
        styles.append(("Decision log", 13, True))
        for decision in decisions:  # type: ignore[union-attr]
            line = "%s  %s  by %s" % (
                decision.get("timestamp", ""),
                decision.get("action", ""),
                decision.get("reviewer") or "unreviewed",
            )
            clusters = decision.get("clusters") or []
            if clusters:
                line += "  [%s]" % ", ".join(clusters)  # type: ignore[arg-type]
            styles.append((line, 9, False))
            if decision.get("note"):
                styles.append(("  note: %s" % decision["note"], 9, False))
        styles.append(("", 10, False))
    return styles


class SimplePdf:
    """A tiny pure-stdlib PDF writer good enough for a text report."""

    PAGE_WIDTH = 595
    PAGE_HEIGHT = 842
    MARGIN = 50

    def render(self, styled_lines: Sequence[tuple]) -> bytes:
        pages = self._paginate(styled_lines)
        return self._assemble(pages)

    def _paginate(self, styled_lines: Sequence[tuple]) -> List[List[str]]:
        max_width = self.PAGE_WIDTH - 2 * self.MARGIN
        pages: List[List[str]] = []
        current: List[str] = []
        y = self.PAGE_HEIGHT - self.MARGIN
        for text, size, bold in styled_lines:
            wrapped = self._wrap(text, size, bold, max_width)
            for line in wrapped:
                if y < self.MARGIN:
                    pages.append(current)
                    current = []
                    y = self.PAGE_HEIGHT - self.MARGIN
                font = "F2" if bold else "F1"
                current.append("BT /%s %d Tf %d %d Td (%s) Tj ET" % (
                    font, size, self.MARGIN, y, self._escape(line)))
                y -= int(size * 1.5)
        pages.append(current)
        return pages

    def _wrap(
        self, text: str, size: int, bold: bool, max_width: float
    ) -> List[str]:
        if not text:
            return [""]
        approx_char = size * (0.55 if bold else 0.5)
        max_chars = max(10, int(max_width / approx_char))
        words = text.split()
        lines: List[str] = []
        line = ""
        for word in words:
            candidate = word if not line else line + " " + word
            if len(candidate) <= max_chars:
                line = candidate
            else:
                if line:
                    lines.append(line)
                while len(word) > max_chars:
                    lines.append(word[:max_chars])
                    word = word[max_chars:]
                line = word
        lines.append(line)
        return lines

    def _escape(self, text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .encode("latin-1", "replace")
            .decode("latin-1")
        )

    def _assemble(self, pages: List[List[str]]) -> bytes:
        objects: List[bytes] = []

        def add(obj: bytes) -> int:
            objects.append(obj)
            return len(objects)

        font_regular = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        )
        font_bold = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
        )
        font_regular_obj = font_regular
        font_bold_obj = font_bold

        pages_obj_number = add(b"")  # placeholder
        page_numbers: List[int] = []
        for page_lines in pages:
            stream = "\n".join(page_lines).encode("latin-1", "replace")
            content_number = add(
                b"<< /Length %d >>\nstream\n%s\nendstream"
                % (len(stream), stream)
            )
            page_number = add(
                (
                    "<< /Type /Page /Parent %d 0 R "
                    "/MediaBox [0 0 %d %d] "
                    "/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> "
                    "/Contents %d 0 R >>"
                    % (
                        pages_obj_number, self.PAGE_WIDTH, self.PAGE_HEIGHT,
                        font_regular_obj, font_bold_obj, content_number,
                    )
                ).encode("ascii")
            )
            page_numbers.append(page_number)

        kids = " ".join("%d 0 R" % n for n in page_numbers)
        objects[pages_obj_number - 1] = (
            "<< /Type /Pages /Kids [%s] /Count %d >>"
            % (kids, len(page_numbers))
        ).encode("ascii")

        catalog_number = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj_number)

        buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(buffer))
            buffer += ("%d 0 obj\n" % index).encode("ascii")
            buffer += obj
            buffer += b"\nendobj\n"
        xref_offset = len(buffer)
        buffer += ("xref\n0 %d\n" % (len(objects) + 1)).encode("ascii")
        buffer += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            buffer += ("%010d 00000 n \n" % offset).encode("ascii")
        buffer += (
            "trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog_number, xref_offset)
        ).encode("ascii")
        return bytes(buffer)
