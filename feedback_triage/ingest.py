"""CSV ingestion with tolerant column auto-detection.

Support exports and app-review exports rarely share column names, so the
importer maps a set of common aliases onto the canonical fields.
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Dict, List, Optional, Sequence

from .models import FeedbackItem


TEXT_ALIASES = (
    "text", "comment", "comments", "feedback", "message", "body", "content",
    "review", "reviewtext", "review_text", "description", "note", "notes",
    "ticket", "subject", "kommentar",
)
SEGMENT_ALIASES = (
    "segment", "plan", "tier", "customer_segment", "customer", "account",
    "company_size", "customer_type", "plan_name", "product_plan",
    "kundensegment",
)
SOURCE_ALIASES = ("source", "channel", "origin", "provider", "app", "platform")
URL_ALIASES = ("url", "link", "source_url", "permalink", "ticket_url", "review_url")
DATE_ALIASES = (
    "date", "created_at", "created", "timestamp", "time", "review_date",
    "submitted_at",
)
RATING_ALIASES = ("rating", "score", "stars", "star_rating", "nps", "csat")
LANGUAGE_ALIASES = ("language", "lang", "locale")
ID_ALIASES = ("id", "feedback_id", "ticket_id", "review_id", "external_id")


def _normalise(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


FIELD_ALIASES = (
    ("text", TEXT_ALIASES),
    ("segment", SEGMENT_ALIASES),
    ("source", SOURCE_ALIASES),
    ("url", URL_ALIASES),
    ("created_at", DATE_ALIASES),
    ("rating", RATING_ALIASES),
    ("language", LANGUAGE_ALIASES),
    ("id", ID_ALIASES),
)


def _exact_match(normalised: Dict[str, str], aliases: Sequence[str]) -> Optional[str]:
    for alias in aliases:
        if alias in normalised:
            return normalised[alias]
    return None


def _substring_match(
    normalised: Dict[str, str], aliases: Sequence[str], claimed: set
) -> Optional[str]:
    # fall back to a substring match so "review body" still resolves to text
    for norm, original in normalised.items():
        if original in claimed:
            continue
        for alias in aliases:
            if alias in norm:
                return original
    return None


def detect_mapping(fieldnames: Sequence[str]) -> Dict[str, Optional[str]]:
    """Return a canonical-field -> csv-column mapping.

    Exact alias matches are resolved for every canonical field before any
    substring fallback runs, and a column that is already claimed is never
    reused. Without this ordering an ``*_id`` column that happens to appear
    before the text column (for example ``review_id`` ahead of ``review_body``)
    would be claimed for ``text`` via the ``review`` alias and the real feedback
    text would be silently dropped.
    """
    normalised = {_normalise(f): f for f in fieldnames}
    mapping: Dict[str, Optional[str]] = {}
    claimed: set = set()
    for field, aliases in FIELD_ALIASES:
        found = _exact_match(normalised, aliases)
        mapping[field] = found
        if found is not None:
            claimed.add(found)
    for field, aliases in FIELD_ALIASES:
        if mapping[field] is None:
            found = _substring_match(normalised, aliases, claimed)
            if found is not None:
                mapping[field] = found
                claimed.add(found)
    return mapping


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(
    content: str,
    source: str = "manual",
    mapping: Optional[Dict[str, Optional[str]]] = None,
) -> List[FeedbackItem]:
    """Parse CSV text into :class:`FeedbackItem` objects.

    ``mapping`` overrides individual canonical fields (or supplies explicit CSV
    column names). Rows without usable text are skipped.
    """
    if not content or not content.strip():
        return []

    delimiter = _sniff_delimiter(content[:4096])
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    fieldnames = reader.fieldnames or []
    detected = detect_mapping(fieldnames)
    if mapping:
        for key, value in mapping.items():
            if value:
                detected[key] = value

    text_col = detected.get("text")
    if not text_col:
        raise ValueError(
            "No text column detected. Columns found: %s" % ", ".join(fieldnames)
        )

    items: List[FeedbackItem] = []
    for row in reader:
        text = (row.get(text_col) or "").strip()
        if not text:
            continue
        items.append(_row_to_item(row, detected, source, text))
    return items


def _clean(row: Dict[str, str], column: Optional[str]) -> str:
    if not column:
        return ""
    return (row.get(column) or "").strip()


def _row_to_item(
    row: Dict[str, str],
    mapping: Dict[str, Optional[str]],
    default_source: str,
    text: str,
) -> FeedbackItem:
    source = _clean(row, mapping.get("source")) or default_source
    segment = _clean(row, mapping.get("segment")) or "unknown"
    rating = _parse_rating(_clean(row, mapping.get("rating")))
    item_id = _clean(row, mapping.get("id")) or uuid.uuid4().hex[:12]
    return FeedbackItem(
        id=item_id,
        text=text,
        source=source,
        segment=segment,
        url=_clean(row, mapping.get("url")),
        created_at=_clean(row, mapping.get("created_at")),
        rating=rating,
        language=_clean(row, mapping.get("language")),
    )


def _parse_rating(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def load_csv_file(path: str, source: str = "manual") -> List[FeedbackItem]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return parse_csv(handle.read(), source=source)
