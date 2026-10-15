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
discard, rename and restore clusters; every correction is written to an
append-only history. Discarded clusters stay visible in a separate section so
they can be restored.

### CLI commands

| Command | Purpose |
| --- | --- |
| `import <file> [--source NAME] [--no-recluster] [--reviewer NAME] [--note TEXT]` | Import a feedback CSV |
| `cluster [--reviewer NAME] [--note TEXT]` | Re-cluster the workspace |
| `report --format {csv,html,pdf,json} [--out FILE] [--reviewer NAME] [--note TEXT]` | Export the weekly report |
| `digest [--since ISO-DATE] [--days N] [--format {text,json}]` | Print the weekly triage digest and reviewer load |
| `reviewer add <name>` | Add a reviewer account |
| `reviewer list` | List reviewer accounts |
| `history` | Print the raw correction history |
| `decisions` | Print the attributed decision log |
| `serve [--host H] [--port P]` | Run the web application |

```sh
# reviewer accounts and an auditable decision log
python3 -m feedback_triage --workspace workspace.json reviewer add "Alice"
python3 -m feedback_triage --workspace workspace.json import data/sample_support.csv --source support --reviewer Alice
python3 -m feedback_triage --workspace workspace.json decisions

# weekly digest of activity and reviewer load (text or JSON)
python3 -m feedback_triage --workspace workspace.json digest
python3 -m feedback_triage --workspace workspace.json digest --since 2026-01-01 --days 7
python3 -m feedback_triage --workspace workspace.json digest --format json
```

CSV columns are auto-detected (`text`, `segment`, `source`, `url`, `date`,
`rating`, `language`, `id` and common aliases, in English and German). The
delimiter (`,`, `;`, tab, `|`) is sniffed automatically.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | HTML dashboard |
| GET | `/healthz` | Health probe (items, clusters) |
| GET | `/api/report` | Weekly report as JSON (includes the `digest` section) |
| GET | `/api/digest` | Weekly digest and reviewer load as JSON |
| GET | `/api/clusters`, `/api/items`, `/api/history` | Raw data |
| GET | `/api/reviewers` | List reviewer accounts |
| POST | `/api/reviewers` | `{"name": "Alice"}` |
| POST | `/api/import?source=NAME` | Import CSV body |
| POST | `/api/recluster` | Rebuild clusters |
| POST | `/api/merge` | `{"cluster_ids": [...], "reviewer": "...", "note": "..."}` |
| POST | `/api/clusters/{id}/{discard,restore,rename,split}` | Corrections; optional `reviewer` and `note` |
| GET | `/report.csv`, `/report.pdf`, `/report.html` | Report exports |

Correction payloads accept an optional `reviewer` (a reviewer id or name) and an
optional `note`. They are recorded on the append-only `EditEvent`, so the report
gains a `decisions` section and the CSV export gains the `decision_*` columns.

## Reviewer accounts and decision log

Reviewer accounts make the correction history auditable. Reviewers are a small
list stored in the workspace JSON (`add`/`list` via the CLI, `/api/reviewers`
over HTTP); there is no authentication for the MVP, a reviewer is an identity
label chosen in the dashboard toolbar or supplied via the API/CLI.

Every merge, split, discard, restore, rename, re-cluster and import can be
attributed to a reviewer and can carry a short rationale. The dashboard shows a
decision log and sends the selected reviewer (plus an optional note) with each
correction. The report payload exposes `decisions` entries with `action`,
`timestamp`, `reviewer`, `note` and the affected cluster labels; HTML, PDF and
CSV exports render the same log, escaping untrusted reviewer names and notes
and neutralising formula-leading cells. Workspaces created before reviewer
attribution load unchanged: missing `reviewers` and `reviewer`/`note` fields
default to empty.

## Weekly digest and reviewer load

The digest makes the weekly triage load visible at a glance: how many decisions
were taken, by whom, and which topics are still unreviewed. It is exposed by
`digest` on the CLI, `GET /api/digest`, the `digest` key of `/api/report`, and a
"This week" card in the HTML/PDF exports and the dashboard.

It is **derived from the append-only history** (plus the current items and
clusters) on every request, never stored in the workspace, so it stays auditable
and old workspaces without any digest fields load unchanged and simply produce a
zeroed digest. The window defaults to the last 7 days; pass `--since ISO-DATE`
for an inclusive custom start (an event exactly on `since` is included).

| Field | Meaning |
| --- | --- |
| `window_start`, `window_end` | Inclusive window boundaries (ISO-8601) |
| `since` | The supplied `--since` value, or `""` for the default window |
| `generated_at`, `days` | When the digest was built and the window length |
| `total_items`, `new_items` | Items in the workspace and created in the window |
| `new_clusters`, `discarded_clusters` | Topics created in the window and discarded topics |
| `decisions`, `decisions_by_action` | Decisions in the window, total and per action |
| `reviewer_load` | Per reviewer, a count for each action plus `total` |
| `unreviewed` | Decisions in the window with no reviewer |
| `untouched_topics` | Topics with no decision in the window |
| `changed_topics` | Top three clusters by size discarded, restored or renamed |

The CSV export gains `digest_*` columns rendered as a single summary row above
the existing per-quote rows, and the HTML/PDF exports render the same values as
a card/section. Reviewer names and notes follow the existing untrusted-input
handling: HTML-escaped in `report_to_html` and neutralised with `_csv_safe` in
`report_to_csv`.

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
