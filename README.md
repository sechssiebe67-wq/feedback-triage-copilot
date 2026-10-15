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
python3 -m feedback_triage --workspace workspace.json import data/sample_sales_notes.csv --source sales

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
| `source add <name> <path> [--glob GLOB] [--label LABEL] [--segment SEGMENT]` | Register a reusable source |
| `source list` | List sources with enabled state and last run |
| `source enable|disable <id>` | Toggle whether a source runs |
| `source remove <id>` | Delete a source |
| `ingest [--source ID] [--reviewer NAME] [--note TEXT]` | Run one or all enabled sources |
| `ingest-log [--limit N]` | Print recent ingestion runs |
| `cluster [--reviewer NAME] [--note TEXT]` | Re-cluster the workspace |
| `report --format {csv,html,pdf,json} [--out FILE] [--reviewer NAME] [--note TEXT]` | Export the weekly report |
| `reviewer add <name>` | Add a reviewer account |
| `reviewer list` | List reviewer accounts |
| `digest --reviewer NAME [--limit N] [--format {text,json}]` | Weekly digest for one reviewer |
| `claim --cluster ID --reviewer NAME [--note TEXT] [--force]` | Claim an uncertain cluster |
| `release --cluster ID [--reviewer NAME]` | Release a claimed cluster |
| `resolve --cluster ID --reviewer NAME [--note TEXT]` | Resolve a claimed cluster |
| `queue [--reviewer NAME] [--format {text,json}]` | Show the claimed / unclaimed review queue |
| `history` | Print the raw correction history |
| `decisions` | Print the attributed decision log |
| `serve [--host H] [--port P]` | Run the web application |

```sh
# reviewer accounts and an auditable decision log
python3 -m feedback_triage --workspace workspace.json reviewer add "Alice"
python3 -m feedback_triage --workspace workspace.json import data/sample_support.csv --source support --reviewer Alice
python3 -m feedback_triage --workspace workspace.json decisions
```

## Scheduled ingestion and idempotency

Instead of importing files one by one, declare **sources** once and let a
scheduled job run them. A source names a file or a directory plus an optional
`glob`; a run records its files, `added`/`skipped` counts and any per-file
errors, updates the source's `last_run_at`/`last_added`/`last_skipped`, and
appends one `EditEvent` with action `ingest` to the decision log.

```sh
# register three sources (a file or a directory + glob)
python3 -m feedback_triage --workspace workspace.json \
  source add support data/sample_support.csv --label support --segment enterprise
python3 -m feedback_triage --workspace workspace.json \
  source add reviews data --glob 'sample_reviews*.csv' --label app_review
python3 -m feedback_triage --workspace workspace.json \
  source add sales data/sample_sales_notes.csv --label sales

# run everything enabled, then inspect the run log
python3 -m feedback_triage --workspace workspace.json ingest --reviewer Alice
python3 -m feedback_triage --workspace workspace.json ingest-log
```

**Idempotency guarantee.** Every run deduplicates by item id. CSVs that carry
an `id` column use it; CSVs without one get a deterministic id derived from the
text, source, segment, URL and date, so re-running the same export never
duplicates rows. Running the command above twice leaves the workspace item count
unchanged on the second run (`added == 0`, `skipped == <rows>`). This makes the
command safe for cron and Kubernetes `CronJob`. A file that fails to parse is
recorded as an error entry and does not abort the rest of the run.

A minimal Kubernetes `CronJob` (reusing the same image and workspace volume as
the Deployment) that ingests every Monday at 06:00:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: feedback-triage-ingest
spec:
  schedule: "0 6 * * 1"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: ingest
              image: feedback-triage-copilot:0.1.0
              args: ["python", "-m", "feedback_triage",
                     "--workspace", "/data/workspace.json", "ingest"]
              volumeMounts:
                - name: data
                  mountPath: /data
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: feedback-triage-data
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
| GET | `/api/digest?reviewer=NAME&limit=N` | Read-only weekly digest for one reviewer (400 when `reviewer` is missing) |
| GET | `/api/queue?reviewer=NAME` | Review queue: `claimed_by_me`, `claimed_by_others`, `unclaimed` (400 when `reviewer` is missing) |
| GET | `/api/clusters`, `/api/items`, `/api/history` | Raw data |
| GET | `/api/reviewers` | List reviewer accounts |
| POST | `/api/reviewers` | `{"name": "Alice"}` |
| GET | `/api/sources` | List ingestion sources |
| POST | `/api/sources` | `{"name": "...", "path": "...", "glob": "...", "label": "...", "segment": "..."}` |
| POST | `/api/sources/{id}/{enable,disable}` | Toggle a source |
| POST | `/api/sources/{id}/run` | Run one source; optional `reviewer`/`note` |
| POST | `/api/ingest` | Run every enabled source; optional `reviewer`/`note` |
| GET | `/api/ingest-runs?limit=N` | Recent ingestion runs |
| POST | `/api/import?source=NAME` | Import CSV body |
| POST | `/api/recluster` | Rebuild clusters |
| POST | `/api/merge` | `{"cluster_ids": [...], "reviewer": "...", "note": "..."}` |
| POST | `/api/clusters/{id}/{discard,restore,rename,split}` | Corrections; optional `reviewer` and `note` |
| POST | `/api/clusters/{id}/claim` | `{"reviewer": "...", "note": "...", "force": false}` |
| POST | `/api/clusters/{id}/release` | `{"reviewer": "..."}` (claimant or empty reviewer) |
| POST | `/api/clusters/{id}/resolve` | `{"reviewer": "...", "note": "..."}` |
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

## Per-reviewer weekly digest

The `digest` command summarises the weekly report for one named reviewer
(a reviewer name or id): the clusters whose latest decision they made, the
uncertain clusters still awaiting their decision, the count of open
uncertainties per flag, and their most recent decisions. It is **read-only**:
running it never modifies the workspace or the history.

```sh
python3 -m feedback_triage --workspace workspace.json digest --reviewer Alice
python3 -m feedback_triage --workspace workspace.json digest --reviewer Alice --format json
```

The same view is available over HTTP as
`GET /api/digest?reviewer=Alice&limit=10` (a missing `reviewer` returns HTTP
400) and on the dashboard as a compact "My digest" card: choosing a reviewer in
the toolbar reloads `/?reviewer=Alice` and renders that reviewer's digest.

## Review queue (claim / release / resolve)

Uncertain clusters form a shared backlog. Any reviewer can **claim** one so
teammates can see it is taken, **release** it back to the backlog, or
**resolve** it with a note. Claims are stored in the workspace JSON (no
database) as `Claim` records and are covered by the same append-only history:
every claim, release and resolve appends exactly one `EditEvent` with the
acting reviewer and note, and resolved claims stay in the report's `decisions`
log.

```sh
# claim an uncertain cluster, resolve it, or hand it back
python3 -m feedback_triage --workspace workspace.json claim --cluster c123 --reviewer Alice --note "checking the retail segment"
python3 -m feedback_triage --workspace workspace.json resolve --cluster c123 --reviewer Alice --note "duplicate of PDF export"
python3 -m feedback_triage --workspace workspace.json release --cluster c123 --reviewer Alice

# every reviewer sees their own queue: mine / others / unclaimed
python3 -m feedback_triage --workspace workspace.json queue --reviewer Alice
python3 -m feedback_triage --workspace workspace.json queue --reviewer Alice --format json
```

Claiming is **idempotent** for the current claimant (claiming twice leaves one
active claim and one `claim` event). Claiming a cluster held by another
reviewer raises an error unless `--force` is given, which reassigns the claim
and records a `claim` event attributed to the new reviewer. Only the current
claimant (or an empty reviewer) may `release`.

The same actions are available over HTTP (`GET /api/queue?reviewer=NAME` and
`POST /api/clusters/{id}/{claim,release,resolve}`) and on the dashboard
"Review queue" card, which shows the three buckets with claim/release/resolve
buttons. The digest and `GET /api/digest` also include `claimed_by_me`,
`claimed_by_others` and `unclaimed`, and the report exports add the
`claim_reviewer` / `claim_status` / `claim_note` CSV columns plus a "Review
queue" section in HTML and PDF (with untrusted reviewer names and notes
escaped and formula-leading cells neutralised).

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
