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


class PersistenceTests(unittest.TestCase):
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
