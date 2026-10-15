"""Command line interface for Feedback Triage Copilot."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

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

    sub.add_parser("cluster", help="re-cluster the workspace")

    rep = sub.add_parser("report", help="render the weekly report")
    rep.add_argument("--format", choices=["csv", "html", "pdf", "json"], default="csv")
    rep.add_argument("--out", default="")

    sub.add_parser("history", help="print the correction history")
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
        added = store.add_items(items, recluster=not args.no_recluster)
        print("Imported %d new items (%d in workspace)" % (added, len(store.items)))
        return 0

    if args.command == "cluster":
        clusters = store.recluster()
        print("Built %d clusters from %d items" % (len(clusters), len(store.items)))
        return 0

    if args.command == "history":
        for event in store.history:
            print("%s  %-10s %s" % (event.timestamp, event.action, event.details))
        return 0

    if args.command == "report":
        report = build_report(store)
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
