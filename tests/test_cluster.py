import unittest

from feedback_triage.cluster import (
    build_tfidf,
    cluster_items,
    cosine,
    representative_quotes,
    tokenize,
)
from feedback_triage.models import FeedbackItem


def make_items(texts):
    return [FeedbackItem(id="i%d" % n, text=t) for n, t in enumerate(texts)]


class TokenizeTests(unittest.TestCase):
    def test_removes_stopwords_and_short_tokens(self):
        tokens = tokenize("The PDF export is broken and a bit slow")
        self.assertIn("pdf", tokens)
        self.assertIn("export", tokens)
        self.assertNotIn("the", tokens)
        self.assertNotIn("a", tokens)
        self.assertNotIn("is", tokens)

    def test_handles_umlauts(self):
        self.assertIn("bericht", tokenize("PDF Bericht Export"))


class TfidfTests(unittest.TestCase):
    def test_vectors_are_unit_length(self):
        vectors = build_tfidf([tokenize("pdf export broken"), tokenize("sso login")])
        for vector in vectors:
            norm = sum(v * v for v in vector.values()) ** 0.5
            self.assertAlmostEqual(norm, 1.0, places=6)

    def test_cosine_identical_is_one(self):
        vectors = build_tfidf([tokenize("pdf export broken") for _ in range(2)])
        self.assertAlmostEqual(cosine(vectors[0], vectors[1]), 1.0, places=6)

    def test_empty_document_vector_is_empty(self):
        vectors = build_tfidf([tokenize("the and of")])
        self.assertEqual(vectors[0], {})


class ClusterTests(unittest.TestCase):
    def test_similar_items_are_grouped(self):
        items = make_items([
            "PDF export is broken",
            "PDF export keeps failing",
            "Cannot export report as PDF",
            "SSO login does not work",
            "SSO login fails constantly",
        ])
        clusters = cluster_items(items, threshold=0.15, min_support=2)
        labels = [c.label.lower() for c in clusters]
        self.assertEqual(len(clusters), 2)
        self.assertTrue(any("pdf" in label for label in labels))
        self.assertTrue(any("sso" in label for label in labels))

    def test_cluster_ids_are_stable(self):
        items = make_items(["PDF export broken", "PDF export fails"])
        first = cluster_items(items, threshold=0.15)
        second = cluster_items(items, threshold=0.15)
        self.assertEqual([c.id for c in first], [c.id for c in second])

    def test_small_cluster_flagged_few_evidence(self):
        items = make_items(["PDF export broken", "PDF export fails", "totally unique"])
        clusters = cluster_items(items, threshold=0.15, min_support=2)
        singles = [c for c in clusters if len(c.item_ids) == 1]
        self.assertTrue(singles)
        self.assertIn("few_evidence", singles[0].uncertainty)

    def test_empty_input(self):
        self.assertEqual(cluster_items([]), [])

    def test_representative_quotes_are_ranked(self):
        items = make_items([
            "PDF export is broken and crashes",
            "PDF export fails",
            "SSO login problem",
        ])
        items_by_id = {i.id: i for i in items}
        vectors = {
            i.id: v
            for i, v in zip(items, build_tfidf([tokenize(i.text) for i in items]))
        }
        quotes = representative_quotes(
            [items[0].id, items[1].id], items_by_id, vectors, limit=2
        )
        self.assertEqual(len(quotes), 2)


if __name__ == "__main__":
    unittest.main()
