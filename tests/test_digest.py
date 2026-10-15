import contextlib
import io
import json
import os
import tempfile
import unittest

from feedback_triage.cli import main
from feedback_triage.models import Cluster, FeedbackItem
from feedback_triage.report import build_digest
from feedback_triage.store import TriageStore
from feedback_triage.web import FeedbackTriageApp


def make_store():
    store = TriageStore()
    store.add_items(
        [
            FeedbackItem(id="a", text="PDF export is broken", segment="ent",
                         source="support"),
            FeedbackItem(id="b", text="PDF export keeps failing", segment="smb",
                         source="support"),
            FeedbackItem(id="c", text="SSO login does not work", segment="ent",
                         source="review"),
        ],
        recluster=False,
    )
    store.workspace.clusters = [
        Cluster(id="c1", label="PDF / Export", item_ids=["a", "b"]),
        Cluster(id="c2", label="SSO / Login", item_ids=["c"],
                uncertainty=["few_evidence"]),
    ]
    return store


class DigestReportTests(unittest.TestCase):
    def test_reviewer_with_prior_decisions(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        store.rename("c1", "PDF export", reviewer=alice.id, note="clear label")
        digest = build_digest(store, "Alice")
        self.assertEqual(digest["reviewer"], "Alice")
        self.assertEqual([c["label"] for c in digest["assigned"]], ["PDF export"])
        self.assertEqual(digest["assigned"][0]["size"], 2)
        self.assertEqual(digest["recent_decisions"][0]["action"], "rename")
        self.assertEqual(digest["recent_decisions"][0]["note"], "clear label")
        self.assertEqual(digest["recent_decisions"][0]["clusters"], ["PDF export"])

    def test_reviewer_with_no_decisions(self):
        store = make_store()
        store.add_reviewer("Alice")
        digest = build_digest(store, "Alice")
        self.assertEqual(digest["assigned"], [])
        self.assertEqual(digest["recent_decisions"], [])
        self.assertEqual([c["label"] for c in digest["needs_review"]],
                         ["SSO / Login"])

    def test_unknown_reviewer_fallback(self):
        store = make_store()
        store.add_reviewer("Alice")
        digest = build_digest(store, "Nobody")
        self.assertEqual(digest["reviewer"], "Nobody")
        self.assertEqual(digest["assigned"], [])
        self.assertEqual([c["label"] for c in digest["needs_review"]],
                         ["SSO / Login"])

    def test_reviewer_resolved_from_id(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        digest = build_digest(store, alice.id)
        self.assertEqual(digest["reviewer"], "Alice")

    def test_open_uncertainty_counts(self):
        store = make_store()
        store.workspace.clusters.extend([
            Cluster(id="c3", label="Billing", item_ids=["x", "y", "z"],
                    uncertainty=["few_evidence", "low_cohesion"]),
            Cluster(id="c4", label="Mobile", item_ids=["q", "r"],
                    uncertainty=["low_separation"]),
        ])
        digest = build_digest(store, "Alice")
        self.assertEqual(
            digest["open_uncertainties"],
            {"few_evidence": 2, "low_cohesion": 1, "low_separation": 1},
        )
        self.assertEqual(digest["totals"]["uncertain_clusters"], 3)

    def test_needs_review_excludes_decided_clusters(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        store.rename("c2", "SSO login", reviewer=alice.id, note="ok")
        digest = build_digest(store, "Alice")
        self.assertEqual([c["label"] for c in digest["needs_review"]], [])
        self.assertEqual([c["label"] for c in digest["assigned"]], ["SSO login"])

    def test_needs_review_sorted_by_size_then_flags(self):
        store = make_store()
        store.workspace.clusters = [
            Cluster(id="small", label="Small", item_ids=["a"],
                    uncertainty=["few_evidence"]),
            Cluster(id="big", label="Big", item_ids=["b", "c", "d"],
                    uncertainty=["few_evidence"]),
            Cluster(id="big2", label="Big2", item_ids=["e", "f", "g"],
                    uncertainty=["few_evidence", "low_cohesion"]),
        ]
        digest = build_digest(store, "Alice")
        self.assertEqual(
            [c["label"] for c in digest["needs_review"]],
            ["Big2", "Big", "Small"],
        )

    def test_assigned_uses_most_recent_decision(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        store.rename("c1", "Alice rename", reviewer=alice.id)
        store.rename("c1", "Bob rename", reviewer="Bob")
        alice_digest = build_digest(store, "Alice")
        bob_digest = build_digest(store, "Bob")
        self.assertEqual(alice_digest["assigned"], [])
        self.assertTrue(alice_digest["recent_decisions"])
        self.assertEqual([c["label"] for c in bob_digest["assigned"]],
                         ["Bob rename"])

    def test_limit_caps_recent_decisions(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        store.rename("c1", "One", reviewer=alice.id)
        store.rename("c1", "Two", reviewer=alice.id)
        store.rename("c1", "Three", reviewer=alice.id)
        digest = build_digest(store, "Alice", limit=2)
        self.assertEqual(len(digest["recent_decisions"]), 2)
        self.assertEqual(digest["recent_decisions"][0]["action"], "rename")

    def test_digest_does_not_modify_workspace(self):
        store = make_store()
        alice = store.add_reviewer("Alice")
        store.discard("c2", reviewer=alice.id, note="x")
        before_history = [e.to_dict() for e in store.history]
        before_clusters = [c.to_dict() for c in store.clusters]
        build_digest(store, "Alice")
        self.assertEqual([e.to_dict() for e in store.history], before_history)
        self.assertEqual([c.to_dict() for c in store.clusters], before_clusters)


class DigestCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = os.path.join(self.tmp.name, "workspace.json")
        self.csv_path = os.path.join(self.tmp.name, "in.csv")
        with open(self.csv_path, "w", encoding="utf-8") as handle:
            handle.write(
                "text,segment,source\n"
                "PDF export broken,ent,support\n"
                "PDF export fails,smb,support\n"
                "SSO login fails,ent,review\n"
            )

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--workspace", self.workspace] + argv)
        return code, buffer.getvalue()

    def prepare(self):
        self.run_cli(["reviewer", "add", "Alice"])
        self.run_cli(["import", self.csv_path, "--source", "support"])

    def test_digest_text_summary(self):
        self.prepare()
        code, out = self.run_cli(["digest", "--reviewer", "Alice"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)
        self.assertIn("needing review", out)
        self.assertIn("Open uncertainties:", out)
        self.assertIn("few_evidence=", out)

    def test_digest_json_path(self):
        self.prepare()
        code, out = self.run_cli(["digest", "--reviewer", "Alice", "--format", "json"])
        self.assertEqual(code, 0)
        digest = json.loads(out)
        for key in ("reviewer", "generated_at", "assigned", "needs_review",
                    "open_uncertainties", "recent_decisions", "totals"):
            self.assertIn(key, digest)
        self.assertEqual(digest["reviewer"], "Alice")

    def test_digest_missing_reviewer_exits_non_zero(self):
        self.prepare()
        code, out = self.run_cli(["digest"])
        self.assertNotEqual(code, 0)
        self.assertIn("reviewer", out.lower())

    def test_digest_unknown_reviewer_prints_message(self):
        self.prepare()
        code, out = self.run_cli(["digest", "--reviewer", "Nobody"])
        self.assertEqual(code, 0)
        self.assertIn("Nobody", out)

    def test_digest_is_read_only(self):
        self.prepare()
        with open(self.workspace, "rb") as handle:
            before = handle.read()
        self.run_cli(["digest", "--reviewer", "Alice"])
        self.run_cli(["digest", "--reviewer", "Alice", "--format", "json"])
        with open(self.workspace, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


class DigestWebTests(unittest.TestCase):
    def setUp(self):
        self.store = TriageStore()
        self.app = FeedbackTriageApp(self.store)

    def call(self, method, path, body=b"", content_type="application/json",
             query=""):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        data = b"".join(self.app(environ, start_response))
        return captured["status"], captured["headers"], data

    def add_reviewer(self, name):
        payload = json.dumps({"name": name}).encode()
        status, _, body = self.call("POST", "/api/reviewers", payload)
        self.assertTrue(status.startswith("200"))
        return json.loads(body)["reviewer"]

    def import_csv(self):
        csv = (
            "text,segment,source\n"
            "PDF export broken,ent,support\n"
            "PDF export fails,smb,support\n"
            "SSO login fails,ent,review\n"
        )
        status, _, _ = self.call(
            "POST", "/api/import", csv.encode(), content_type="text/csv",
            query="source=support",
        )
        self.assertTrue(status.startswith("200"))

    def test_api_digest_returns_shape(self):
        self.import_csv()
        self.add_reviewer("Alice")
        status, headers, body = self.call(
            "GET", "/api/digest", query="reviewer=Alice"
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        digest = json.loads(body)
        for key in ("reviewer", "generated_at", "assigned", "needs_review",
                    "open_uncertainties", "recent_decisions", "totals"):
            self.assertIn(key, digest)
        self.assertEqual(digest["reviewer"], "Alice")

    def test_api_digest_missing_reviewer_is_400(self):
        status, _, body = self.call("GET", "/api/digest")
        self.assertTrue(status.startswith("400"))
        self.assertIn("error", json.loads(body))

    def test_api_digest_unknown_reviewer_echoes_raw(self):
        self.import_csv()
        status, _, body = self.call(
            "GET", "/api/digest", query="reviewer=Nobody"
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["reviewer"], "Nobody")

    def test_dashboard_digest_escapes_untrusted_values(self):
        self.import_csv()
        reviewer = self.add_reviewer("<b>Eve</b>")
        _, _, body = self.call("GET", "/api/report")
        cluster_id = json.loads(body)["clusters"][0]["id"]
        note = json.dumps({
            "reviewer": reviewer["id"],
            "note": "</script><script>alert(1)</script>",
        }).encode()
        self.call(
            "POST", "/api/clusters/%s/discard" % cluster_id, note
        )
        status, _, body = self.call(
            "GET", "/", query="reviewer=%3Cb%3EEve%3C%2Fb%3E"
        )
        self.assertTrue(status.startswith("200"))
        html = body.decode("utf-8")
        self.assertIn("My digest", html)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)
        self.assertNotIn("<b>Eve</b>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()
