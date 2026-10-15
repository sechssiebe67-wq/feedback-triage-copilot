# Feedback Triage Copilot

## Goal

Feedback Triage Copilot turns scattered customer feedback into a weekly,
evidence-backed triage report so a small product team can make a defensible
prioritisation in under 30 minutes.

It is **not** another feedback board. It is a transparent triage layer that sits
on top of sources the team already has (support exports, app-review exports,
sales notes). Every topic it surfaces is backed by original quotes, source
links, a frequency estimate, a segment distribution and an explicit uncertainty
label. Existing tools are complemented, not replaced.

## Intended users

Product teams in B2B SaaS companies of roughly 5-50 people that collect feedback
weekly across support, sales, app reviews and NPS comments but do not have a
dedicated research role. Their current process is manual, inconsistent and
biased towards the loudest single voice instead of patterns over time and
customer segments.

## Problem

- Feedback arrives through several channels and is reviewed by hand.
- Prioritisation is inconsistent between people and weeks.
- Trends and recurring themes stay invisible.
- Decisions rest on anecdotes rather than on evidence.

## Value proposition

A weekly, automatically clustered report where each topic carries:

- original quotes and source links (evidence is mandatory),
- frequency estimate and segment/source distribution,
- explicit uncertainty flags for thin or weakly separated clusters,
- user corrections (merge, split, discard, rename) with a stored history.

## Current capabilities (MVP)

- **Ingestion:** flexible CSV import for two-three sources with column
  auto-detection (`text`, `segment`, `source`, `url`, `date`, `rating`,
  `language`, `id`).
- **Scheduled per-source ingestion:** a workspace can declare named **sources**
  (a file path or directory, an optional glob, a default source label and a
  default segment). The CLI (`source`, `ingest`, `ingest-log`) and the HTTP API
  (`/api/sources`, `/api/ingest`, `/api/ingest-runs`) can run one source or every
  enabled source. Runs are idempotent (items already in the workspace are
  skipped), isolated per file (a malformed CSV is recorded as an error and does
  not abort the run) and append one `IngestRun` plus one `EditEvent` with action
  `ingest` so the decision log stays complete. This makes weekly ingestion safe
  for cron and a Kubernetes `CronJob`.
- **Clustering:** dependency-free TF-IDF + leader clustering with configurable
  similarity threshold and minimum support.
- **Uncertainty:** clusters below the support threshold, with low cohesion or
  with low separation from the nearest cluster are flagged.
- **Report:** weekly web view plus CSV and PDF export; representative quotes
  chosen by closeness to the cluster centroid; no opaque score ranking.
- **Corrections:** merge, split, discard/restore and rename with an append-only
  history so every change is auditable.
- **Reviewer accounts and decision log:** a lightweight list of named reviewers
  in the workspace; every merge, split, discard, restore, rename, re-cluster,
  import and scheduled ingest can be attributed to a reviewer and carry a short
  rationale. The
  decision log is exported alongside the report (JSON, CSV, HTML, PDF) and is
  visible on the dashboard, turning the correction history into an auditable
  decision log without authentication or a database.
- **Per-reviewer weekly digest:** `digest --reviewer NAME` on the CLI,
  `GET /api/digest?reviewer=NAME` over HTTP and a compact "My digest" card on
  the dashboard summarise the weekly report for one reviewer: the active
  clusters whose most recent decision they made, the uncertain clusters still
  awaiting their decision (sorted by size and number of flags), open
  uncertainty counts per flag and their recent decisions. The digest is
  **read-only** — it never modifies the workspace, clusters or history, so it
  can be generated as often as needed and closes roadmap item 4 without adding
  a dependency, database or external service.
- **Review queue (claim / release / resolve):** any reviewer can *claim* an
  uncertain cluster so teammates can see it is taken, *release* it back to the
  backlog, or *resolve* it with a note. Claims live in the workspace JSON as
  `Claim` records (no database) and are surfaced through `claim`, `release`,
  `resolve` and `queue` on the CLI, `GET /api/queue` and the
  `POST /api/clusters/{id}/{claim,release,resolve}` endpoints, the dashboard
  "Review queue" card and the per-reviewer digest, which splits the backlog
  into **claimed by me**, **claimed by others** and **unclaimed**. Claiming is
  idempotent for the current claimant and rejected for another reviewer unless
  `--force` reassigns it; only the claimant (or an empty reviewer) may release.
  Every claim, release and resolve appends exactly one `EditEvent`, so the
  decision log and the exported CSV/HTML/PDF "Review queue" sections stay
  complete and auditable.
- **Persistence:** single JSON workspace file; works without a database.
- **Operations:** `/healthz` endpoint, WSGI server, container image and Helm
  chart for the existing k3s / Argo CD setup.

## Architecture

```
feedback_triage/
  models.py    domain dataclasses and serialisation (items, clusters, sources,
               ingest runs, reviewers, claims, history)
  ingest.py    CSV parsing with column auto-detection
  cluster.py   tokenisation, TF-IDF vectors, leader clustering, uncertainty
  store.py     JSON workspace, source registry, ingestion runs, corrections,
               review queue (claim / release / resolve)
  report.py    weekly report, per-reviewer digest and review queue aggregation,
               CSV/HTML/PDF
  web.py       WSGI application (dashboard, JSON API, digest, exports, health)
  cli.py       command line entry point
  __main__.py  `python -m feedback_triage`
```

The design stays intentionally dependency-free (Python standard library only),
so it is resource-efficient, easy to test offline and cheap to operate.

## Next direction

Shipped this cycle: the **review queue** — reviewer assignment for uncertain
clusters. Any reviewer can `claim`, `release` and `resolve` a cluster through
the CLI, `GET /api/queue` and the `POST /api/clusters/{id}/{claim,release,resolve}`
endpoints; the dashboard "Review queue" card exposes the same actions; and the
digest splits the uncertain backlog into claimed-by-me, claimed-by-others and
unclaimed. This marks **assignment** (roadmap item 5) as shipped.

Previously shipped: the **per-reviewer weekly digest** (roadmap item 4) — the
read-only `digest` command, `GET /api/digest` and the dashboard "My digest"
card show each reviewer the active clusters whose most recent decision they
made, the uncertain clusters still awaiting their decision, open uncertainty
counts and their recent decisions.

Remaining follow-ups:

1. Real per-source connectors (support desk, app-store and CRM APIs) that feed
   the source registry without a manual CSV export; the scheduled ingestion
   pipeline, run log and idempotent dedupe shipped here are the foundation. A
   Kubernetes `CronJob` already covers the scheduled part for CSV files.
2. Embedding-based similarity as an optional upgrade over TF-IDF.
3. Multi-language stop-word handling and per-language clustering.
4. Digest delivery through Slack or e-mail (the in-app, CLI and HTTP digest
   shipped here is the payload that would be delivered).
5. Load balancing that assigns clusters across reviewers: with claim/release
   events plus the per-reviewer digest, the load per reviewer is now recorded
   and visible, so the data the balancing rule needs already exists. The next
   step is a suggested-assignment rule on top of that data.
6. Volume-based subscription tiers (99 EUR up to 2,000 items / 299 EUR up to
   10,000 items per workspace).

## Validation plan (from the pitch)

Two weeks: five semi-structured interviews with product owners receiving at
least 100 feedback items per week, plus a concierge prototype with a design
partner (500 real items clustered, corrected and rated). Success criteria
before further investment: three of five teams halve triage time, at least 70%
of clusters accepted unchanged, and at least two teams commit to a paid pilot.

Reviewer attribution strengthens this plan: because every correction is tied to
a named reviewer and an optional rationale, the prototype produces a defensible
audit trail instead of an anonymous edit list. Interviews can reconstruct *why*
a theme was merged, split or discarded, and disputed prioritisation decisions
can be traced back to the person and the note that justified them, which makes
the "defensible prioritisation" promise verifiable rather than aspirational.

The per-reviewer digest makes that prototype measurable per person: it names
exactly which uncertain clusters still need a decision and how many open
uncertainty flags remain, so a design partner can see each reviewer's queue and
confirm whether the weekly report is actually being triaged. Because the digest
is read-only it can be produced as often as the trial needs without touching
the audit trail.

The **review queue** turns that visibility into a hard metric: every claim,
release and resolve appends an `EditEvent` attributed to a named reviewer, so
the prototype can compute *clusters resolved per reviewer* directly from the
`resolve` events instead of inferring it from the report. The interviews can
then ask "how many uncertain clusters did each reviewer resolve this week, and
how many were left unclaimed?" — a count that is backed by an append-only log,
not by self-reporting. This is the evidence behind the "halve triage time" and
"70% of clusters accepted unchanged" success criteria.

## Platform requirements

The product satisfies the factory criteria: it is a web application, original,
expandable, serves a clearly defined audience, is standalone, runs autonomously,
is containerised for k3s with Helm/Argo CD, stays within a small resource budget,
uses no external services, is covered by automated tests, and exposes a health
endpoint.

### Known risks

- Data access and consent for customer quotes can delay rollout.
- Cluster quality can suffer on very short, multilingual or ironic text.
- Without mandatory evidence users may distrust clusters as hallucination.
- Value depends on the discipline to actually read the report weekly.
- Pricing and willingness to pay are unproven.
