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
- **Clustering:** dependency-free TF-IDF + leader clustering with configurable
  similarity threshold and minimum support.
- **Uncertainty:** clusters below the support threshold, with low cohesion or
  with low separation from the nearest cluster are flagged.
- **Report:** weekly web view plus CSV and PDF export; representative quotes
  chosen by closeness to the cluster centroid; no opaque score ranking.
- **Corrections:** merge, split, discard/restore and rename with an append-only
  history so every change is auditable.
- **Persistence:** single JSON workspace file; works without a database.
- **Operations:** `/healthz` endpoint, WSGI server, container image and Helm
  chart for the existing k3s / Argo CD setup.

## Architecture

```
feedback_triage/
  models.py    domain dataclasses and serialisation
  ingest.py    CSV parsing with column auto-detection
  cluster.py   tokenisation, TF-IDF vectors, leader clustering, uncertainty
  store.py     JSON workspace + correction history
  report.py    weekly report aggregation and CSV/HTML/PDF rendering
  web.py       WSGI application (dashboard, JSON API, exports, health)
  cli.py       command line entry point
  __main__.py  `python -m feedback_triage`
```

The design stays intentionally dependency-free (Python standard library only),
so it is resource-efficient, easy to test offline and cheap to operate.

## Next direction

1. Scheduled weekly ingestion jobs and per-source connectors.
2. Embedding-based similarity as an optional upgrade over TF-IDF.
3. Multi-language stop-word handling and per-language clustering.
4. Reviewer accounts, comments per cluster and exported decision log.
5. Slack/e-mail digest of the weekly report.
6. Volume-based subscription tiers (99 EUR up to 2,000 items / 299 EUR up to
   10,000 items per workspace).

## Validation plan (from the pitch)

Two weeks: five semi-structured interviews with product owners receiving at
least 100 feedback items per week, plus a concierge prototype with a design
partner (500 real items clustered, corrected and rated). Success criteria
before further investment: three of five teams halve triage time, at least 70%
of clusters accepted unchanged, and at least two teams commit to a paid pilot.

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
