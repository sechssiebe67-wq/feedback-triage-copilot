import unittest

from feedback_triage.models import FeedbackItem
from feedback_triage.report import (
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from feedback_triage.store import TriageStore


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
        self.assertEqual(len(csv_text.strip().splitlines()), 1)


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
