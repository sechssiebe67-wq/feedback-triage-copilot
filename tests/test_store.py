import json
import os
import tempfile
import unittest

from feedback_triage.models import ACTIVE, DISCARDED, FeedbackItem
from feedback_triage.store import TriageStore


def sample_items():
    return [
        FeedbackItem(id="a", text="PDF export is broken", segment="ent", source="support"),
        FeedbackItem(id="b", text="PDF export keeps failing", segment="smb", source="support"),
        FeedbackItem(id="c", text="SSO login does not work", segment="ent", source="review"),
        FeedbackItem(id="d", text="SSO login fails", segment="ent", source="review"),
    ]


def make_store(path=None):
    store = TriageStore(path=path)
    store.add_items(sample_items())
    return store


class AddItemsTests(unittest.TestCase):
    def test_deduplicates_by_id(self):
        store = make_store()
        again = store.add_items(sample_items())
        self.assertEqual(again, 0)
        self.assertEqual(len(store.items), 4)

    def test_records_import_history(self):
        store = make_store()
        self.assertEqual(store.history[0].action, "import")
        self.assertEqual(store.history[0].details["added"], 4)


class CorrectionTests(unittest.TestCase):
    def test_merge_discards_sources_and_creates_cluster(self):
        store = make_store()
        active = store.active_clusters()
        self.assertGreaterEqual(len(active), 2)
        merged = store.merge([c.id for c in active])
        self.assertEqual(len(merged.item_ids), 4)
        self.assertEqual(merged.origin, "manual")
        active_ids = {c.id for c in store.active_clusters()}
        self.assertIn(merged.id, active_ids)
        for cluster in active:
            self.assertEqual(cluster.status, DISCARDED)
        self.assertEqual(store.history[-1].action, "merge")

    def test_split_moves_items_and_keeps_rest(self):
        store = make_store()
        cluster = store.active_clusters()[0]
        moving = cluster.item_ids[:1]
        new_cluster = store.split(cluster.id, moving)
        self.assertEqual(new_cluster.item_ids, moving)
        self.assertNotIn(moving[0], cluster.item_ids)
        self.assertEqual(store.history[-1].action, "split")

    def test_split_requires_items(self):
        store = make_store()
        with self.assertRaises(ValueError):
            store.split(store.active_clusters()[0].id, [])

    def test_discard_and_restore(self):
        store = make_store()
        cluster = store.active_clusters()[0]
        store.discard(cluster.id)
        self.assertEqual(cluster.status, DISCARDED)
        store.restore(cluster.id)
        self.assertEqual(cluster.status, ACTIVE)

    def test_rename_records_old_and_new(self):
        store = make_store()
        cluster = store.active_clusters()[0]
        store.rename(cluster.id, "Renamed topic")
        self.assertEqual(cluster.label, "Renamed topic")
        event = store.history[-1]
        self.assertEqual(event.action, "rename")
        self.assertEqual(event.details["to"], "Renamed topic")

    def test_unknown_cluster_raises(self):
        store = make_store()
        with self.assertRaises(KeyError):
            store.discard("does-not-exist")


class ReviewerTests(unittest.TestCase):
    def test_add_get_and_list_reviewers(self):
        store = TriageStore()
        reviewer = store.add_reviewer("Alice")
        self.assertTrue(reviewer.id)
        self.assertEqual(reviewer.name, "Alice")
        self.assertEqual(store.list_reviewers(), [reviewer])
        self.assertEqual(store.get_reviewer(reviewer.id), reviewer)
        self.assertEqual(store.reviewer_name(reviewer.id), "Alice")

    def test_add_reviewer_requires_name(self):
        with self.assertRaises(ValueError):
            TriageStore().add_reviewer("   ")

    def test_add_reviewer_is_case_insensitive_dedupe(self):
        store = TriageStore()
        first = store.add_reviewer("Alice")
        again = store.add_reviewer("alice")
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(store.list_reviewers()), 1)

    def test_unknown_reviewer_name_passes_through(self):
        store = TriageStore()
        self.assertEqual(store.reviewer_name("Nobody"), "Nobody")
        self.assertEqual(store.reviewer_name(""), "")

    def test_get_unknown_reviewer_raises(self):
        with self.assertRaises(KeyError):
            TriageStore().get_reviewer("nope")


class AttributedDecisionTests(unittest.TestCase):
    def test_corrections_record_reviewer_and_note(self):
        store = make_store()
        active = store.active_clusters()
        store.merge([c.id for c in active], reviewer="Alice", note="same theme")
        self.assertEqual(store.history[-1].reviewer, "Alice")
        self.assertEqual(store.history[-1].note, "same theme")

    def test_discard_restore_rename_split_are_attributed(self):
        store = make_store()
        cluster = store.active_clusters()[0]
        store.discard(cluster.id, reviewer="Bob", note="noise")
        self.assertEqual(store.history[-1].reviewer, "Bob")
        store.restore(cluster.id, reviewer="Bob", note="back")
        self.assertEqual(store.history[-1].note, "back")
        store.rename(cluster.id, "New", reviewer="Bob", note="clearer")
        self.assertEqual(store.history[-1].action, "rename")
        self.assertEqual(store.history[-1].reviewer, "Bob")
        moved = cluster.item_ids[:1]
        store.split(cluster.id, moved, reviewer="Bob", note="distinct")
        self.assertEqual(store.history[-1].details["moved"], moved)
        self.assertEqual(store.history[-1].note, "distinct")

    def test_import_and_recluster_are_attributed(self):
        store = TriageStore()
        store.add_items(sample_items(), reviewer="Alice", note="weekly batch")
        self.assertEqual(store.history[-1].action, "import")
        self.assertEqual(store.history[-1].reviewer, "Alice")
        store.recluster(reviewer="Alice", note="retune")
        self.assertEqual(store.history[-1].action, "recluster")
        self.assertEqual(store.history[-1].note, "retune")

    def test_events_default_to_empty_attribution(self):
        store = make_store()
        event = store.history[-1]
        self.assertEqual(event.reviewer, "")
        self.assertEqual(event.note, "")


class PersistenceTests(unittest.TestCase):
    def test_reviewer_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workspace.json")
            store = make_store(path)
            reviewer = store.add_reviewer("Alice")
            store.discard(store.active_clusters()[0].id, reviewer=reviewer.id,
                          note="duplicate")
            loaded = TriageStore.load(path)
            self.assertEqual(len(loaded.list_reviewers()), 1)
            self.assertEqual(loaded.list_reviewers()[0].name, "Alice")
            self.assertEqual(loaded.history[-1].reviewer, reviewer.id)
            self.assertEqual(loaded.history[-1].note, "duplicate")

    def test_loads_legacy_workspace_without_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            legacy = {
                "version": 1,
                "settings": {"similarity_threshold": 0.14, "min_support": 2},
                "items": [{"id": "a", "text": "PDF export is broken"}],
                "clusters": [{"id": "c1", "label": "PDF", "item_ids": ["a"]}],
                "history": [
                    {"id": "e1", "timestamp": "2026-01-01T00:00:00+00:00",
                     "action": "discard", "details": {"cluster_id": "c1"}}
                ],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            store = TriageStore.load(path)
            self.assertEqual(store.list_reviewers(), [])
            self.assertEqual(store.history[0].reviewer, "")
            self.assertEqual(store.history[0].note, "")
            # Still writable and forward compatible after loading.
            store.add_reviewer("Alice")
            store.save()
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertIn("reviewers", saved)
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workspace.json")
            store = make_store(path)
            store.discard(store.active_clusters()[0].id)
            loaded = TriageStore.load(path)
            self.assertEqual(len(loaded.items), 4)
            self.assertEqual(len(loaded.history), len(store.history))
            statuses = sorted(c.status for c in loaded.clusters)
            self.assertIn(DISCARDED, statuses)

    def test_load_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TriageStore.load(os.path.join(tmp, "nope.json"))
            self.assertEqual(store.items, [])


if __name__ == "__main__":
    unittest.main()
