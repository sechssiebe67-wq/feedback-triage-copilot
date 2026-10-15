import contextlib
import io
import json
import os
import tempfile
import unittest

from feedback_triage.cli import main
from feedback_triage.store import TriageStore
from feedback_triage.web import FeedbackTriageApp


CSV = (
    "id,text,segment,source\n"
    "A1,PDF export broken,ent,support\n"
    "A2,PDF export fails,smb,support\n"
    "A3,SSO login fails,ent,review\n"
)


def write(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


class SourceCrudTests(unittest.TestCase):
    def test_add_list_get_and_remove(self):
        store = TriageStore()
        source = store.add_source("support", "/tmp/in.csv", source_label="support")
        self.assertTrue(source.id)
        self.assertTrue(source.enabled)
        self.assertEqual(source.source_label, "support")
        self.assertEqual(store.list_sources(), [source])
        self.assertEqual(store.get_source(source.id), source)
        store.remove_source(source.id)
        self.assertEqual(store.list_sources(), [])

    def test_add_source_requires_name_and_path(self):
        store = TriageStore()
        with self.assertRaises(ValueError):
            store.add_source("", "/tmp/in.csv")
        with self.assertRaises(ValueError):
            store.add_source("support", "  ")

    def test_set_source_enabled(self):
        store = TriageStore()
        source = store.add_source("support", "/tmp/in.csv")
        store.set_source_enabled(source.id, False)
        self.assertFalse(store.get_source(source.id).enabled)
        store.set_source_enabled(source.id, True)
        self.assertTrue(store.get_source(source.id).enabled)

    def test_unknown_source_raises(self):
        with self.assertRaises(KeyError):
            TriageStore().get_source("nope")


class RunSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_run_is_idempotent(self):
        path = write(os.path.join(self.tmp.name, "in.csv"), CSV)
        store = TriageStore()
        source = store.add_source("support", path, source_label="support")
        first = store.run_source(source.id)
        self.assertEqual(first.added, 3)
        self.assertEqual(first.skipped, 0)
        self.assertEqual(len(store.items), 3)
        second = store.run_source(source.id)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.skipped, 3)
        self.assertEqual(len(store.items), 3)
        self.assertEqual(len(store.ingest_runs), 2)
        self.assertEqual(store.get_source(source.id).last_added, 0)

    def test_run_records_ingest_event(self):
        path = write(os.path.join(self.tmp.name, "in.csv"), CSV)
        store = TriageStore()
        source = store.add_source("support", path)
        store.run_source(source.id, reviewer="Alice", note="weekly")
        ingest_events = [e for e in store.history if e.action == "ingest"]
        self.assertEqual(len(ingest_events), 1)
        self.assertEqual(ingest_events[0].details["added"], 3)
        self.assertEqual(ingest_events[0].reviewer, "Alice")
        self.assertEqual(ingest_events[0].note, "weekly")

    def test_directory_glob_resolution(self):
        nested = os.path.join(self.tmp.name, "nested")
        os.makedirs(nested)
        write(os.path.join(self.tmp.name, "one.csv"), CSV)
        write(os.path.join(nested, "two.csv"), CSV)
        write(os.path.join(self.tmp.name, "ignore.txt"), "not a csv")
        store = TriageStore()
        source = store.add_source("support", self.tmp.name, glob="*.csv")
        run = store.run_source(source.id)
        self.assertEqual(sorted(run.files), ["one.csv", "two.csv"])
        self.assertEqual(run.added, 3)
        self.assertEqual(run.skipped, 3)

    def test_default_segment_applied(self):
        path = write(
            os.path.join(self.tmp.name, "in.csv"),
            "id,text\nA1,PDF export broken\n",
        )
        store = TriageStore()
        source = store.add_source("support", path, segment_default="smb")
        store.run_source(source.id)
        self.assertEqual(store.items[0].segment, "smb")

    def test_bad_file_does_not_abort_run(self):
        directory = self.tmp.name
        write(os.path.join(directory, "good.csv"), CSV)
        write(os.path.join(directory, "bad.csv"), "segment,rating\nsmb,3\n")
        store = TriageStore()
        source = store.add_source("support", directory)
        run = store.run_source(source.id)
        self.assertEqual(run.added, 3)
        self.assertEqual(len(run.errors), 1)
        self.assertEqual(run.errors[0]["file"], "bad.csv")
        self.assertEqual(len(store.items), 3)

    def test_missing_path_raises(self):
        store = TriageStore()
        source = store.add_source("support", os.path.join(self.tmp.name, "gone.csv"))
        with self.assertRaises(ValueError):
            store.run_source(source.id)

    def test_run_persists_and_reloads(self):
        path = os.path.join(self.tmp.name, "workspace.json")
        csv_path = write(os.path.join(self.tmp.name, "in.csv"), CSV)
        store = TriageStore(path=path)
        source = store.add_source("support", csv_path)
        store.run_source(source.id, reviewer="Alice", note="weekly")
        loaded = TriageStore.load(path)
        self.assertEqual(len(loaded.ingest_runs), 1)
        run = loaded.ingest_runs[0]
        self.assertEqual(run.added, 3)
        self.assertEqual(run.reviewer, "Alice")
        self.assertEqual(run.note, "weekly")
        self.assertEqual(len(loaded.items), 3)
        self.assertEqual(loaded.get_source(source.id).last_added, 3)
        self.assertEqual(loaded.history[-1].action, "ingest")

    def test_run_all_skips_disabled_and_isolates_failures(self):
        csv_path = write(os.path.join(self.tmp.name, "in.csv"), CSV)
        store = TriageStore()
        good = store.add_source("good", csv_path, source_label="good")
        disabled = store.add_source("disabled", csv_path, source_label="disabled")
        store.set_source_enabled(disabled.id, False)
        missing = store.add_source(
            "missing", os.path.join(self.tmp.name, "gone.csv")
        )
        runs = store.run_all_sources()
        names = [r.source_name for r in runs]
        self.assertEqual(names, ["good", "missing"])
        self.assertEqual(runs[0].added, 3)
        self.assertEqual(len(runs[1].errors), 1)
        self.assertNotIn("disabled", names)
        self.assertIsNotNone(good)
        self.assertEqual(len(store.ingest_runs), 2)


class BackwardCompatTests(unittest.TestCase):
    def test_legacy_workspace_loads_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            legacy = {
                "version": 1,
                "settings": {"similarity_threshold": 0.14, "min_support": 2},
                "items": [{"id": "a", "text": "PDF export broken"}],
                "clusters": [{"id": "c1", "label": "PDF", "item_ids": ["a"]}],
                "history": [],
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(legacy, handle)
            store = TriageStore.load(path)
            self.assertEqual(store.list_sources(), [])
            self.assertEqual(store.ingest_runs, [])
            # Still importable and exportable after loading.
            store.add_items([])
            store.save()
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertIn("sources", saved)
            self.assertIn("ingest_runs", saved)


class CliSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = os.path.join(self.tmp.name, "workspace.json")
        self.csv_path = write(os.path.join(self.tmp.name, "in.csv"), CSV)

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--workspace", self.workspace] + argv)
        return code, buffer.getvalue()

    def test_source_add_list_enable_disable_remove(self):
        code, out = self.run_cli(
            ["source", "add", "support", self.csv_path, "--label", "support"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Added source support", out)
        store = TriageStore.load(self.workspace)
        source_id = store.list_sources()[0].id

        code, out = self.run_cli(["source", "list"])
        self.assertEqual(code, 0)
        self.assertIn("support", out)
        self.assertIn("enabled", out)

        code, out = self.run_cli(["source", "disable", source_id])
        self.assertEqual(code, 0)
        self.assertIn("Disabled", out)
        self.assertFalse(TriageStore.load(self.workspace).get_source(source_id).enabled)

        self.run_cli(["source", "enable", source_id])
        self.assertTrue(TriageStore.load(self.workspace).get_source(source_id).enabled)

        code, out = self.run_cli(["source", "remove", source_id])
        self.assertEqual(code, 0)
        self.assertIn("Removed", out)
        self.assertEqual(TriageStore.load(self.workspace).list_sources(), [])

    def test_ingest_and_ingest_log(self):
        self.run_cli(["source", "add", "support", self.csv_path, "--label", "support"])
        code, out = self.run_cli(["ingest"])
        self.assertEqual(code, 0)
        self.assertIn("added=3", out.replace(" ", ""))
        self.assertIn("Total:", out)
        code, out = self.run_cli(["ingest"])
        self.assertEqual(code, 0)
        self.assertIn("added=0", out.replace(" ", ""))
        code, out = self.run_cli(["ingest-log"])
        self.assertEqual(code, 0)
        self.assertIn("support", out)

    def test_ingest_single_source(self):
        self.run_cli(["source", "add", "support", self.csv_path])
        source_id = TriageStore.load(self.workspace).list_sources()[0].id
        code, out = self.run_cli(["ingest", "--source", source_id, "--reviewer", "Alice"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)


class WebSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv_path = write(os.path.join(self.tmp.name, "in.csv"), CSV)
        self.store = TriageStore()
        self.app = FeedbackTriageApp(self.store)

    def call(self, method, path, body=b"", content_type="application/json", query=""):
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

    def create_source(self, name="support", path=None, **extra):
        payload = dict(name=name, path=path or self.csv_path)
        payload.update(extra)
        status, _, body = self.call("POST", "/api/sources", json.dumps(payload).encode())
        self.assertTrue(status.startswith("200"), body)
        return json.loads(body)["source"]

    def test_sources_crud_endpoints(self):
        status, _, body = self.call("GET", "/api/sources")
        self.assertEqual(json.loads(body)["sources"], [])
        source = self.create_source()
        status, _, body = self.call("GET", "/api/sources")
        self.assertEqual(len(json.loads(body)["sources"]), 1)
        status, _, _ = self.call("POST", "/api/sources/%s/disable" % source["id"])
        self.assertTrue(status.startswith("200"))
        self.assertFalse(self.store.get_source(source["id"]).enabled)
        status, _, _ = self.call("POST", "/api/sources/%s/enable" % source["id"])
        self.assertTrue(status.startswith("200"))
        self.assertTrue(self.store.get_source(source["id"]).enabled)

    def test_run_source_and_ingest_runs(self):
        source = self.create_source()
        status, _, body = self.call("POST", "/api/sources/%s/run" % source["id"])
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["run"]["added"], 3)
        status, _, body = self.call("GET", "/api/ingest-runs")
        self.assertEqual(len(json.loads(body)["ingest_runs"]), 1)

    def test_ingest_all_endpoint(self):
        self.create_source()
        status, _, body = self.call("POST", "/api/ingest", b"{}")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["added"], 3)
        status, _, body = self.call("POST", "/api/ingest", b"{}")
        self.assertEqual(json.loads(body)["added"], 0)
        self.assertEqual(json.loads(body)["skipped"], 3)

    def test_ingest_without_enabled_sources_is_ok(self):
        source = self.create_source()
        self.call("POST", "/api/sources/%s/disable" % source["id"])
        status, _, body = self.call("POST", "/api/ingest", b"{}")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["added"], 0)

    def test_unknown_source_run_returns_404(self):
        status, _, _ = self.call("POST", "/api/sources/nope/run")
        self.assertTrue(status.startswith("404"))

    def test_missing_path_returns_400(self):
        status, _, _ = self.call(
            "POST", "/api/sources", json.dumps({"name": "x"}).encode()
        )
        self.assertTrue(status.startswith("400"))

    def test_run_missing_path_returns_400(self):
        source = self.create_source(path=os.path.join(self.tmp.name, "gone.csv"))
        status, _, _ = self.call("POST", "/api/sources/%s/run" % source["id"])
        self.assertTrue(status.startswith("400"))

    def test_ingest_run_visible_in_report_decisions(self):
        source = self.create_source()
        self.call("POST", "/api/sources/%s/run" % source["id"])
        _, _, body = self.call("GET", "/api/report")
        actions = [d["action"] for d in json.loads(body)["decisions"]]
        self.assertIn("ingest", actions)

    def test_dashboard_shows_sources_panel(self):
        source = self.create_source()
        self.call("POST", "/api/sources/%s/run" % source["id"])
        status, _, body = self.call("GET", "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"id=\"sources\"", body)
        self.assertIn(b"renderSources", body)
        self.assertIn(b"source-run", body)
        self.assertIn(source["id"].encode(), body)


if __name__ == "__main__":
    unittest.main()
