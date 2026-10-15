"""WSGI application: dashboard, JSON API, exports and health check.

Runs on the standard library only, so it can be served directly with
``wsgiref`` or behind any WSGI host. The same object is used by the tests.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, Tuple
from urllib.parse import parse_qs, unquote

from .ingest import parse_csv
from .models import ACTIVE
from .report import (
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from .store import TriageStore


CLUSTER_ACTION = re.compile(r"^/api/clusters/([^/]+)/(discard|restore|rename|split)$")


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
            return self._html(200, self._dashboard())
        if path == "/healthz" and method == "GET":
            return self._json(200, self._health())
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
            self.store.recluster()
            return self._json(200, build_report(self.store))
        if path == "/api/merge" and method == "POST":
            payload = self._payload(environ)
            cluster_ids = payload.get("cluster_ids") or []
            if not isinstance(cluster_ids, list) or len(cluster_ids) < 2:
                raise ValueError("cluster_ids must contain at least two clusters")
            merged = self.store.merge(cluster_ids)
            return self._json(200, {"ok": True, "cluster_id": merged.id})

        action = CLUSTER_ACTION.match(path)
        if action and method == "POST":
            return self._cluster_action(action.group(1), action.group(2), environ)
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
        content_type = str(environ.get("CONTENT_TYPE", ""))
        if "application/json" in content_type:
            payload = json.loads(body or "{}")
            content = payload.get("csv", "")
            source = payload.get("source", source)
            mapping.update(payload.get("mapping", {}) or {})
        else:
            content = body
        items = parse_csv(content, source=source, mapping=mapping or None)
        added = self.store.add_items(items)
        report = build_report(self.store)
        return self._json(200, {"added": added, "total": len(self.store.items),
                                "clusters": report["totals"]["clusters"]})

    def _cluster_action(
        self, cluster_id: str, action: str, environ: Dict[str, object]
    ) -> Tuple[str, Dict[str, str], object]:
        cluster_id = unquote(cluster_id)
        payload = self._payload(environ)
        if action == "discard":
            self.store.discard(cluster_id)
        elif action == "restore":
            self.store.restore(cluster_id)
        elif action == "rename":
            label = str(payload.get("label", "")).strip()
            if not label:
                raise ValueError("label is required")
            self.store.rename(cluster_id, label)
        elif action == "split":
            item_ids = payload.get("item_ids") or []
            if not isinstance(item_ids, list) or not item_ids:
                raise ValueError("item_ids must be a non-empty list")
            self.store.split(cluster_id, item_ids)
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

    def _dashboard(self) -> str:
        report = build_report(self.store)
        discarded = [
            {"id": c.id, "label": c.label, "size": len(c.item_ids)}
            for c in self.store.clusters
            if c.status != ACTIVE
        ]
        return (
            DASHBOARD_TEMPLATE
            .replace("__REPORT__", _json_for_script(report))
            .replace("__DISCARDED__", _json_for_script(discarded))
            .replace("__CONTROLS__", _controls_script())
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
</div>
<div id="summary" class="card"></div>
<div id="clusters"></div>
<div id="discarded"></div>
</main>
<script>
const REPORT = __REPORT__;
const DISCARDED = __DISCARDED__;
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
async function mergeSelected(){const ids=selected();if(ids.length<2){alert('Select at least two clusters.');return;}
  if(await post('/api/merge',{cluster_ids:ids}))location.reload();}
async function discardCluster(id){if(await post(`/api/clusters/${id}/discard`))location.reload();}
async function restoreCluster(id){if(await post(`/api/clusters/${id}/restore`))location.reload();}
async function renameCluster(id){const label=prompt('New label');if(!label)return;
  if(await post(`/api/clusters/${id}/rename`,{label}))location.reload();}
async function splitCluster(id){const items=selectedItems();if(!items.length){alert('Select quotes to split off.');return;}
  if(await post(`/api/clusters/${id}/split`,{item_ids:items}))location.reload();}
async function recluster(){if(await post('/api/recluster'))location.reload();}
async function importFile(){const f=document.getElementById('csv-file').files[0];
  if(!f){alert('Choose a CSV file first.');return;}
  const csv=await f.text();
  const r=await fetch('/api/import?source=upload',{method:'POST',
    headers:{'Content-Type':'text/csv'},body:csv});
  if(r.ok)location.reload();else alert('Import failed');}
function doMerge(){mergeSelected();}
render();
renderDiscarded();
"""
