# Feedback Triage Copilot

Evidence-backed weekly feedback triage for small B2B product teams.

Feedback arrives through support, sales notes, app reviews and NPS comments.
This tool imports those sources, clusters them into themes with original quotes
and source links, and produces a weekly report with frequency and segment
distribution plus explicit uncertainty flags. It is a transparent triage layer
over existing tools, not another feedback board.

See [`CONCEPT.md`](CONCEPT.md) for the goal, users, capabilities and roadmap.

## Quick start

No third-party dependencies are required (Python 3.8+ standard library only).

```sh
# import sample sources into a workspace
python3 -m feedback_triage --workspace workspace.json import data/sample_support.csv --source support
python3 -m feedback_triage --workspace workspace.json import data/sample_reviews.csv --source app_review

# render exports
python3 -m feedback_triage --workspace workspace.json report --format csv --out report.csv
python3 -m feedback_triage --workspace workspace.json report --format pdf --out report.pdf

# run the web app (dashboard, JSON API, CSV/PDF export, /healthz)
python3 -m feedback_triage --workspace workspace.json serve --port 8080
```

Open <http://localhost:8080>. The dashboard lets you re-cluster, merge, split,
discard and rename clusters; every correction is written to an append-only
history.

### CLI commands

| Command | Purpose |
| --- | --- |
| `import <file> [--source NAME] [--no-recluster]` | Import a feedback CSV |
| `cluster` | Re-cluster the workspace |
| `report --format {csv,html,pdf,json} [--out FILE]` | Export the weekly report |
| `history` | Print the correction history |
| `serve [--host H] [--port P]` | Run the web application |

CSV columns are auto-detected (`text`, `segment`, `source`, `url`, `date`,
`rating`, `language`, `id` and common aliases, in English and German). The
delimiter (`,`, `;`, tab, `|`) is sniffed automatically.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | HTML dashboard |
| GET | `/healthz` | Health probe (items, clusters) |
| GET | `/api/report` | Weekly report as JSON |
| GET | `/api/clusters`, `/api/items`, `/api/history` | Raw data |
| POST | `/api/import?source=NAME` | Import CSV body |
| POST | `/api/recluster` | Rebuild clusters |
| POST | `/api/merge` | `{"cluster_ids": [...]}` |
| POST | `/api/clusters/{id}/{discard,restore,rename,split}` | Corrections |
| GET | `/report.csv`, `/report.pdf`, `/report.html` | Report exports |

## Container and Kubernetes

```sh
docker build -t feedback-triage-copilot:0.1.0 .
docker run -p 8080:8080 -v "$PWD/data:/data" feedback-triage-copilot:0.1.0

helm install feedback-triage deploy/helm/feedback-triage \
  --set ingress.host=feedback-triage.example.com
```

The container runs as a non-root user, exposes `/healthz` for liveness and
readiness probes, persists the workspace on a volume, and stays within a small
CPU/memory budget.

## Design notes

- **Evidence is mandatory:** every cluster carries representative quotes and
  source links; there is no opaque priority score.
- **Uncertainty is visible:** clusters with too few items (`few_evidence`), low
  internal consistency (`low_cohesion`) or weak separation from a neighbouring
  cluster (`low_separation`) are flagged.
- **Dependency-free:** TF-IDF plus leader clustering over a small stop-word list
  and light suffix stripping, implemented with the Python standard library.
- **Database-free:** one workspace is one JSON document, written atomically.
- **Untrusted input stays inert:** imported feedback is never trusted. The
  dashboard escapes values before they reach the DOM and restricts source links
  to `http(s)`, the embedded report payload is escaped for the `<script>`
  context, CSV exports neutralise formula-leading cells (`= + - @`, tab, CR),
  and the HTML report escapes every imported field.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

CI also runs the pipeline tests and the product tests from this `tests/`
directory, plus the workflow definition checks.

## Development pipeline

This repository is developed by an autonomous pipeline. Its documentation lives
in [`.github/README.md`](.github/README.md), including the required GitHub
secrets and repository settings.
