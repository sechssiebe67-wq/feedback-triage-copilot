import unittest

from feedback_triage.models import FeedbackItem
from feedback_triage.report import (
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from feedback_triage.store import TriageStore


def decision_store():
    store = make_store()
    reviewer = store.add_reviewer("Alice")
    cluster = store.active_clusters()[0]
    store.discard(cluster.id, reviewer=reviewer.id, note="duplicate of PDF export")
    return store, reviewer


def make_store():
    store = TriageStore()
    store.add_items([
        FeedbackItem(id="a", text="PDF export is broken", segment="ent",
                     source="support", url="http://x", created_at="2026-01-01"),
        FeedbackItem(id="b", text="PDF export keeps failing", segment="smb",
                     source="support", url="http://y", created_at="2026-01-03"),
        FeedbackItem(id="c", text="SSO login does not work", segment="ent",
                     source="review", url="http://z", created_at="2026-01-02"),
    ])
    return store


class BuildReportTests(unittest.TestCase):
    def test_totals(self):
        report = build_report(make_store())
        self.assertEqual(report["totals"]["items"], 3)
        self.assertGreaterEqual(report["totals"]["clusters"], 2)
        self.assertEqual(report["totals"]["sources"]["support"], 2)

    def test_cluster_carries_evidence(self):
        report = build_report(make_store())
        for cluster in report["clusters"]:
            self.assertTrue(cluster["quotes"])
            self.assertEqual(len(cluster["quotes"]), min(cluster["size"], 3))
            self.assertIsInstance(cluster["uncertainty"], list)

    def test_report_includes_digest(self):
        report = build_report(make_store())
        self.assertIn("digest", report)
        digest = report["digest"]
        self.assertIn("window_start", digest)
        self.assertIn("window_end", digest)
        self.assertIn("reviewer_load", digest)
        self.assertIn("unreviewed", digest)

    def test_reports_first_and_last_seen(self):
        report = build_report(make_store())
        pdf_cluster = next(
            c for c in report["clusters"] if "PDF" in c["label"].upper()
        )
        self.assertEqual(pdf_cluster["first_seen"], "2026-01-01")
        self.assertEqual(pdf_cluster["last_seen"], "2026-01-03")


class CsvTests(unittest.TestCase):
    def test_csv_has_header_and_quote_rows(self):
        csv_text = report_to_csv(build_report(make_store()))
        lines = csv_text.strip().splitlines()
        self.assertIn("cluster_id", lines[0])
        self.assertIn("quote_text", lines[0])
        self.assertGreaterEqual(len(lines), 4)

    def test_empty_report_still_has_header(self):
        csv_text = report_to_csv(build_report(TriageStore()))
        lines = csv_text.strip().splitlines()
        self.assertIn("cluster_id", lines[0])
        self.assertIn("digest_total_items", lines[0])
        self.assertEqual(len(lines), 2)  # header + digest summary row

    def test_csv_neutralises_formula_injection(self):
        store = TriageStore()
        store.add_items([
            FeedbackItem(id="=1+1", text="=cmd|'/C calc'!A1", segment="@seg"),
            FeedbackItem(id="q", text="=cmd|'/C calc'!A1 second", segment="@seg"),
        ])
        csv_text = report_to_csv(build_report(store))
        self.assertIn("'=1+1", csv_text)
        self.assertIn("'=cmd|", csv_text)
        self.assertIn("'@seg", csv_text)
        for line in csv_text.splitlines()[1:]:
            for cell in line.split(","):
                self.assertFalse(
                    cell.startswith(("=", "+", "@", "\t", "\r")),
                    "formula-capable cell not neutralised: %r" % cell,
                )

    def test_csv_neutralises_leading_plus_and_minus(self):
        store = TriageStore()
        store.add_items([
            FeedbackItem(id="a", text="+1+1", url="-2+3"),
            FeedbackItem(id="b", text="+1+1 again", url="-2+3"),
        ])
        csv_text = report_to_csv(build_report(store))
        self.assertIn("'+1+1", csv_text)
        self.assertIn("'-2+3", csv_text)
        self.assertNotIn(",+1+1", csv_text)
        self.assertNotIn(",-2+3", csv_text)


class HtmlTests(unittest.TestCase):
    def test_html_escapes_content(self):
        store = TriageStore()
        store.add_items([
            FeedbackItem(id="x", text="<script>alert(1)</script> broken export"),
            FeedbackItem(id="y", text="broken export again"),
        ])
        html = report_to_html(build_report(store))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_contains_uncertainty_flags(self):
        html = report_to_html(build_report(make_store()))
        self.assertIn("few_evidence", html)

    def test_html_escapes_segment_distribution(self):
        store = TriageStore()
        store.add_items([
            FeedbackItem(id="x", text="broken export page",
                         segment="<img src=x onerror=alert(1)>"),
            FeedbackItem(id="y", text="broken export page two",
                         segment="<img src=x onerror=alert(1)>"),
        ])
        html = report_to_html(build_report(store))
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_html_drops_non_http_url_schemes(self):
        store = TriageStore()
        store.add_items([
            FeedbackItem(id="x", text="broken export page", url="javascript:alert(1)"),
            FeedbackItem(id="y", text="broken export page two", url="javascript:alert(2)"),
        ])
        html = report_to_html(build_report(store))
        self.assertNotIn("javascript:", html)
        self.assertNotIn('href=""', html)

    def test_html_keeps_http_links(self):
        html = report_to_html(build_report(make_store()))
        self.assertIn('href="http://', html)
        self.assertIn('rel="noopener noreferrer"', html)


class DecisionLogTests(unittest.TestCase):
    def test_report_payload_has_resolved_decisions(self):
        store, reviewer = decision_store()
        report = build_report(store)
        self.assertIn("decisions", report)
        latest = report["decisions"][0]
        self.assertEqual(latest["action"], "discard")
        self.assertEqual(latest["reviewer"], "Alice")
        self.assertEqual(latest["note"], "duplicate of PDF export")
        self.assertTrue(latest["timestamp"])
        self.assertTrue(latest["clusters"])

    def test_decision_falls_back_to_raw_reviewer_value(self):
        store = make_store()
        store.discard(store.active_clusters()[0].id, reviewer="Alice", note="x")
        report = build_report(store)
        self.assertEqual(report["decisions"][0]["reviewer"], "Alice")

    def test_report_header_reviewer_and_note(self):
        report = build_report(make_store(), reviewer="Alice", note="week 42")
        self.assertEqual(report["reviewer"], "Alice")
        self.assertEqual(report["note"], "week 42")

    def test_csv_has_decision_columns_and_rows(self):
        store, _ = decision_store()
        csv_text = report_to_csv(build_report(store))
        header = csv_text.splitlines()[0]
        for column in (
            "decision_timestamp", "decision_action", "decision_reviewer",
            "decision_note", "decision_clusters",
        ):
            self.assertIn(column, header)
        self.assertIn("duplicate of PDF export", csv_text)
        self.assertIn("discard", csv_text)

    def test_csv_neutralises_decision_formula_injection(self):
        store = make_store()
        store.discard(
            store.active_clusters()[0].id, reviewer="=cmd", note="+1+1",
        )
        csv_text = report_to_csv(build_report(store))
        self.assertIn("'=cmd", csv_text)
        self.assertIn("'+1+1", csv_text)

    def test_html_renders_decision_log_and_escapes(self):
        store = make_store()
        reviewer = store.add_reviewer("<b>Eve</b>")
        store.discard(
            store.active_clusters()[0].id,
            reviewer=reviewer.id,
            note="</script><script>alert(1)</script>",
        )
        html = report_to_html(build_report(store))
        self.assertIn("Decision log", html)
        self.assertNotIn("<b>Eve</b>", html)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_pdf_renders_decision_log(self):
        store, _ = decision_store()
        pdf = report_to_pdf(build_report(store))
        self.assertIn(b"Decision log", pdf)
        self.assertIn(b"Alice", pdf)


class PdfTests(unittest.TestCase):
    def test_pdf_structure(self):
        pdf = report_to_pdf(build_report(make_store()))
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/BaseFont /Helvetica", pdf)

    def test_pdf_handles_empty_report(self):
        pdf = report_to_pdf(build_report(TriageStore()))
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))

    def test_pdf_wraps_long_quote(self):
        store = TriageStore()
        long_text = "export " * 400
        store.add_items([
            FeedbackItem(id="x", text=long_text),
            FeedbackItem(id="y", text=long_text + " broken"),
        ])
        pdf = report_to_pdf(build_report(store))
        self.assertGreater(pdf.count(b"/Type /Page "), 1)


if __name__ == "__main__":
    unittest.main()
