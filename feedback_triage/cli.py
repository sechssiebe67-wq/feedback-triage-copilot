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

    digest = sub.add_parser("digest", help="print the weekly triage digest")
    digest.add_argument(
        "--since", default="", help="ISO date/datetime start of the window"
    )
    digest.add_argument(
        "--days", type=int, default=7, help="window length in days (default 7)"
    )
    digest.add_argument("--format", choices=["text", "json"], default="text")

    reviewer = sub.add_parser("reviewer", help="manage reviewer accounts")
    reviewer_sub = reviewer.add_subparsers(dest="reviewer_command", required=True)
    reviewer_add = reviewer_sub.add_parser("add", help="add a reviewer")
    reviewer_add.add_argument("name")
    reviewer_sub.add_parser("list", help="list reviewers")

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


def _print_digest_text(digest: dict) -> None:
    print("Digest %s -> %s" % (digest["window_start"], digest["window_end"]))
    print("Items: %d total, %d new in window" % (
        digest["total_items"], digest["new_items"]))
    print("Topics: %d new, %d discarded" % (
        digest["new_clusters"], digest["discarded_clusters"]))
    print("Decisions in window: %d (unreviewed %d)" % (
        digest["decisions"], digest["unreviewed"]))
    load = digest.get("reviewer_load") or {}
    if load:
        print("Reviewer load:")
        for name in sorted(load):
            counts = load[name]
            detail = ", ".join(
                "%s=%d" % (action, count)
                for action, count in sorted(counts.items()) if action != "total"
            ) or "none"
            print("  %s: %s (total %d)" % (name, detail, counts.get("total", 0)))
    untouched = digest.get("untouched_topics") or []
    if untouched:
        print("Untouched topics: %s" % "; ".join(
            "%s (%s)" % (entry["label"], entry["size"]) for entry in untouched))
    changed = digest.get("changed_topics") or []
    if changed:
        print("Changed topics: %s" % "; ".join(
            "%s (%s)" % (entry["label"], entry["size"]) for entry in changed))


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

    if args.command == "digest":
        try:
            digest = build_digest(store, since=args.since or None, days=args.days)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.format == "json":
            import json

            print(json.dumps(digest, ensure_ascii=False, indent=2))
        else:
            _print_digest_text(digest)
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
