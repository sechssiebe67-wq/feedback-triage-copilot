"""WSGI application: dashboard, JSON API, exports and health check.

Runs on the standard library only, so it can be served directly with
``wsgiref`` or behind any WSGI host. The same object is used by the tests.
"""

from __future__ import annotations

import html
import json
import re
from typing import Dict, Iterable, List, Tuple
from urllib.parse import parse_qs, unquote

from .ingest import parse_csv
from .models import ACTIVE
from .report import (
    build_digest,
    build_queue,
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from .store import TriageStore


CLUSTER_ACTION = re.compile(
    r"^/api/clusters/([^/]+)/(discard|restore|rename|split|claim|release|resolve)$"
)
SOURCE_ACTION = re.compile(r"^/api/sources/([^/]+)/(enable|disable|run)$")


class FeedbackTriageApp:
    def __init__(self, store: TriageStore) -> None:
        self.store = store

    # WSGI entry point ---------------------------------------------------
    def __call__(self, environ, start_response) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        try:
            status, headers, body = self._dispatch(method, path, environ)
        except KeyError as exc:
            status, headers, body = self._json(404, {"error": str(exc)})
        except ValueError as exc:
            status, headers, body = self._json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - safety net
            status, headers, body = self._json(500, {"error": str(exc)})
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers.setdefault("Content-Length", str(len(body)))
        start_response(status, list(headers.items()))
        return [body]

    def _dispatch(
        self, method: str, path: str, environ: Dict[str, object]
    ) -> Tuple[str, Dict[str, str], object]:
        if path == "/" and method == "GET":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            reviewer = (query.get("reviewer") or [""])[0].strip()
            return self._html(200, self._dashboard(reviewer))
        if path == "/healthz" and method == "GET":
            return self._json(200, self._health())
        if path == "/api/digest" and method == "GET":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            reviewer = (query.get("reviewer") or [""])[0].strip()
            if not reviewer:
                return self._json(400, {"error": "reviewer is required"})
            try:
                limit = int((query.get("limit") or ["10"])[0])
            except ValueError:
                limit = 10
            return self._json(
                200, build_digest(self.store, reviewer, limit=limit)
            )
        if path == "/api/queue" and method == "GET":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            reviewer = (query.get("reviewer") or [""])[0].strip()
            if not reviewer:
                return self._json(400, {"error": "reviewer is required"})
            return self._json(200, build_queue(self.store, reviewer))
        if path == "/api/report" and method == "GET":
            return self._json(200, build_report(self.store))
        if path == "/api/clusters" and method == "GET":
            clusters = [c.to_dict() for c in self.store.clusters]
            return self._json(200, {"clusters": clusters})
        if path == "/api/items" and method == "GET":
            items = [i.to_dict() for i in self.store.items]
            return self._json(200, {"items": items})
        if path == "/api/history" and method == "GET":
            history = [h.to_dict() for h in self.store.history]
            return self._json(200, {"history": history})
        if path == "/api/reviewers" and method == "GET":
            reviewers = [r.to_dict() for r in self.store.list_reviewers()]
            return self._json(200, {"reviewers": reviewers})
        if path == "/api/reviewers" and method == "POST":
            payload = self._payload(environ)
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("name is required")
            reviewer = self.store.add_reviewer(name)
            return self._json(200, {"ok": True, "reviewer": reviewer.to_dict()})
        if path == "/api/sources" and method == "GET":
            sources = [s.to_dict() for s in self.store.list_sources()]
            return self._json(200, {"sources": sources})
        if path == "/api/sources" and method == "POST":
            return self._create_source(environ)
        if path == "/api/ingest-runs" and method == "GET":
            query = parse_qs(str(environ.get("QUERY_STRING", "")))
            try:
                limit = int((query.get("limit") or ["0"])[0])
            except ValueError:
                limit = 0
            runs = list(self.store.ingest_runs)
            if limit > 0:
                runs = runs[-limit:]
            return self._json(
                200, {"ingest_runs": [r.to_dict() for r in runs]}
            )
        if path == "/api/ingest" and method == "POST":
            payload = self._payload(environ)
            runs = self.store.run_all_sources(
                reviewer=str(payload.get("reviewer", "") or ""),
                note=str(payload.get("note", "") or ""),
            )
            return self._json(200, {
                "added": sum(r.added for r in runs),
                "skipped": sum(r.skipped for r in runs),
                "runs": [r.to_dict() for r in runs],
            })
        if path == "/report.csv" and method == "GET":
            return (
                "200 OK",
                {"Content-Type": "text/csv; charset=utf-8",
                 "Content-Disposition": "attachment; filename=feedback-report.csv"},
                report_to_csv(build_report(self.store)).encode("utf-8"),
            )
        if path == "/report.pdf" and method == "GET":
            return (
                "200 OK",
                {"Content-Type": "application/pdf",
                 "Content-Disposition": "attachment; filename=feedback-report.pdf"},
                report_to_pdf(build_report(self.store)),
            )
        if path == "/report.html" and method == "GET":
            return self._html(200, report_to_html(build_report(self.store)))
        if path == "/api/import" and method == "POST":
            return self._import(environ)
        if path == "/api/recluster" and method == "POST":
            payload = self._payload(environ)
            self.store.recluster(
                reviewer=str(payload.get("reviewer", "") or ""),
                note=str(payload.get("note", "") or ""),
            )
            return self._json(200, build_report(self.store))
        if path == "/api/merge" and method == "POST":
            payload = self._payload(environ)
            cluster_ids = payload.get("cluster_ids") or []
            if not isinstance(cluster_ids, list) or len(cluster_ids) < 2:
                raise ValueError("cluster_ids must contain at least two clusters")
            merged = self.store.merge(
                cluster_ids,
                reviewer=str(payload.get("reviewer", "") or ""),
                note=str(payload.get("note", "") or ""),
            )
            return self._json(200, {"ok": True, "cluster_id": merged.id})

        action = CLUSTER_ACTION.match(path)
        if action and method == "POST":
            return self._cluster_action(action.group(1), action.group(2), environ)
        source_action = SOURCE_ACTION.match(path)
        if source_action and method == "POST":
            return self._source_action(
                source_action.group(1), source_action.group(2), environ
            )
        return self._json(404, {"error": "Not found: %s" % path})

    # handlers -----------------------------------------------------------
    def _health(self) -> Dict[str, object]:
        return {
            "status": "ok",
            "items": len(self.store.items),
            "clusters": len(self.store.active_clusters()),
        }

    def _import(self, environ: Dict[str, object]) -> Tuple[str, Dict[str, str], object]:
        body = self._read_body(environ)
        query = parse_qs(str(environ.get("QUERY_STRING", "")))
        source = (query.get("source") or ["import"])[0]
        mapping = {}
        for key in ("text", "segment", "source", "url", "created_at", "rating"):
            if key in query:
                mapping[key] = query[key][0]
        reviewer = (query.get("reviewer") or [""])[0]
        note = (query.get("note") or [""])[0]
        content_type = str(environ.get("CONTENT_TYPE", ""))
        if "application/json" in content_type:
            payload = json.loads(body or "{}")
            content = payload.get("csv", "")
            source = payload.get("source", source)
            mapping.update(payload.get("mapping", {}) or {})
            reviewer = payload.get("reviewer", reviewer)
            note = payload.get("note", note)
        else:
            content = body
        items = parse_csv(content, source=source, mapping=mapping or None)
        added = self.store.add_items(
            items, reviewer=str(reviewer or ""), note=str(note or "")
        )
        report = build_report(self.store)
        return self._json(200, {"added": added, "total": len(self.store.items),
                                "clusters": report["totals"]["clusters"]})

    def _create_source(
        self, environ: Dict[str, object]
    ) -> Tuple[str, Dict[str, str], object]:
        payload = self._payload(environ)
        name = str(payload.get("name", "") or "").strip()
        path = str(payload.get("path", "") or "").strip()
        if not name:
            raise ValueError("name is required")
        if not path:
            raise ValueError("path is required")
        enabled = payload.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("false", "0", "no", "")
        source = self.store.add_source(
            name,
            path,
            glob=str(payload.get("glob", "") or ""),
            source_label=str(
                payload.get("label", payload.get("source_label", "")) or ""
            ),
            segment_default=str(
                payload.get("segment", payload.get("segment_default", "")) or ""
            ),
            enabled=bool(enabled),
        )
        return self._json(200, {"ok": True, "source": source.to_dict()})

    def _source_action(
        self, source_id: str, action: str, environ: Dict[str, object]
    ) -> Tuple[str, Dict[str, str], object]:
        source_id = unquote(source_id)
        payload = self._payload(environ)
        reviewer = str(payload.get("reviewer", "") or "")
        note = str(payload.get("note", "") or "")
        if action == "enable":
            source = self.store.set_source_enabled(source_id, True)
            return self._json(200, {"ok": True, "source": source.to_dict()})
        if action == "disable":
            source = self.store.set_source_enabled(source_id, False)
            return self._json(200, {"ok": True, "source": source.to_dict()})
        run = self.store.run_source(source_id, reviewer=reviewer, note=note)
        return self._json(200, {"ok": True, "run": run.to_dict()})

    def _cluster_action(
        self, cluster_id: str, action: str, environ: Dict[str, object]
    ) -> Tuple[str, Dict[str, str], object]:
        cluster_id = unquote(cluster_id)
        payload = self._payload(environ)
        reviewer = str(payload.get("reviewer", "") or "")
        note = str(payload.get("note", "") or "")
        if action == "claim":
            force = payload.get("force", False)
            if isinstance(force, str):
                force = force.strip().lower() in ("true", "1", "yes", "on")
            claim = self.store.claim_cluster(
                cluster_id, reviewer, note=note, force=bool(force)
            )
            return self._json(200, {"ok": True, "claim": claim.to_dict()})
        if action == "release":
            claim = self.store.release_cluster(cluster_id, reviewer)
            return self._json(200, {"ok": True, "claim": claim.to_dict()})
        if action == "resolve":
            claim = self.store.resolve_cluster(cluster_id, reviewer, note=note)
            return self._json(
                200, {"ok": True, "claim": claim.to_dict() if claim else None}
            )
        if action == "discard":
            self.store.discard(cluster_id, reviewer=reviewer, note=note)
        elif action == "restore":
            self.store.restore(cluster_id, reviewer=reviewer, note=note)
        elif action == "rename":
            label = str(payload.get("label", "")).strip()
            if not label:
                raise ValueError("label is required")
            self.store.rename(
                cluster_id, label, reviewer=reviewer, note=note
            )
        elif action == "split":
            item_ids = payload.get("item_ids") or []
            if not isinstance(item_ids, list) or not item_ids:
                raise ValueError("item_ids must be a non-empty list")
            self.store.split(
                cluster_id, item_ids, reviewer=reviewer, note=note
            )
        return self._json(200, {"ok": True, "action": action})

    # helpers ------------------------------------------------------------
    def _read_body(self, environ: Dict[str, object]) -> str:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        stream = environ.get("wsgi.input")
        if not stream or length <= 0:
            return ""
        data = stream.read(length)
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)

    def _payload(self, environ: Dict[str, object]) -> Dict[str, object]:
        body = self._read_body(environ)
        content_type = str(environ.get("CONTENT_TYPE", ""))
        if not body:
            return {}
        if "application/json" in content_type:
            return json.loads(body)
        parsed = parse_qs(body)
        return {key: values[0] for key, values in parsed.items()}

    def _json(
        self, status: int, payload: object
    ) -> Tuple[str, Dict[str, str], bytes]:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "%d %s" % (status, _status_text(status)),
            {"Content-Type": "application/json; charset=utf-8"},
            text.encode("utf-8"),
        )

    def _html(
        self, status: int, text: str
    ) -> Tuple[str, Dict[str, str], bytes]:
        return (
            "%d %s" % (status, _status_text(status)),
            {"Content-Type": "text/html; charset=utf-8"},
            text.encode("utf-8"),
        )

    def _dashboard(self, reviewer: str = "") -> str:
        report = build_report(self.store)
        discarded = [
            {"id": c.id, "label": c.label, "size": len(c.item_ids)}
            for c in self.store.clusters
            if c.status != ACTIVE
        ]
        sources = [s.to_dict() for s in self.store.list_sources()]
        ingest_runs = [r.to_dict() for r in self.store.ingest_runs][-10:]
        digest = build_digest(self.store, reviewer) if reviewer else None
        queue = build_queue(self.store, reviewer) if reviewer else None
        values = {
            "__REPORT__": _json_for_script(report),
            "__DISCARDED__": _json_for_script(discarded),
            "__SOURCES__": _json_for_script(sources),
            "__INGEST_RUNS__": _json_for_script(ingest_runs),
            "__SELECTED_REVIEWER__": _json_for_script(reviewer),
            "__DIGEST__": _digest_card(digest, reviewer),
            "__QUEUE__": _queue_card(queue, reviewer),
            "__CONTROLS__": _controls_script(),
        }
        return re.sub(
            "__REPORT__|__DISCARDED__|__SOURCES__|__INGEST_RUNS__"
            "|__SELECTED_REVIEWER__|__DIGEST__|__QUEUE__|__CONTROLS__",
            lambda match: values[match.group(0)],
            DASHBOARD_TEMPLATE,
        )


def _json_for_script(payload: object) -> str:
    """Serialise ``payload`` so it is safe to embed in a ``<script>`` block.

    ``json.dumps`` does not escape ``<``, ``>`` or ``&``, so a value containing
    ``</script>`` would terminate the surrounding element and let untrusted
    feedback text inject markup. Escaping those characters (plus the U+2028 /
    U+2029 line separators) keeps the payload inert while remaining valid
    JavaScript.
    """
    text = json.dumps(payload, ensure_ascii=False)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _digest_card(digest, reviewer: str) -> str:
    """Render the compact read-only "My digest" card for one reviewer.

    Every untrusted value (reviewer name, cluster labels, notes) is escaped
    before it reaches the HTML, so a reviewer named ``<b>Eve</b>`` cannot inject
    markup.
    """
    if not reviewer or digest is None:
        return (
            '<section class="card"><h2>My digest</h2>'
            '<p class="empty">Select a reviewer to see their weekly digest.</p>'
            "</section>"
        )

    def e(value: object) -> str:
        return html.escape("" if value is None else str(value))

    totals = digest["totals"]
    counts = digest["open_uncertainties"]
    parts = [
        '<section class="card">',
        "<h2>My digest</h2>",
        '<p class="metrics">Reviewer: <strong>%s</strong> &middot; generated %s</p>'
        % (e(digest["reviewer"]), e(digest["generated_at"])),
        '<p class="metrics">%d items &middot; %d topics &middot; '
        "%d clusters need review &middot; %d uncertain</p>"
        % (
            totals["items"], totals["clusters"],
            len(digest["needs_review"]), totals["uncertain_clusters"],
        ),
        '<p class="metrics">Open uncertainties: few_evidence %d &middot; '
        "low_cohesion %d &middot; low_separation %d</p>"
        % (
            counts.get("few_evidence", 0),
            counts.get("low_cohesion", 0),
            counts.get("low_separation", 0),
        ),
        '<p class="metrics">Claimed by you: %d &middot; unclaimed backlog: %d</p>'
        % (
            len(digest.get("claimed_by_me", [])),
            len(digest.get("unclaimed", [])),
        ),
    ]
    if digest["needs_review"]:
        for cluster in digest["needs_review"]:
            flags = "".join(
                '<span class="flag">%s</span>' % e(flag)
                for flag in cluster["uncertainty"]
            )
            parts.append(
                '<p class="metrics">%s %s (%d quotes)</p>'
                % (flags, e(cluster["label"]), cluster["size"])
            )
    else:
        parts.append(
            '<p class="empty">No uncertain clusters need your review.</p>'
        )
    if digest["recent_decisions"]:
        parts.append("<h2>Recent decisions</h2>")
        for decision in digest["recent_decisions"]:
            clusters = decision.get("clusters") or []
            cluster_line = " [%s]" % e(", ".join(clusters)) if clusters else ""
            note = decision.get("note") or ""
            note_line = " &mdash; %s" % e(note) if note else ""
            parts.append(
                '<p class="metrics"><strong>%s</strong>%s%s</p>'
                % (e(decision.get("action")), cluster_line, note_line)
            )
    parts.append("</section>")
    return "".join(parts)


def _queue_card(queue, reviewer: str) -> str:
    """Render the "Review queue" card with claim/release/resolve controls.

    Every untrusted value (reviewer, cluster label, note) is escaped. The card
    degrades gracefully when no reviewer is selected or the queue is empty.
    """
    if not reviewer or queue is None:
        return (
            '<section class="card"><h2>Review queue</h2>'
            '<p class="empty">Select a reviewer to see the review queue.</p>'
            "</section>"
        )

    def e(value: object) -> str:
        return html.escape("" if value is None else str(value))

    def buttons(entry: dict, mine: bool, other: bool) -> str:
        cid = e(entry["id"])
        out = ""
        if mine:
            out += (
                '<button onclick="releaseCluster(\'%s\')">Release</button> '
                '<button onclick="resolveCluster(\'%s\')">Resolve</button>'
                % (cid, cid)
            )
        elif other:
            out += (
                '<button onclick="forceClaimCluster(\'%s\')">Take over</button>'
                % cid
            )
        else:
            out += '<button onclick="claimCluster(\'%s\')">Claim</button>' % cid
        return out

    def block(title: str, entries, mine=False, other=False, empty=None) -> List[str]:
        lines = ["<h2>%s (%d)</h2>" % (e(title), len(entries))]
        if not entries and empty:
            lines.append('<p class="empty">%s</p>' % e(empty))
        for entry in entries:
            flags = "".join(
                '<span class="flag">%s</span>' % e(flag)
                for flag in entry["uncertainty"]
            )
            claim_line = ""
            if entry["claim_reviewer"]:
                claim_line = " &middot; claimed by %s" % e(entry["claim_reviewer"])
                if entry["claim_note"]:
                    claim_line += " (%s)" % e(entry["claim_note"])
            lines.append(
                '<p class="metrics">%s %s (%d quotes)%s</p><p>%s</p>'
                % (
                    flags, e(entry["label"]), entry["size"],
                    claim_line, buttons(entry, mine, other),
                )
            )
        return lines

    parts = [
        '<section class="card">',
        "<h2>Review queue</h2>",
        '<p class="metrics">Reviewer: <strong>%s</strong> &middot; '
        "mine %d &middot; others %d &middot; unclaimed %d</p>"
        % (
            e(queue["reviewer"]),
            len(queue["claimed_by_me"]),
            len(queue["claimed_by_others"]),
            len(queue["unclaimed"]),
        ),
    ]
    parts.extend(block(
        "Claimed by you", queue["claimed_by_me"], mine=True,
        empty="You have not claimed any clusters.",
    ))
    parts.extend(block(
        "Claimed by others", queue["claimed_by_others"], other=True,
        empty="No clusters are claimed by others.",
    ))
    parts.extend(block(
        "Unclaimed", queue["unclaimed"],
        empty="No unclaimed clusters. Nice work.",
    ))
    parts.append("</section>")
    return "".join(parts)


def _status_text(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        500: "Internal Server Error",
    }.get(status, "OK")


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedback Triage Copilot</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--line:#e4e7eb;--ink:#1f2933;--muted:#52606d}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
background:var(--bg);color:var(--ink);line-height:1.5}
header{background:#111827;color:#fff;padding:16px 24px}
header h1{margin:0;font-size:1.25rem}
header p{margin:2px 0 0;color:#9ca3af;font-size:.85rem}
main{max-width:960px;margin:0 auto;padding:20px}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 20px}
button{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid var(--line);
background:#fff;cursor:pointer}
button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:16px;margin:14px 0}
.card h2{margin:0 0 6px;font-size:1.05rem}
.metrics{color:var(--muted);font-size:.85rem;margin:4px 0}
.flag{display:inline-block;background:#fff3cd;color:#8a6100;border-radius:999px;
padding:1px 8px;font-size:.75rem;margin-right:4px}
blockquote{margin:8px 0;padding:8px 12px;background:#f0f4f8;
border-left:3px solid #9aa5b1;border-radius:0 6px 6px 0}
.quote-actions{margin-top:6px}
.quote-actions button{padding:2px 8px;font-size:.75rem}
input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);
border-radius:6px;width:100%}
a{color:#2563eb;word-break:break-all}
.empty{color:var(--muted)}
</style></head>
<body>
<header>
<h1>Feedback Triage Copilot</h1>
<p>Evidence-backed weekly triage for product teams</p>
</header>
<main>
<div class="toolbar">
<button class="primary" onclick="recluster()">Re-cluster</button>
<button onclick="doMerge()">Merge selected</button>
<a href="/report.csv"><button>Export CSV</button></a>
<a href="/report.pdf"><button>Export PDF</button></a>
<a href="/api/report"><button>JSON report</button></a>
<label style="display:flex;align-items:center;gap:6px">
<input type="file" id="csv-file" accept=".csv">
<button onclick="importFile()">Import CSV</button>
</label>
<label style="display:flex;align-items:center;gap:6px">
Reviewer:
<select id="reviewer-select" title="Reviewer for your decisions"
        onchange="selectReviewer()"></select>
</label>
<button onclick="addReviewer()">Add reviewer</button>
</div>
<div id="summary" class="card"></div>
<div id="digest">__DIGEST__</div>
<div id="queue">__QUEUE__</div>
<div id="sources"></div>
<div id="ingest-runs"></div>
<div id="clusters"></div>
<div id="discarded"></div>
<div id="decisions"></div>
</main>
<script>
const REPORT = __REPORT__;
const DISCARDED = __DISCARDED__;
const SOURCES = __SOURCES__;
const INGEST_RUNS = __INGEST_RUNS__;
const SELECTED_REVIEWER = __SELECTED_REVIEWER__;
__CONTROLS__
</script>
</body></html>
"""


def _controls_script() -> str:
    return r"""
function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}
function attr(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function safeUrl(s){const u=String(s??'').trim();return /^https?:\/\//i.test(u)?u:'';}
function fmtDist(d){return Object.entries(d).map(([k,v])=>`${esc(k)}: ${v}`).join(', ')||'none';}
function selected(){return Array.from(document.querySelectorAll('.pick:checked')).map(c=>c.value);}
function selectedItems(){return Array.from(document.querySelectorAll('.pickitem:checked')).map(c=>c.value);}
function render(){
  document.getElementById('summary').innerHTML =
    `<strong>${REPORT.totals.items}</strong> items &middot; `+
    `<strong>${REPORT.totals.clusters}</strong> topics &middot; `+
    `<strong>${REPORT.totals.uncertain_clusters}</strong> uncertain &middot; `+
    `${REPORT.totals.discarded_clusters} discarded<br>`+
    `<span class="metrics">Generated ${esc(REPORT.generated_at)}. `+
    `Select clusters below to merge, or quotes to split.</span>`;
  const root=document.getElementById('clusters');
  if(!REPORT.clusters.length){root.innerHTML='<p class="empty">No clusters yet. Import a CSV to begin.</p>';return;}
  root.innerHTML=REPORT.clusters.map(c=>`
    <section class="card">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start">
        <h2><input type="checkbox" class="pick" value="${attr(c.id)}"> ${esc(c.label)}</h2>
        <div>
          <button class="cluster-action" data-action="rename" data-cluster="${attr(c.id)}">Rename</button>
          <button class="cluster-action" data-action="discard" data-cluster="${attr(c.id)}">Discard</button>
        </div>
      </div>
      <p class="metrics">${c.uncertainty.map(f=>`<span class="flag">${esc(f)}</span>`).join('')}</p>
      <p class="metrics">${c.size} quotes (${(c.share*100).toFixed(0)}%) &middot;
        cohesion ${c.cohesion.toFixed(2)} &middot; separation ${c.separation.toFixed(2)}</p>
      <p class="metrics">Segments: ${fmtDist(c.segments)} &middot; Sources: ${fmtDist(c.sources)}</p>
      ${c.quotes.map(q=>{const u=safeUrl(q.url);return `<blockquote>
        <label><input type="checkbox" class="pickitem" value="${attr(q.id)}"> ${esc(q.text)}</label>
        <br><small>${esc(q.source)} &middot; ${esc(q.segment)}
        ${u?`&middot; <a href="${attr(u)}" target="_blank" rel="noopener noreferrer">source</a>`:''}</small>
      </blockquote>`;}).join('')}
      <div class="quote-actions">
        <button class="cluster-action" data-action="split" data-cluster="${attr(c.id)}">Split selected quotes off</button>
      </div>
    </section>`).join('');
  root.querySelectorAll('.cluster-action').forEach(b=>{
    b.addEventListener('click',()=>{
      const id=b.dataset.cluster;
      if(b.dataset.action==='rename')renameCluster(id);
      else if(b.dataset.action==='discard')discardCluster(id);
      else splitCluster(id);
    });
  });
}
function renderDiscarded(){
  const root=document.getElementById('discarded');
  if(!DISCARDED.length){root.innerHTML='';return;}
  root.innerHTML=`<h2 style="margin:18px 0 0;font-size:1.05rem">Discarded clusters</h2>`+
    DISCARDED.map(c=>`<section class="card">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
        <h2>${esc(c.label)}</h2>
        <button class="restore-action" data-cluster="${attr(c.id)}">Restore</button>
      </div>
      <p class="metrics">${c.size} quotes</p>
    </section>`).join('');
  root.querySelectorAll('.restore-action').forEach(b=>{
    b.addEventListener('click',()=>restoreCluster(b.dataset.cluster));
  });
}
async function post(url, body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:body?JSON.stringify(body):'{}'});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){alert(data.error||('HTTP '+r.status));return null;}
  return data;
}
async function loadReviewers(){
  const sel=document.getElementById('reviewer-select');
  if(!sel)return;
  const previous=sel.value;
  let reviewers=[];
  try{const r=await fetch('/api/reviewers');
    if(r.ok){reviewers=(await r.json()).reviewers||[];}}catch(e){}
  sel.innerHTML='<option value="">(no reviewer)</option>'+
    reviewers.map(v=>`<option value="${attr(v.id)}">${esc(v.name)}</option>`).join('');
  if(SELECTED_REVIEWER){
    const match=Array.from(sel.options).find(o=>o.value===SELECTED_REVIEWER||o.textContent===SELECTED_REVIEWER);
    if(match)sel.value=match.value;
  } else if(previous){sel.value=previous;}
}
function selectReviewer(){
  const sel=document.getElementById('reviewer-select');
  const value=sel?sel.value:'';
  window.location=value?('/?reviewer='+encodeURIComponent(value)):'/';
}
function currentReviewer(){const sel=document.getElementById('reviewer-select');return sel?sel.value:'';}
function optionalNote(message){const value=prompt(message);return value?value:'';}
function withReviewer(body){const out=Object.assign({},body||{});const rv=currentReviewer();
  if(rv)out.reviewer=rv;return out;}
async function addReviewer(){const name=prompt('Reviewer name');if(!name)return;
  if(await post('/api/reviewers',{name}))location.reload();}
async function mergeSelected(){const ids=selected();if(ids.length<2){alert('Select at least two clusters.');return;}
  const note=optionalNote('Optional note for this merge');
  if(await post('/api/merge',withReviewer({cluster_ids:ids,note}))location.reload();}
async function claimCluster(id){const note=optionalNote('Optional note for claiming this cluster');
  if(await post(`/api/clusters/${id}/claim`,withReviewer({note})))location.reload();}
async function forceClaimCluster(id){const note=optionalNote('Optional note for taking over this cluster');
  if(await post(`/api/clusters/${id}/claim`,withReviewer({note,force:true})))location.reload();}
async function releaseCluster(id){if(await post(`/api/clusters/${id}/release`,withReviewer({})))location.reload();}
async function resolveCluster(id){const note=optionalNote('Optional note for resolving this cluster');
  if(await post(`/api/clusters/${id}/resolve`,withReviewer({note})))location.reload();}
async function discardCluster(id){const note=optionalNote('Optional note for discarding this cluster');
  if(await post(`/api/clusters/${id}/discard`,withReviewer({note})))location.reload();}
async function restoreCluster(id){if(await post(`/api/clusters/${id}/restore`,withReviewer({})))location.reload();}
async function renameCluster(id){const label=prompt('New label');if(!label)return;
  const note=optionalNote('Optional note for this rename');
  if(await post(`/api/clusters/${id}/rename`,withReviewer({label,note})))location.reload();}
async function splitCluster(id){const items=selectedItems();if(!items.length){alert('Select quotes to split off.');return;}
  const note=optionalNote('Optional note for this split');
  if(await post(`/api/clusters/${id}/split`,withReviewer({item_ids:items,note})))location.reload();}
async function recluster(){if(await post('/api/recluster',withReviewer({})))location.reload();}
async function importFile(){const f=document.getElementById('csv-file').files[0];
  if(!f){alert('Choose a CSV file first.');return;}
  const csv=await f.text();
  const rv=currentReviewer();
  const r=await fetch('/api/import?source=upload'+(rv?'&reviewer='+encodeURIComponent(rv):''),{method:'POST',
    headers:{'Content-Type':'text/csv'},body:csv});
  if(r.ok)location.reload();else alert('Import failed');}
function renderSources(){
  const root=document.getElementById('sources');
  if(!root)return;
  if(!SOURCES.length){root.innerHTML='';return;}
  root.innerHTML=`<h2 style="margin:18px 0 0;font-size:1.05rem">Sources</h2>`+
    SOURCES.map(s=>`<section class="card">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:center">
        <h2>${esc(s.name)} <span class="metrics">${s.enabled?'enabled':'disabled'}</span></h2>
        <div>
          <button class="source-run" data-source="${attr(s.id)}">Run</button>
          <button class="source-toggle" data-source="${attr(s.id)}" data-enabled="${s.enabled?'false':'true'}">${s.enabled?'Disable':'Enable'}</button>
        </div>
      </div>
      <p class="metrics">${esc(s.path)}${s.glob?` &middot; glob ${esc(s.glob)}`:''}</p>
      <p class="metrics">last run ${esc(s.last_run_at||'never')} &middot; added ${s.last_added} &middot; skipped ${s.last_skipped}</p>
    </section>`).join('');
  root.querySelectorAll('.source-run').forEach(b=>{
    b.addEventListener('click',()=>runSource(b.dataset.source));
  });
  root.querySelectorAll('.source-toggle').forEach(b=>{
    b.addEventListener('click',()=>toggleSource(b.dataset.source,b.dataset.enabled==='true'));
  });
}
function renderIngestRuns(){
  const root=document.getElementById('ingest-runs');
  if(!root)return;
  if(!INGEST_RUNS.length){root.innerHTML='';return;}
  root.innerHTML=`<h2 style="margin:18px 0 0;font-size:1.05rem">Recent ingestion runs</h2>`+
    INGEST_RUNS.slice().reverse().map(r=>`<section class="card">
      <p class="metrics"><strong>${esc(r.source_name)}</strong> &middot; ${esc(r.timestamp)} &middot; added ${r.added} &middot; skipped ${r.skipped}${r.errors.length?` &middot; errors ${r.errors.length}`:''}</p>
      <p class="metrics">${esc((r.files||[]).join(', ')||'no files')}</p>
    </section>`).join('');
}
async function runSource(id){if(await post(`/api/sources/${id}/run`,withReviewer({})))location.reload();}
async function toggleSource(id,enabled){const action=enabled?'enable':'disable';if(await post(`/api/sources/${id}/${action}`,{}))location.reload();}
function renderDecisions(){
  const root=document.getElementById('decisions');
  const decisions=REPORT.decisions||[];
  if(!decisions.length){root.innerHTML='';return;}
  root.innerHTML=`<h2 style="margin:18px 0 0;font-size:1.05rem">Decision log</h2>`+
    decisions.map(d=>`<section class="card">
      <p class="metrics"><strong>${esc(d.action)}</strong> &middot;
        ${esc(d.reviewer||'unreviewed')} &middot; ${esc(d.timestamp)}</p>
      ${d.clusters&&d.clusters.length?`<p class="metrics">Clusters: ${esc(d.clusters.join(', '))}</p>`:''}
      ${d.note?`<p>${esc(d.note)}</p>`:''}
    </section>`).join('');
}
function doMerge(){mergeSelected();}
render();
renderSources();
renderIngestRuns();
renderDiscarded();
renderDecisions();
loadReviewers();
"""
