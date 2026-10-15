import contextlib
import io
import json
import os
import tempfile
import unittest

from feedback_triage.cli import main
from feedback_triage.models import ACTIVE, CLAIMED, RESOLVED, Claim, Cluster, FeedbackItem
from feedback_triage.report import (
    build_digest,
    build_queue,
    build_report,
    report_to_csv,
    report_to_html,
    report_to_pdf,
)
from feedback_triage.store import TriageStore
from feedback_triage.web import FeedbackTriageApp


def make_store(path=None):
    store = TriageStore(path=path)
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
        Cluster(id="c1", label="PDF / Export", item_ids=["a", "b"],
                uncertainty=["few_evidence"]),
        Cluster(id="c2", label="SSO / Login", item_ids=["c"],
                uncertainty=["few_evidence", "low_cohesion"]),
    ]
    return store


class ClaimStoreTests(unittest.TestCase):
    def test_claim_creates_active_claim_and_event(self):
        store = make_store()
        claim = store.claim_cluster("c1", "Alice", note="mine")
        self.assertEqual(claim.status, CLAIMED)
        self.assertEqual(store.claim_for("c1"), claim)
        self.assertEqual([c.id for c in store.list_claims(CLAIMED)], [claim.id])
        event = store.history[-1]
        self.assertEqual(event.action, "claim")
        self.assertEqual(event.reviewer, "Alice")
        self.assertEqual(event.note, "mine")

    def test_claim_is_idempotent_for_same_reviewer(self):
        store = make_store()
        first = store.claim_cluster("c1", "Alice")
        history_len = len(store.history)
        again = store.claim_cluster("c1", "Alice")
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(store.list_claims(CLAIMED)), 1)
        self.assertEqual(len(store.history), history_len)
        self.assertEqual(
            [e.action for e in store.history].count("claim"), 1
        )

    def test_claim_rejected_for_different_reviewer_without_force(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        history_len = len(store.history)
        with self.assertRaises(ValueError):
            store.claim_cluster("c1", "Bob")
        self.assertEqual(store.claim_for("c1").reviewer, "Alice")
        self.assertEqual(len(store.history), history_len)

    def test_force_reassigns_claim_to_new_reviewer(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        claim = store.claim_cluster("c1", "Bob", force=True)
        self.assertEqual(claim.reviewer, "Bob")
        self.assertEqual(len(store.list_claims(CLAIMED)), 1)
        event = store.history[-1]
        self.assertEqual(event.action, "claim")
        self.assertEqual(event.reviewer, "Bob")
        self.assertTrue(event.details["forced"])

    def test_claim_requires_reviewer(self):
        store = make_store()
        with self.assertRaises(ValueError):
            store.claim_cluster("c1", "   ")

    def test_claim_unknown_cluster_raises(self):
        with self.assertRaises(KeyError):
            make_store().claim_cluster("nope", "Alice")

    def test_release_by_non_claimant_raises(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        with self.assertRaises(ValueError):
            store.release_cluster("c1", "Bob")
        self.assertIsNotNone(store.claim_for("c1"))

    def test_release_by_claimant_clears_queue_entry(self):
        store = make_store()
        store.claim_cluster("c1", "Alice", note="x")
        released = store.release_cluster("c1", "Alice")
        self.assertEqual(released.reviewer, "Alice")
        self.assertIsNone(store.claim_for("c1"))
        self.assertEqual(store.list_claims(CLAIMED), [])
        self.assertEqual(store.history[-1].action, "release")
        self.assertEqual(store.history[-1].reviewer, "Alice")

    def test_release_with_empty_reviewer_clears_queue_entry(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        store.release_cluster("c1", "")
        self.assertIsNone(store.claim_for("c1"))

    def test_release_without_claim_raises(self):
        store = make_store()
        with self.assertRaises(ValueError):
            store.release_cluster("c1", "Alice")

    def test_resolve_marks_claim_resolved_and_removes_from_queue(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        claim = store.resolve_cluster("c1", "Alice", note="duplicate of PDF export")
        self.assertEqual(claim.status, RESOLVED)
        self.assertIsNone(store.claim_for("c1"))
        self.assertEqual(len(store.list_claims(RESOLVED)), 1)
        events = [e for e in store.history if e.action == "resolve"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reviewer, "Alice")
        self.assertEqual(events[0].note, "duplicate of PDF export")

    def test_resolve_unclaimed_cluster_records_resolved_claim(self):
        store = make_store()
        store.resolve_cluster("c1", "Alice", note="handled")
        self.assertEqual(len(store.list_claims(RESOLVED)), 1)

    def test_resolve_requires_reviewer(self):
        store = make_store()
        with self.assertRaises(ValueError):
            store.resolve_cluster("c1", "")


class ClaimPersistenceTests(unittest.TestCase):
    def test_claim_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "workspace.json")
            store = make_store(path)
            store.claim_cluster("c1", "Alice", note="mine")
            store.resolve_cluster("c2", "Bob", note="done")
            loaded = TriageStore.load(path)
            self.assertEqual(len(loaded.claims), 2)
            by_cluster = {c.cluster_id: c for c in loaded.claims}
            self.assertEqual(by_cluster["c1"].status, CLAIMED)
            self.assertEqual(by_cluster["c1"].reviewer, "Alice")
            self.assertEqual(by_cluster["c1"].note, "mine")
            self.assertEqual(by_cluster["c2"].status, RESOLVED)

    def test_legacy_workspace_without_claims_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            legacy = {
                "version": 1,
                "settings": {"similarity_threshold": 0.14, "min_support": 2},
                "items": [{"id": "a", "text": "PDF export is broken"}],
                "clusters": [{"id": "c1", "label": "PDF", "item_ids": ["a"]}],
                "history": [],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            store = TriageStore.load(path)
            self.assertEqual(store.claims, [])
            store.save()
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["claims"], [])


class QueueDigestTests(unittest.TestCase):
    def test_build_digest_partitions_for_two_reviewers(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        alice = build_digest(store, "Alice")
        bob = build_digest(store, "Bob")
        self.assertEqual([c["id"] for c in alice["claimed_by_me"]], ["c1"])
        self.assertEqual([c["id"] for c in alice["claimed_by_others"]], [])
        self.assertEqual([c["id"] for c in alice["unclaimed"]], ["c2"])
        self.assertEqual([c["id"] for c in bob["claimed_by_me"]], [])
        self.assertEqual([c["id"] for c in bob["claimed_by_others"]], ["c1"])
        self.assertEqual([c["id"] for c in bob["unclaimed"]], ["c2"])

    def test_cluster_appears_in_exactly_one_bucket(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        digest = build_digest(store, "Bob")
        ids = [
            c["id"] for c in digest["claimed_by_me"]
            + digest["claimed_by_others"] + digest["unclaimed"]
        ]
        self.assertEqual(len(ids), len(set(ids)))

    def test_queue_entries_carry_claim_metadata(self):
        store = make_store()
        store.claim_cluster("c1", "Alice", note="mine")
        digest = build_digest(store, "Alice")
        entry = digest["claimed_by_me"][0]
        for key in ("id", "label", "size", "uncertainty", "claim_reviewer",
                    "claim_status", "claim_note"):
            self.assertIn(key, entry)
        self.assertEqual(entry["claim_reviewer"], "Alice")
        self.assertEqual(entry["claim_status"], CLAIMED)
        self.assertEqual(entry["claim_note"], "mine")

    def test_resolved_cluster_leaves_the_queue(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        store.resolve_cluster("c1", "Alice")
        digest = build_digest(store, "Alice")
        self.assertEqual(digest["claimed_by_me"], [])
        self.assertEqual([c["id"] for c in digest["unclaimed"]], ["c2"])
        self.assertEqual([c["id"] for c in digest["assigned"]], ["c1"])

    def test_reclaimed_cluster_returns_to_queue(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        store.resolve_cluster("c1", "Alice")
        store.claim_cluster("c1", "Bob")

        self.assertEqual(store.claim_for("c1").reviewer, "Bob")
        report = build_report(store)
        self.assertEqual([c["cluster_id"] for c in report["claims"]], ["c1"])
        self.assertEqual(report["claims"][0]["reviewer"], "Bob")

        bob_queue = build_queue(store, "Bob")
        self.assertEqual([c["id"] for c in bob_queue["claimed_by_me"]], ["c1"])
        self.assertEqual(bob_queue["claimed_by_others"], [])
        self.assertEqual([c["id"] for c in bob_queue["unclaimed"]], ["c2"])

        bob_digest = build_digest(store, "Bob")
        self.assertEqual([c["id"] for c in bob_digest["claimed_by_me"]], ["c1"])
        self.assertEqual([c["id"] for c in bob_digest["unclaimed"]], ["c2"])

        alice_queue = build_queue(store, "Alice")
        self.assertEqual(alice_queue["claimed_by_me"], [])
        self.assertEqual(
            [c["id"] for c in alice_queue["claimed_by_others"]], ["c1"]
        )
        self.assertEqual([c["id"] for c in alice_queue["unclaimed"]], ["c2"])

        ids = [
            c["id"]
            for c in bob_queue["claimed_by_me"]
            + bob_queue["claimed_by_others"] + bob_queue["unclaimed"]
        ]
        self.assertEqual(len(ids), len(set(ids)))

    def test_build_queue_and_digest_agree(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        queue = build_queue(store, "Alice")
        digest = build_digest(store, "Alice")
        for key in ("claimed_by_me", "claimed_by_others", "unclaimed"):
            self.assertEqual(
                [c["id"] for c in queue[key]],
                [c["id"] for c in digest[key]],
            )

    def test_build_queue_without_reviewer(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        queue = build_queue(store, "")
        self.assertEqual(queue["claimed_by_me"], [])
        self.assertEqual([c["id"] for c in queue["claimed_by_others"]], ["c1"])
        self.assertEqual([c["id"] for c in queue["unclaimed"]], ["c2"])


class ReportClaimTests(unittest.TestCase):
    def test_report_exposes_active_claims_only(self):
        store = make_store()
        store.claim_cluster("c1", "Alice", note="mine")
        store.claim_cluster("c2", "Bob")
        store.resolve_cluster("c2", "Bob")
        report = build_report(store)
        self.assertEqual(
            [c["cluster_id"] for c in report["claims"]], ["c1"]
        )
        self.assertEqual(report["claims"][0]["reviewer"], "Alice")

    def test_report_decision_log_includes_resolve(self):
        store = make_store()
        store.claim_cluster("c1", "Alice")
        store.resolve_cluster("c1", "Alice", note="duplicate of PDF export")
        report = build_report(store)
        actions = [d["action"] for d in report["decisions"]]
        self.assertIn("resolve", actions)
        latest = report["decisions"][0]
        self.assertEqual(latest["reviewer"], "Alice")
        self.assertEqual(latest["note"], "duplicate of PDF export")
        self.assertTrue(latest["clusters"])

    def test_csv_has_claim_columns(self):
        store = make_store()
        store.claim_cluster("c1", "Alice", note="mine")
        csv_text = report_to_csv(build_report(store))
        header = csv_text.splitlines()[0]
        for column in ("claim_reviewer", "claim_status", "claim_note"):
            self.assertIn(column, header)
        self.assertIn("Alice", csv_text)
        self.assertIn("mine", csv_text)

    def test_csv_neutralises_claim_formula_injection(self):
        store = make_store()
        store.claim_cluster("c1", "=cmd", note="+1+1")
        csv_text = report_to_csv(build_report(store))
        self.assertIn("'=cmd", csv_text)
        self.assertIn("'+1+1", csv_text)

    def test_html_renders_review_queue_and_escapes(self):
        store = make_store()
        store.claim_cluster(
            "c1", "<b>Eve</b>", note="</script><script>alert(1)</script>"
        )
        html = report_to_html(build_report(store))
        self.assertIn("Review queue", html)
        self.assertNotIn("<b>Eve</b>", html)
        self.assertIn("&lt;b&gt;Eve&lt;/b&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_pdf_renders_review_queue(self):
        store = make_store()
        store.claim_cluster("c1", "Alice", note="mine")
        pdf = report_to_pdf(build_report(store))
        self.assertIn(b"Review queue", pdf)
        self.assertIn(b"Alice", pdf)

    def test_html_without_claims_has_no_queue_section(self):
        html = report_to_html(build_report(make_store()))
        self.assertNotIn("Review queue", html)


class ClaimCliTests(unittest.TestCase):
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
        self.run_cli(["import", self.csv_path, "--source", "support"])
        store = TriageStore.load(self.workspace)
        self.cluster_id = store.active_clusters()[0].id

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--workspace", self.workspace] + argv)
        return code, buffer.getvalue()

    def test_claim_idempotent_and_force(self):
        code, _ = self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        self.assertEqual(code, 0)
        code, _ = self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        self.assertEqual(code, 0)
        saved = json.load(open(self.workspace, encoding="utf-8"))
        self.assertEqual(len(saved["claims"]), 1)
        self.assertEqual(
            [e["action"] for e in saved["history"]].count("claim"), 1
        )

        code, out = self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Bob"]
        )
        self.assertNotEqual(code, 0)
        self.assertIn("Error", out)
        saved = json.load(open(self.workspace, encoding="utf-8"))
        self.assertEqual(saved["claims"][0]["reviewer"], "Alice")

        code, _ = self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Bob", "--force"]
        )
        self.assertEqual(code, 0)
        saved = json.load(open(self.workspace, encoding="utf-8"))
        self.assertEqual(saved["claims"][0]["reviewer"], "Bob")
        self.assertEqual(saved["history"][-1]["action"], "claim")
        self.assertEqual(saved["history"][-1]["reviewer"], "Bob")

    def test_release_and_resolve(self):
        self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        code, out = self.run_cli(
            ["release", "--cluster", self.cluster_id, "--reviewer", "Bob"]
        )
        self.assertNotEqual(code, 0)
        code, _ = self.run_cli(
            ["release", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        self.assertEqual(code, 0)
        saved = json.load(open(self.workspace, encoding="utf-8"))
        self.assertEqual(saved["claims"], [])

        self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        code, _ = self.run_cli([
            "resolve", "--cluster", self.cluster_id, "--reviewer", "Alice",
            "--note", "duplicate of PDF export",
        ])
        self.assertEqual(code, 0)
        saved = json.load(open(self.workspace, encoding="utf-8"))
        self.assertEqual(saved["claims"][0]["status"], RESOLVED)
        resolve_events = [
            e for e in saved["history"] if e["action"] == "resolve"
        ]
        self.assertEqual(len(resolve_events), 1)
        self.assertEqual(resolve_events[0]["reviewer"], "Alice")

    def test_queue_text_and_json(self):
        self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        code, out = self.run_cli(["queue", "--reviewer", "Alice"])
        self.assertEqual(code, 0)
        self.assertIn("Claimed by you", out)
        self.assertIn("Unclaimed", out)
        code, out = self.run_cli(
            ["queue", "--reviewer", "Bob", "--format", "json"]
        )
        self.assertEqual(code, 0)
        queue = json.loads(out)
        for key in ("claimed_by_me", "claimed_by_others", "unclaimed"):
            self.assertIn(key, queue)

    def test_digest_text_lists_claim_counts(self):
        self.run_cli(
            ["claim", "--cluster", self.cluster_id, "--reviewer", "Alice"]
        )
        code, out = self.run_cli(["digest", "--reviewer", "Alice"])
        self.assertEqual(code, 0)
        self.assertIn("Claimed by you", out)
        self.assertIn("Unclaimed backlog", out)


class ClaimWebTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
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

        data = b"".join(self.app(environ, start_response))
        return captured["status"], data

    def test_api_queue_requires_reviewer(self):
        status, body = self.call("GET", "/api/queue")
        self.assertTrue(status.startswith("400"))
        self.assertIn("error", json.loads(body))

    def test_api_queue_returns_buckets(self):
        self.store.claim_cluster("c1", "Alice")
        status, body = self.call("GET", "/api/queue", query="reviewer=Alice")
        self.assertTrue(status.startswith("200"))
        queue = json.loads(body)
        self.assertEqual([c["id"] for c in queue["claimed_by_me"]], ["c1"])
        self.assertEqual([c["id"] for c in queue["unclaimed"]], ["c2"])

    def test_claim_release_resolve_endpoints(self):
        payload = json.dumps({"reviewer": "Alice"}).encode()
        status, _ = self.call("POST", "/api/clusters/c1/claim", payload)
        self.assertTrue(status.startswith("200"))
        self.assertEqual(self.store.claim_for("c1").reviewer, "Alice")

        status, _ = self.call(
            "POST", "/api/clusters/c1/claim", json.dumps({"reviewer": "Bob"}).encode()
        )
        self.assertTrue(status.startswith("400"))
        status, _ = self.call(
            "POST", "/api/clusters/c1/release",
            json.dumps({"reviewer": "Bob"}).encode(),
        )
        self.assertTrue(status.startswith("400"))
        status, _ = self.call(
            "POST", "/api/clusters/c1/release",
            json.dumps({"reviewer": "Alice"}).encode(),
        )
        self.assertTrue(status.startswith("200"))
        self.assertIsNone(self.store.claim_for("c1"))

        self.store.claim_cluster("c1", "Alice")
        status, _ = self.call(
            "POST", "/api/clusters/c1/resolve",
            json.dumps({"reviewer": "Alice", "note": "done"}).encode(),
        )
        self.assertTrue(status.startswith("200"))
        self.assertIsNone(self.store.claim_for("c1"))

    def test_dashboard_renders_review_queue_and_controls(self):
        self.store.claim_cluster("c1", "Alice")
        status, body = self.call("GET", "/", query="reviewer=Alice")
        self.assertTrue(status.startswith("200"))
        html = body.decode("utf-8")
        self.assertIn("Review queue", html)
        self.assertIn("resolveCluster", html)
        self.assertIn("releaseCluster", html)
        self.assertIn("claimCluster", html)

    def test_dashboard_without_reviewer_does_not_break(self):
        status, body = self.call("GET", "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"Review queue", body)


if __name__ == "__main__":
    unittest.main()
