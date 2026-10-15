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

# per-reviewer digest (text, HTML or JSON)
python3 -m feedback_triage --workspace workspace.json digest --format text
python3 -m feedback_triage --workspace workspace.json digest --reviewer Alice --format json

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
| `digest [--reviewer NAME_OR_ID] [--format {text,html,json}] [--out FILE]` | Render the per-reviewer digest |
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

# weekly digest for the whole team, or just one reviewer
python3 -m feedback_triage --workspace workspace.json digest
python3 -m feedback_triage --workspace workspace.json digest --reviewer Alice --format json
```

CSV columns are auto-detected (`text`, `segment`, `source`, `url`, `date`,
`rating`, `language`, `id` and common aliases, in English and German). The
delimiter (`,`, `;`, tab, `|`) is sniffed automatically.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | HTML dashboard |
| GET | `/healthz` | Health probe (items, clusters) |
| GET | `/api/report` | Weekly report as JSON |
| GET | `/api/digest` | Per-reviewer digest as JSON; `?reviewer=<id-or-name>` filters to one reviewer |
| GET | `/api/clusters`, `/api/items`, `/api/history` | Raw data |
| GET | `/api/reviewers` | List reviewer accounts |
| POST | `/api/reviewers` | `{"name": "Alice"}` |
| POST | `/api/import?source=NAME` | Import CSV body |
| POST | `/api/recluster` | Rebuild clusters |
| POST | `/api/merge` | `{"cluster_ids": [...], "reviewer": "...", "note": "..."}` |
| POST | `/api/clusters/{id}/{discard,restore,rename,split}` | Corrections; optional `reviewer` and `note` |
| GET | `/report.csv`, `/report.pdf`, `/report.html` | Report exports |
| GET | `/digest.html`, `/digest.txt` | Digest exports (`?reviewer=<id-or-name>` filters) |

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

## Per-reviewer digest and workload balancing

The digest answers "who is responsible for which open clusters?" without leaving
the dependency-free, database-free design. Because the workspace has no explicit
assignment field, responsibility is inferred from the append-only decision log:
the reviewer attributed to the most recent decision that touched a cluster owns
it. Clusters that no reviewer has touched are listed as having no reviewer
activity.

`build_digest(store, reviewer="")` returns a plain dictionary with
`generated_at`, `reviewer`, `reviewers`, `assignments`, `unassigned_clusters`,
`recent_decisions`, `suggested_assignments` and `totals`. It is available as:

```sh
python3 -m feedback_triage --workspace workspace.json digest --format text
python3 -m feedback_triage --workspace workspace.json digest --reviewer Alice --format html --out digest.html
```

`--reviewer` accepts a reviewer id or name (case-insensitive) and filters the
digest to that reviewer; an unknown reviewer yields an empty digest rather than
an error. The same view is exposed over HTTP at `/api/digest` (JSON),
`/digest.html` and `/digest.txt`, and as a read-only **Reviewer digest** panel on
the dashboard. HTML, text and JSON reuse the report's escaping rules: HTML
escapes `<`, `>`, `&` and `"`, text neutralises spreadsheet-formula prefixes,
and JSON is standard UTF-8 JSON.

**Balancing rule (least-loaded-first).**
`suggest_assignments(store, clusters=None)` proposes an assignment for the
active clusters that no reviewer owns yet. Existing ownership counts towards a
reviewer's starting load; the clusters are then assigned largest first to the
reviewer with the fewest open clusters (least-loaded-first), with ties broken by
reviewer name and then id. The rule is stable and explainable — running it twice
on the same workspace returns the same order — and it is advisory only: it never
writes to the workspace. Example JSON:

```json
{
  "cluster_id": "c1a2b3c4d5",
  "cluster_label": "PDF / Export",
  "reviewer_id": "r9f0e1d2c3b4",
  "reviewer_name": "Alice",
  "open_items": 4
}
```

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
- **Read-only digest:** responsibility is inferred from the decision log and the
  balancing suggestion is advisory; rendering a digest never writes to the
  workspace.
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
