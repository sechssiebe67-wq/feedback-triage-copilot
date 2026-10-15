import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from feedback_triage.models import FeedbackItem, now_iso
from feedback_triage.report import (
    build_digest,
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from feedback_triage.store import TriageStore


def sample_items():
    return [
        FeedbackItem(id="a", text="PDF export is broken", segment="ent",
                     source="support", url="http://x", created_at="2026-01-01"),
        FeedbackItem(id="b", text="PDF export keeps failing", segment="smb",
                     source="support", url="http://y", created_at="2026-01-03"),
        FeedbackItem(id="c", text="SSO login does not work", segment="ent",
                     source="review", url="http://z", created_at="2026-01-02"),
        FeedbackItem(id="d", text="SSO login fails again", segment="ent",
                     source="review", url="http://w", created_at="2026-01-04"),
    ]


def reviewed_store():
    store = TriageStore()
    alice = store.add_reviewer("Alice")
    bob = store.add_reviewer("Bob")
    store.add_items(sample_items(), reviewer=alice.id, note="weekly batch")
    return store, alice, bob


class EmptyDigestTests(unittest.TestCase):
    def test_empty_workspace_counters_are_zero(self):
        digest = build_digest(TriageStore())
        self.assertEqual(digest["total_items"], 0)
        self.assertEqual(digest["new_items"], 0)
        self.assertEqual(digest["new_clusters"], 0)
        self.assertEqual(digest["discarded_clusters"], 0)
        self.assertEqual(digest["decisions"], 0)
        self.assertEqual(digest["unreviewed"], 0)
        self.assertEqual(digest["reviewer_load"], {})
        self.assertEqual(digest["untouched_topics"], [])
        self.assertEqual(digest["changed_topics"], [])

    def test_empty_workspace_windows_are_present(self):
        digest = build_digest(TriageStore())
        self.assertTrue(digest["window_start"])
        self.assertTrue(digest["window_end"])
        self.assertEqual(digest["since"], "")

    def test_empty_report_renders_digest_without_raising(self):
        report = build_report(TriageStore())
        self.assertIn("This week", report_to_html(report))
        self.assertTrue(report_to_pdf(report).startswith(b"%PDF"))
        csv_text = report_to_csv(report)
        self.assertIn("digest_total_items", csv_text)


class ReviewerLoadTests(unittest.TestCase):
    def test_two_reviewers_and_one_unreviewed_decision(self):
        store, alice, bob = reviewed_store()
        active = store.active_clusters()
        self.assertGreaterEqual(len(active), 2)
        store.discard(active[0].id, reviewer=bob.id, note="noise")
        store.rename(active[1].id, "Login problems", reviewer="")
        digest = build_digest(store)
        self.assertEqual(digest["decisions"], 3)
        self.assertEqual(digest["unreviewed"], 1)
        load = digest["reviewer_load"]
        self.assertEqual(load["Alice"]["import"], 1)
        self.assertEqual(load["Alice"]["total"], 1)
        self.assertEqual(load["Bob"]["discard"], 1)
        self.assertEqual(load["Bob"]["total"], 1)
        self.assertNotIn("", load)

    def test_decisions_by_action_counts_each_type(self):
        store, _, bob = reviewed_store()
        active = store.active_clusters()
        store.discard(active[0].id, reviewer=bob.id)
        store.restore(active[0].id, reviewer=bob.id)
        digest = build_digest(store)
        self.assertEqual(digest["decisions_by_action"]["import"], 1)
        self.assertEqual(digest["decisions_by_action"]["discard"], 1)
        self.assertEqual(digest["decisions_by_action"]["restore"], 1)

    def test_changed_topics_and_untouched_topics(self):
        store, _, bob = reviewed_store()
        active = store.active_clusters()
        changed = active[0]
        store.discard(changed.id, reviewer=bob.id, note="noise")
        digest = build_digest(store)
        changed_ids = {entry["id"] for entry in digest["changed_topics"]}
        self.assertIn(changed.id, changed_ids)
        untouched_ids = {entry["id"] for entry in digest["untouched_topics"]}
        self.assertIn(active[1].id, untouched_ids)
        self.assertNotIn(changed.id, untouched_ids)

    def test_changed_topics_are_escaped_in_html(self):
        store = TriageStore()
        reviewer = store.add_reviewer("<b>Eve</b>")
        store.add_items(
            [
                FeedbackItem(id="a", text="Export is broken"),
                FeedbackItem(id="b", text="Export keeps failing"),
            ],
            reviewer=reviewer.id,
            note="</script><script>alert(1)</script>",
        )
        cluster = store.active_clusters()[0]
        store.discard(
            cluster.id,
            reviewer=reviewer.id,
            note="</script><script>alert(1)</script>",
        )
        html = report_to_html(build_report(store))
        self.assertNotIn("<b>Eve</b>", html)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class WindowTests(unittest.TestCase):
    def test_since_boundary_is_inclusive(self):
        store, _, _ = reviewed_store()
        event = store.history[0]
        digest = build_digest(store, since=event.timestamp)
        self.assertGreaterEqual(digest["decisions"], 1)
        self.assertEqual(digest["since"], event.timestamp)

    def test_since_after_events_excludes_them(self):
        store, _, _ = reviewed_store()
        event = store.history[0]
        later = (
            datetime.fromisoformat(event.timestamp) + timedelta(seconds=1)
        ).isoformat()
        digest = build_digest(store, since=later)
        self.assertEqual(digest["decisions"], 0)
        self.assertEqual(digest["unreviewed"], 0)

    def test_days_window_counts_recent_items_only(self):
        store = TriageStore()
        store.add_items(
            [
                FeedbackItem(id="old", text="Old feedback about exports",
                             created_at="2000-01-01"),
                FeedbackItem(id="current", text="Current feedback about exports",
                             created_at=now_iso()),
            ],
            recluster=False,
        )
        digest = build_digest(store, days=7)
        self.assertEqual(digest["total_items"], 2)
        self.assertEqual(digest["new_items"], 1)

    def test_invalid_since_raises(self):
        with self.assertRaises(ValueError):
            build_digest(TriageStore(), since="not-a-date")


class LegacyWorkspaceTests(unittest.TestCase):
    def test_legacy_workspace_loads_and_digests_zeroed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            legacy = {
                "version": 1,
                "settings": {"similarity_threshold": 0.14, "min_support": 2},
                "items": [{"id": "a", "text": "PDF export is broken"}],
                "clusters": [{"id": "c1", "label": "PDF", "item_ids": ["a"]}],
                "history": [
                    {"id": "e1", "timestamp": "2000-01-01T00:00:00+00:00",
                     "action": "discard", "details": {"cluster_id": "c1"}}
                ],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            store = TriageStore.load(path)
            digest = build_digest(store)
            self.assertEqual(digest["total_items"], 1)
            self.assertEqual(digest["decisions"], 0)
            self.assertEqual(digest["unreviewed"], 0)
            self.assertEqual(digest["reviewer_load"], {})
            store.save()
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertIn("items", saved)
            self.assertNotIn("digest", saved)


class CsvDigestTests(unittest.TestCase):
    def test_digest_reviewer_and_note_are_neutralised(self):
        store = TriageStore()
        reviewer = store.add_reviewer("=cmd")
        store.add_items(
            [
                FeedbackItem(id="a", text="Export is broken"),
                FeedbackItem(id="b", text="Export keeps failing"),
            ],
            reviewer=reviewer.id,
            note="+1+1",
        )
        store.discard(
            store.active_clusters()[0].id, reviewer=reviewer.id, note="+1+1"
        )
        csv_text = report_to_csv(build_report(store))
        self.assertIn("'=cmd", csv_text)
        self.assertIn("'+1+1", csv_text)
        self.assertIn("digest_notes", csv_text)

    def test_digest_summary_row_keeps_quote_rows(self):
        import csv as csv_module
        import io

        store, _, _ = reviewed_store()
        csv_text = report_to_csv(build_report(store))
        rows = list(csv_module.reader(io.StringIO(csv_text)))
        header, digest_row = rows[0], rows[1]
        self.assertIn("cluster_id", header)
        self.assertIn("decision_action", header)
        self.assertIn("digest_window_start", header)
        indexed = dict(zip(header, digest_row))
        self.assertTrue(indexed["digest_window_start"])
        self.assertEqual(indexed["digest_total_items"], "4")
        self.assertGreaterEqual(len(rows), 3)  # header + digest + quote rows


if __name__ == "__main__":
    unittest.main()
