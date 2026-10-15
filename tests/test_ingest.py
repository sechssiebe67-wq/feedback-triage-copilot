import unittest

from feedback_triage.ingest import detect_mapping, parse_csv


class DetectMappingTests(unittest.TestCase):
    def test_detects_common_aliases(self):
        mapping = detect_mapping(
            ["Review Text", "Plan", "Channel", "Permalink", "Review Date", "Stars"]
        )
        self.assertEqual(mapping["text"], "Review Text")
        self.assertEqual(mapping["segment"], "Plan")
        self.assertEqual(mapping["source"], "Channel")
        self.assertEqual(mapping["url"], "Permalink")
        self.assertEqual(mapping["created_at"], "Review Date")
        self.assertEqual(mapping["rating"], "Stars")

    def test_detects_german_and_support_columns(self):
        mapping = detect_mapping(["Kommentar", "Kundensegment", "Ticket URL"])
        self.assertEqual(mapping["text"], "Kommentar")
        self.assertEqual(mapping["segment"], "Kundensegment")
        self.assertEqual(mapping["url"], "Ticket URL")

    def test_id_column_is_not_stolen_as_text(self):
        mapping = detect_mapping(["review_id", "review_body", "plan"])
        self.assertEqual(mapping["id"], "review_id")
        self.assertEqual(mapping["text"], "review_body")
        self.assertEqual(mapping["segment"], "plan")

    def test_ticket_id_column_is_not_stolen_as_text(self):
        mapping = detect_mapping(["ticket_id", "ticket_subject", "plan"])
        self.assertEqual(mapping["id"], "ticket_id")
        self.assertEqual(mapping["text"], "ticket_subject")


class ParseCsvTests(unittest.TestCase):
    def test_keeps_real_text_when_id_column_precedes_it(self):
        content = (
            "review_id,review_body,plan\n"
            "R-1,The app crashes on export,Pro\n"
        )
        items = parse_csv(content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "R-1")
        self.assertEqual(items[0].text, "The app crashes on export")
        self.assertEqual(items[0].segment, "Pro")

    def test_parses_rows_and_types(self):
        content = (
            "text,segment,source,url,date,rating\n"
            "PDF export broken,enterprise,support,http://x,2026-01-02,1\n"
            "SSO login fails,smb,review,http://y,2026-01-03,2.5\n"
        )
        items = parse_csv(content)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].segment, "enterprise")
        self.assertEqual(items[0].source, "support")
        self.assertEqual(items[0].url, "http://x")
        self.assertEqual(items[0].rating, 1.0)
        self.assertEqual(items[1].rating, 2.5)

    def test_semicolon_delimiter_is_sniffed(self):
        content = "text;segment\nBroken export;smb\n"
        items = parse_csv(content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].segment, "smb")

    def test_skips_empty_text_rows(self):
        content = "text,segment\n,foo\nReal feedback,bar\n"
        items = parse_csv(content)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "Real feedback")

    def test_missing_text_column_raises(self):
        with self.assertRaises(ValueError):
            parse_csv("segment,rating\nsmb,3\n")

    def test_explicit_mapping_overrides_detection(self):
        content = "body,kind\nLogin broken,bug\n"
        items = parse_csv(content, mapping={"text": "body", "segment": "kind"})
        self.assertEqual(items[0].text, "Login broken")
        self.assertEqual(items[0].segment, "bug")

    def test_defaults_for_missing_fields(self):
        items = parse_csv("text\nHello world\n", source="manual")
        self.assertEqual(items[0].source, "manual")
        self.assertEqual(items[0].segment, "unknown")
        self.assertTrue(items[0].id)


if __name__ == "__main__":
    unittest.main()
