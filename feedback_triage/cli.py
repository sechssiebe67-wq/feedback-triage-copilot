"""Command line interface for Feedback Triage Copilot."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .ingest import load_csv_file
from .report import (
    build_digest,
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from .store import TriageStore


DEFAULT_WORKSPACE = "workspace.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feedback_triage",
        description="Evidence-backed weekly feedback triage.",
    )
    parser.add_argument(
        "--workspace", default=DEFAULT_WORKSPACE,
        help="path to the JSON workspace file",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the web application")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    imp = sub.add_parser("import", help="import a feedback CSV")
    imp.add_argument("file")
    imp.add_argument("--source", default="import")
    imp.add_argument("--no-recluster", action="store_true")
    imp.add_argument("--reviewer", default="", help="reviewer name or id")
    imp.add_argument("--note", default="", help="optional rationale")

    cluster = sub.add_parser("cluster", help="re-cluster the workspace")
    cluster.add_argument("--reviewer", default="", help="reviewer name or id")
    cluster.add_argument("--note", default="", help="optional rationale")

    rep = sub.add_parser("report", help="render the weekly report")
    rep.add_argument("--format", choices=["csv", "html", "pdf", "json"], default="csv")
    rep.add_argument("--out", default="")
    rep.add_argument("--reviewer", default="", help="reviewer name for the report header")
    rep.add_argument("--note", default="", help="optional note for the report header")

    reviewer = sub.add_parser("reviewer", help="manage reviewer accounts")
    reviewer_sub = reviewer.add_subparsers(dest="reviewer_command", required=True)
    reviewer_add = reviewer_sub.add_parser("add", help="add a reviewer")
    reviewer_add.add_argument("name")
    reviewer_sub.add_parser("list", help="list reviewers")

    source = sub.add_parser("source", help="manage ingestion sources")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    source_add = source_sub.add_parser("add", help="register a source")
    source_add.add_argument("name")
    source_add.add_argument("path")
    source_add.add_argument("--glob", default="", help="glob applied inside a directory")
    source_add.add_argument("--label", default="", help="default feedback source label")
    source_add.add_argument("--segment", default="", help="default segment for rows without one")
    source_sub.add_parser("list", help="list sources")
    for action in ("enable", "disable", "remove"):
        action_parser = source_sub.add_parser(action, help="%s a source" % action)
        action_parser.add_argument("id")

    ingest = sub.add_parser("ingest", help="run one or all enabled sources")
    ingest.add_argument("--source", default="", help="source id (default: all enabled)")
    ingest.add_argument("--reviewer", default="", help="reviewer name or id")
    ingest.add_argument("--note", default="", help="optional rationale")

    ingest_log = sub.add_parser("ingest-log", help="print recent ingestion runs")
    ingest_log.add_argument("--limit", type=int, default=20)

    digest = sub.add_parser("digest", help="weekly digest for one reviewer")
    digest.add_argument("--reviewer", default="", help="reviewer name or id")
    digest.add_argument(
        "--limit", type=int, default=10, help="recent decisions to list"
    )
    digest.add_argument("--format", choices=["text", "json"], default="text")

    sub.add_parser("history", help="print the correction history")
    sub.add_parser("decisions", help="print the attributed decision log")
    return parser


def _print_bytes(data: bytes, out: str) -> None:
    if out:
        with open(out, "wb") as handle:
            handle.write(data)
        print("Wrote %s" % out)
    else:
        sys.stdout.buffer.write(data)


def _format_run(run) -> str:
    line = "%s  %-16s added=%d skipped=%d files=%d" % (
        run.timestamp, run.source_name, run.added, run.skipped, len(run.files),
    )
    if run.errors:
        line += " errors=%d" % len(run.errors)
    if run.reviewer:
        line += " reviewer=%s" % run.reviewer
    return line


def _format_digest_text(digest) -> str:
    totals = digest["totals"]
    lines = [
        "Weekly digest for %s (generated %s)"
        % (digest["reviewer"] or "unknown reviewer", digest["generated_at"]),
        "%d items, %d clusters, %d uncertain"
        % (totals["items"], totals["clusters"], totals["uncertain_clusters"]),
        "Assigned to you: %d cluster(s)" % len(digest["assigned"]),
        "Clusters needing review: %d" % len(digest["needs_review"]),
    ]
    for cluster in digest["needs_review"]:
        flags = ", ".join(cluster["uncertainty"]) or "none"
        lines.append(
            "  - %s (%d quotes) [%s]"
            % (cluster["label"], cluster["size"], flags)
        )
    counts = digest["open_uncertainties"]
    lines.append(
        "Open uncertainties: " + ", ".join(
            "%s=%d" % (flag, counts.get(flag, 0))
            for flag in ("few_evidence", "low_cohesion", "low_separation")
        )
    )
    if digest["recent_decisions"]:
        lines.append("Recent decisions:")
        for decision in digest["recent_decisions"]:
            line = "  - %s %s" % (decision["timestamp"], decision["action"])
            if decision["clusters"]:
                line += " [%s]" % ", ".join(decision["clusters"])
            if decision["note"]:
                line += ": %s" % decision["note"]
            lines.append(line)
    return "\n".join(lines)


def _handle_source(store: TriageStore, args) -> int:
    if args.source_command == "add":
        source = store.add_source(
            args.name,
            args.path,
            glob=args.glob,
            source_label=args.label,
            segment_default=args.segment,
        )
        print("Added source %s (%s) -> %s" % (source.name, source.id, source.path))
    elif args.source_command == "list":
        sources = store.list_sources()
        if not sources:
            print("No sources configured.")
        for source in sources:
            state = "enabled" if source.enabled else "disabled"
            print("  %-14s %-10s %-8s last_run=%s added=%d skipped=%d" % (
                source.id,
                source.name[:10],
                state,
                source.last_run_at or "-",
                source.last_added,
                source.last_skipped,
            ))
    elif args.source_command == "enable":
        source = store.set_source_enabled(args.id, True)
        print("Enabled source %s" % source.name)
    elif args.source_command == "disable":
        source = store.set_source_enabled(args.id, False)
        print("Disabled source %s" % source.name)
    elif args.source_command == "remove":
        source = store.remove_source(args.id)
        print("Removed source %s" % source.name)
    return 0


def _handle_ingest(store: TriageStore, args) -> int:
    if args.source:
        runs = [store.run_source(args.source, reviewer=args.reviewer, note=args.note)]
    else:
        runs = store.run_all_sources(reviewer=args.reviewer, note=args.note)
    total_added = 0
    total_skipped = 0
    for run in runs:
        total_added += run.added
        total_skipped += run.skipped
        print(_format_run(run))
    print("Total: %d added, %d skipped across %d source(s), %d items in workspace"
          % (total_added, total_skipped, len(runs), len(store.items)))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = TriageStore.load(args.workspace)

    if args.command == "serve":
        from wsgiref.simple_server import make_server

        from .web import FeedbackTriageApp

        app = FeedbackTriageApp(store)
        httpd = make_server(args.host, args.port, app)
        print("Serving on http://%s:%d (workspace: %s)" % (
            args.host, args.port, args.workspace))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if args.command == "import":
        items = load_csv_file(args.file, source=args.source)
        added = store.add_items(
            items,
            recluster=not args.no_recluster,
            reviewer=args.reviewer,
            note=args.note,
        )
        print("Imported %d new items (%d in workspace)" % (added, len(store.items)))
        return 0

    if args.command == "cluster":
        clusters = store.recluster(reviewer=args.reviewer, note=args.note)
        print("Built %d clusters from %d items" % (len(clusters), len(store.items)))
        return 0

    if args.command == "reviewer":
        if args.reviewer_command == "add":
            reviewer = store.add_reviewer(args.name)
            print("Added reviewer %s (%s)" % (reviewer.name, reviewer.id))
        else:
            for reviewer in store.list_reviewers():
                print("%s  %s" % (reviewer.id, reviewer.name))
        return 0

    if args.command == "source":
        return _handle_source(store, args)

    if args.command == "ingest":
        return _handle_ingest(store, args)

    if args.command == "ingest-log":
        runs = list(store.ingest_runs)[-max(args.limit, 0):]
        for run in runs:
            print(_format_run(run))
        return 0

    if args.command == "digest":
        if not args.reviewer:
            print("No reviewer given. Use --reviewer NAME.")
            return 2
        digest = build_digest(store, args.reviewer, limit=args.limit)
        registered = any(
            reviewer.id == args.reviewer
            or reviewer.name.lower() == args.reviewer.lower()
            for reviewer in store.reviewers
        )
        if not registered:
            print(
                "Reviewer %r is not registered; showing an empty digest."
                % args.reviewer
            )
        if args.format == "json":
            import json

            print(json.dumps(digest, ensure_ascii=False, indent=2))
        else:
            print(_format_digest_text(digest))
        return 0

    if args.command == "history":
        for event in store.history:
            print("%s  %-10s %s" % (event.timestamp, event.action, event.details))
        return 0

    if args.command == "decisions":
        report = build_report(store)
        for decision in report["decisions"]:  # type: ignore[index]
            line = "%s  %-10s %-14s %s" % (
                decision["timestamp"], decision["action"],
                decision["reviewer"] or "unreviewed", decision["note"],
            )
            clusters = decision.get("clusters") or []
            if clusters:
                line += "  [%s]" % ", ".join(clusters)
            print(line)
        return 0

    if args.command == "report":
        report = build_report(store, reviewer=args.reviewer, note=args.note)
        if args.format == "csv":
            _print_bytes(report_to_csv(report).encode("utf-8"), args.out)
        elif args.format == "html":
            _print_bytes(report_to_html(report).encode("utf-8"), args.out)
        elif args.format == "pdf":
            _print_bytes(report_to_pdf(report), args.out)
        else:
            import json

            _print_bytes(
                json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                args.out,
            )
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
