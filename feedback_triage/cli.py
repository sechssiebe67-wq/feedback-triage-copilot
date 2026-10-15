"""Command line interface for Feedback Triage Copilot."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .digest import build_digest, digest_to_html, digest_to_json, digest_to_text
from .ingest import load_csv_file
from .report import build_report, report_to_csv, report_to_html, report_to_pdf
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

    digest = sub.add_parser("digest", help="render the per-reviewer digest")
    digest.add_argument("--reviewer", default="", help="reviewer name or id")
    digest.add_argument(
        "--format", choices=["text", "html", "json"], default="text"
    )
    digest.add_argument("--out", default="")

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

    if args.command == "digest":
        digest = build_digest(store, reviewer=args.reviewer)
        if args.format == "json":
            payload = digest_to_json(digest)
        elif args.format == "html":
            payload = digest_to_html(digest)
        else:
            payload = digest_to_text(digest)
        _print_bytes(payload.encode("utf-8"), args.out)
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
