import contextlib
import io
import json
import os
import tempfile
import unittest

from feedback_triage.cli import main
from feedback_triage.digest import (
    build_digest,
    digest_to_html,
    digest_to_json,
    digest_to_text,
    suggest_assignments,
)
from feedback_triage.models import ACTIVE, Cluster, FeedbackItem
from feedback_triage.store import TriageStore
from feedback_triage.web import FeedbackTriageApp


def cluster_store(reviewers=(), clusters=None):
    """A store with manually controlled clusters and reviewers."""
    store = TriageStore()
    store.workspace.items = [
        FeedbackItem(id="i%d" % n, text="feedback %d" % n) for n in range(20)
    ]
    if clusters is None:
        clusters = [
            Cluster(id="c1", label="Alpha", item_ids=["i0", "i1"]),
            Cluster(id="c2", label="Beta", item_ids=["i2"]),
            Cluster(id="c3", label="Gamma", item_ids=["i3", "i4"]),
            Cluster(id="c4", label="Delta", item_ids=["i5"]),
        ]
    store.workspace.clusters = list(clusters)
    for name in reviewers:
        store.add_reviewer(name)
    return store


class BuildDigestTests(unittest.TestCase):
    def test_empty_workspace_has_all_keys(self):
        digest = build_digest(TriageStore())
        for key in (
            "generated_at", "reviewer", "reviewers", "assignments",
            "unassigned_clusters", "recent_decisions", "totals",
        ):
            self.assertIn(key, digest)
        self.assertEqual(digest["assignments"], [])
        self.assertEqual(digest["unassigned_clusters"], [])
        self.assertEqual(digest["recent_decisions"], [])
        self.assertEqual(digest["reviewers"], [])
        self.assertEqual(digest["totals"]["reviewers"], 0)
        self.assertEqual(digest["totals"]["active_clusters"], 0)

    def test_single_reviewer_gets_every_unassigned_cluster(self):
        store = cluster_store(reviewers=["Alice"])
        digest = build_digest(store)
        self.assertEqual(digest["assignments"], [])
        self.assertEqual(len(digest["unassigned_clusters"]), 4)
        suggested = digest["suggested_assignments"]
        self.assertEqual(len(suggested), 4)
        self.assertTrue(all(s["reviewer_name"] == "Alice" for s in suggested))
        self.assertEqual(digest["totals"]["unassigned_clusters"], 4)

    def test_multiple_reviewers_are_balanced(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        suggested = suggest_assignments(store)
        counts = {}
        for entry in suggested:
            counts[entry["reviewer_name"]] = counts.get(entry["reviewer_name"], 0) + 1
        self.assertEqual(sorted(counts.values()), [2, 2])

    def test_assignment_is_inferred_from_decision_log(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        alice = store.list_reviewers()[0]
        store.rename("c1", "PDF problems", reviewer=alice.id, note="own it")
        digest = build_digest(store)
        self.assertEqual(len(digest["assignments"]), 1)
        entry = digest["assignments"][0]
        self.assertEqual(entry["cluster_id"], "c1")
        self.assertEqual(entry["cluster_label"], "PDF problems")
        self.assertEqual(entry["reviewer_name"], "Alice")
        self.assertEqual(entry["open_items"], 2)
        self.assertEqual(digest["totals"]["assigned_clusters"], 1)
        self.assertEqual(digest["totals"]["unassigned_clusters"], 3)

    def test_filter_by_reviewer_id_and_name_match(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        alice = store.list_reviewers()[0]
        store.rename("c1", "PDF problems", reviewer=alice.id, note="mine")
        by_id = build_digest(store, reviewer=alice.id)
        by_name = build_digest(store, reviewer="Alice")
        self.assertEqual(by_id["assignments"], by_name["assignments"])
        self.assertEqual(len(by_id["assignments"]), 1)
        self.assertEqual(len(by_id["recent_decisions"]), 1)
        self.assertEqual(by_id["unassigned_clusters"], [])
        self.assertEqual(by_id["suggested_assignments"], [])
        self.assertEqual(by_id["totals"]["assigned_clusters"], 1)
        self.assertEqual(by_id["totals"]["reviewers"], 1)

    def test_filter_is_case_insensitive(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        alice = store.list_reviewers()[0]
        store.rename("c1", "PDF problems", reviewer=alice.id)
        self.assertEqual(
            build_digest(store, reviewer="alice")["assignments"],
            build_digest(store, reviewer="ALICE")["assignments"],
        )

    def test_unknown_reviewer_returns_empty_digest(self):
        store = cluster_store(reviewers=["Alice"])
        digest = build_digest(store, reviewer="Nobody")
        self.assertEqual(digest["assignments"], [])
        self.assertEqual(digest["recent_decisions"], [])
        self.assertEqual(digest["reviewers"], [])
        self.assertEqual(digest["totals"]["active_clusters"], 0)

    def test_recent_decisions_are_newest_first(self):
        store = cluster_store(reviewers=["Alice"])
        alice = store.list_reviewers()[0]
        store.rename("c1", "First", reviewer=alice.id, note="one")
        store.rename("c2", "Second", reviewer=alice.id, note="two")
        digest = build_digest(store)
        self.assertEqual(
            [d["note"] for d in digest["recent_decisions"]], ["two", "one"]
        )


class SuggestAssignmentsTests(unittest.TestCase):
    def test_deterministic_across_calls(self):
        store = cluster_store(reviewers=["Alice", "Bob", "Cara"])
        first = suggest_assignments(store)
        second = suggest_assignments(store)
        self.assertEqual(first, second)

    def test_fewest_open_clusters_chosen_first(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        suggested = suggest_assignments(store)
        per_reviewer = {}
        for entry in suggested:
            per_reviewer[entry["reviewer_name"]] = (
                per_reviewer.get(entry["reviewer_name"], 0) + 1
            )
        self.assertLessEqual(
            abs(per_reviewer["Alice"] - per_reviewer["Bob"]), 1
        )

    def test_tie_broken_by_name_then_id(self):
        store = cluster_store(
            reviewers=["Zoe", "Alice"],
            clusters=[Cluster(id="c1", label="Only", item_ids=["i0"])],
        )
        suggested = suggest_assignments(store)
        self.assertEqual(suggested[0]["reviewer_name"], "Alice")

    def test_tie_broken_by_name_is_case_insensitive(self):
        store = cluster_store(
            reviewers=["bob", "Alice"],
            clusters=[Cluster(id="c1", label="Only", item_ids=["i0"])],
        )
        suggested = suggest_assignments(store)
        self.assertEqual(suggested[0]["reviewer_name"], "Alice")

    def test_existing_load_is_respected(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        alice = store.list_reviewers()[0]
        for cluster_id in ("c1", "c2", "c3"):
            store.rename(cluster_id, "Owned " + cluster_id, reviewer=alice.id)
        suggested = suggest_assignments(store)
        self.assertEqual(len(suggested), 1)
        self.assertEqual(suggested[0]["reviewer_name"], "Bob")

    def test_no_reviewers_returns_empty(self):
        store = cluster_store()
        self.assertEqual(suggest_assignments(store), [])

    def test_does_not_mutate_workspace(self):
        store = cluster_store(reviewers=["Alice", "Bob"])
        before = store.workspace.to_dict()
        suggest_assignments(store)
        self.assertEqual(store.workspace.to_dict(), before)

    def test_entries_expose_expected_keys(self):
        store = cluster_store(reviewers=["Alice"])
        entry = suggest_assignments(store)[0]
        self.assertEqual(
            set(entry),
            {"cluster_id", "cluster_label", "reviewer_id", "reviewer_name",
             "open_items"},
        )

    def test_accepts_explicit_cluster_subset(self):
        store = cluster_store(reviewers=["Alice"])
        subset = [Cluster(id="x", label="Only this", item_ids=["i9", "i10", "i11"])]
        suggested = suggest_assignments(store, clusters=subset)
        self.assertEqual(len(suggested), 1)
        self.assertEqual(suggested[0]["cluster_id"], "x")
        self.assertEqual(suggested[0]["open_items"], 3)


class RenderTests(unittest.TestCase):
    def malicious_store(self):
        store = cluster_store(reviewers=["<script>alert(1)</script>"])
        reviewer = store.list_reviewers()[0]
        store.rename(
            "c1",
            'Broken "export" & report',
            reviewer=reviewer.id,
            note='</script><script>alert(2)</script> & "quoted"',
        )
        return store

    def test_html_escapes_reviewer_names_and_notes(self):
        digest = build_digest(self.malicious_store())
        html = digest_to_html(digest)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<script>alert(2)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;", html)
        self.assertIn("&amp;", html)
        self.assertIn("&quot;", html)

    def test_html_escapes_cluster_labels(self):
        digest = build_digest(self.malicious_store())
        html = digest_to_html(digest)
        self.assertNotIn('Broken "export" & report', html)
        self.assertIn("Broken &quot;export&quot; &amp; report", html)

    def test_text_neutralises_formula_injection(self):
        store = cluster_store(reviewers=["=cmd"])
        reviewer = store.list_reviewers()[0]
        store.rename("c1", "+dangerous", reviewer=reviewer.id, note="@note")
        text = digest_to_text(build_digest(store))
        self.assertIn("'=cmd", text)
        self.assertIn("'+dangerous", text)
        self.assertIn("'@note", text)

    def test_text_flattens_newlines(self):
        store = cluster_store(reviewers=["Alice"])
        alice = store.list_reviewers()[0]
        store.rename("c1", "line one", reviewer=alice.id, note="a\nb\r\nc")
        text = digest_to_text(build_digest(store))
        self.assertNotIn("a\nb", text)

    def test_json_round_trips(self):
        digest = build_digest(self.malicious_store())
        encoded = digest_to_json(digest)
        self.assertEqual(json.loads(encoded), digest)
        self.assertIn("<script>alert(1)</script>", encoded)

    def test_renderers_work_on_empty_digest(self):
        digest = build_digest(TriageStore())
        self.assertIn("Per-reviewer", digest_to_text(digest))
        self.assertIn("<html", digest_to_html(digest))
        self.assertIn("reviewers", digest_to_json(digest))


class CliDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = os.path.join(self.tmp.name, "workspace.json")

    def run_cli(self, argv):
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(buffer, encoding="utf-8")
        with contextlib.redirect_stdout(wrapper):
            code = main(["--workspace", self.workspace] + argv)
        wrapper.flush()
        return code, buffer.getvalue().decode("utf-8")

    def test_empty_workspace_json_exits_zero(self):
        out_path = os.path.join(self.tmp.name, "digest.json")
        code, _ = self.run_cli(["digest", "--format", "json", "--out", out_path])
        self.assertEqual(code, 0)
        with open(out_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for key in (
            "reviewers", "assignments", "unassigned_clusters",
            "recent_decisions", "totals",
        ):
            self.assertIn(key, payload)

    def test_digest_stdout_contains_reviewer(self):
        self.run_cli(["reviewer", "add", "Alice"])
        code, out = self.run_cli(["digest", "--format", "text"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)

    def test_filter_by_reviewer_with_no_activity_exits_zero(self):
        code, out = self.run_cli(
            ["digest", "--reviewer", "Alice", "--format", "text"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)

    def test_digest_writes_html_to_out(self):
        out_path = os.path.join(self.tmp.name, "digest.html")
        code, _ = self.run_cli(["digest", "--format", "html", "--out", out_path])
        self.assertEqual(code, 0)
        with open(out_path, encoding="utf-8") as handle:
            self.assertIn("<html", handle.read())


class WebDigestTests(unittest.TestCase):
    def setUp(self):
        self.store = cluster_store(reviewers=["Alice", "Bob"])
        self.app = FeedbackTriageApp(self.store)

    def call(self, method, path, query=""):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": "",
            "CONTENT_LENGTH": "0",
            "wsgi.input": io.BytesIO(b""),
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        data = b"".join(self.app(environ, start_response))
        return captured["status"], captured["headers"], data

    def test_api_digest_returns_json_keys(self):
        status, headers, body = self.call("GET", "/api/digest")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(
            headers["Content-Type"], "application/json; charset=utf-8"
        )
        payload = json.loads(body)
        for key in (
            "reviewers", "assignments", "unassigned_clusters",
            "recent_decisions", "totals",
        ):
            self.assertIn(key, payload)

    def test_api_digest_filters_by_reviewer(self):
        alice = self.store.list_reviewers()[0]
        self.store.rename("c1", "PDF", reviewer=alice.id, note="mine")
        status, _, body = self.call("GET", "/api/digest", query="reviewer=" + alice.id)
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(len(payload["assignments"]), 1)
        self.assertEqual(payload["assignments"][0]["reviewer_name"], "Alice")

    def test_api_digest_unknown_reviewer_is_empty(self):
        status, _, body = self.call("GET", "/api/digest", query="reviewer=nobody")
        self.assertTrue(status.startswith("200"))
        payload = json.loads(body)
        self.assertEqual(payload["assignments"], [])
        self.assertEqual(payload["reviewers"], [])

    def test_digest_html_content_type_and_escaping(self):
        bad = self.store.add_reviewer("<script>alert(1)</script>")
        self.store.rename("c1", "label", reviewer=bad.id, note="<b>note</b>")
        status, headers, body = self.call("GET", "/digest.html")
        self.assertTrue(status.startswith("200"))
        self.assertIn("text/html", headers["Content-Type"])
        self.assertNotIn(b"<script>alert(1)</script>", body)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertNotIn(b"<b>note</b>", body)

    def test_digest_txt_content_type_and_disposition(self):
        status, headers, body = self.call("GET", "/digest.txt")
        self.assertTrue(status.startswith("200"))
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"Per-reviewer", body)

    def test_dashboard_has_digest_panel(self):
        status, _, body = self.call("GET", "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"renderDigest", body)
        self.assertIn(b"Reviewer digest", body)
        self.assertIn(b"/digest.html", body)
        self.assertIn(b"/digest.txt", body)
        self.assertIn(b"/api/digest", body)


if __name__ == "__main__":
    unittest.main()
