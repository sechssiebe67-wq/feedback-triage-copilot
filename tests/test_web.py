import io
import json
import unittest

from feedback_triage.store import TriageStore
from feedback_triage.web import FeedbackTriageApp


CSV = (
    "text,segment,source,url\n"
    "PDF export is broken,ent,support,http://a\n"
    "PDF export keeps failing,smb,support,http://b\n"
    "SSO login does not work,ent,review,http://c\n"
    "SSO login fails,ent,review,http://d\n"
)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.store = TriageStore()
        self.app = FeedbackTriageApp(self.store)

    def call(self, method, path, body=b"", content_type="text/csv", query=""):
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

    def import_csv(self):
        return self.call("POST", "/api/import", CSV.encode(), query="source=support")

    def test_health_before_import(self):
        status, _, body = self.call("GET", "/healthz")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["items"], 0)

    def test_import_and_report(self):
        status, _, body = self.import_csv()
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["added"], 4)
        status, _, body = self.call("GET", "/api/report")
        report = json.loads(body)
        self.assertEqual(report["totals"]["items"], 4)
        self.assertGreaterEqual(report["totals"]["clusters"], 2)

    def test_dashboard_renders(self):
        self.import_csv()
        status, headers, body = self.call("GET", "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Feedback Triage Copilot", body)

    def test_discard_and_history(self):
        self.import_csv()
        _, _, body = self.call("GET", "/api/report")
        cluster_id = json.loads(body)["clusters"][0]["id"]
        status, _, _ = self.call("POST", "/api/clusters/%s/discard" % cluster_id)
        self.assertTrue(status.startswith("200"))
        _, _, body = self.call("GET", "/api/history")
        actions = [e["action"] for e in json.loads(body)["history"]]
        self.assertIn("discard", actions)

    def test_merge_endpoint(self):
        self.import_csv()
        _, _, body = self.call("GET", "/api/report")
        ids = [c["id"] for c in json.loads(body)["clusters"]]
        payload = json.dumps({"cluster_ids": ids}).encode()
        status, _, body = self.call(
            "POST", "/api/merge", payload, content_type="application/json"
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn("cluster_id", json.loads(body))

    def test_merge_requires_two_clusters(self):
        self.import_csv()
        _, _, body = self.call("GET", "/api/report")
        one = [json.loads(body)["clusters"][0]["id"]]
        payload = json.dumps({"cluster_ids": one}).encode()
        status, _, _ = self.call(
            "POST", "/api/merge", payload, content_type="application/json"
        )
        self.assertTrue(status.startswith("400"))

    def test_split_endpoint(self):
        self.import_csv()
        _, _, body = self.call("GET", "/api/report")
        cluster = json.loads(body)["clusters"][0]
        payload = json.dumps({"item_ids": [cluster["quotes"][0]["id"]]}).encode()
        status, _, _ = self.call(
            "POST", "/api/clusters/%s/split" % cluster["id"],
            payload, content_type="application/json",
        )
        self.assertTrue(status.startswith("200"))

    def test_exports(self):
        self.import_csv()
        status, headers, body = self.call("GET", "/report.csv")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"cluster_id", body)
        status, headers, body = self.call("GET", "/report.pdf")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertTrue(body.startswith(b"%PDF"))

    def test_unknown_route(self):
        status, _, _ = self.call("GET", "/does/not/exist")
        self.assertTrue(status.startswith("404"))

    def test_import_with_json_payload(self):
        payload = json.dumps({"csv": "text\nHello world\n", "source": "api"}).encode()
        status, _, body = self.call(
            "POST", "/api/import", payload, content_type="application/json"
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["added"], 1)


if __name__ == "__main__":
    unittest.main()
