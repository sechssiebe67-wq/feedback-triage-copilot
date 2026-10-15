import contextlib
import io
import os
import tempfile
import unittest

from feedback_triage.cli import build_parser, main


class CliTests(unittest.TestCase):
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

    def test_parser_requires_command(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_import_then_cluster_then_history(self):
        code, out = self.run_cli(["import", self.csv_path, "--source", "support"])
        self.assertEqual(code, 0)
        self.assertIn("Imported 3", out)

        code, out = self.run_cli(["cluster"])
        self.assertEqual(code, 0)
        self.assertIn("clusters", out)

        code, out = self.run_cli(["history"])
        self.assertEqual(code, 0)
        self.assertIn("import", out)

    def test_report_csv_to_file(self):
        self.run_cli(["import", self.csv_path])
        out_path = os.path.join(self.tmp.name, "report.csv")
        code, out = self.run_cli(["report", "--format", "csv", "--out", out_path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path, encoding="utf-8") as handle:
            self.assertIn("cluster_id", handle.read())

    def test_report_pdf_to_file(self):
        self.run_cli(["import", self.csv_path])
        out_path = os.path.join(self.tmp.name, "report.pdf")
        code, _ = self.run_cli(["report", "--format", "pdf", "--out", out_path])
        self.assertEqual(code, 0)
        with open(out_path, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"%PDF"))

    def test_import_no_recluster(self):
        code, _ = self.run_cli(["import", self.csv_path, "--no-recluster"])
        self.assertEqual(code, 0)

    def test_reviewer_add_and_list(self):
        code, out = self.run_cli(["reviewer", "add", "Alice"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)
        code, out = self.run_cli(["reviewer", "list"])
        self.assertEqual(code, 0)
        self.assertIn("Alice", out)
        with open(self.workspace, encoding="utf-8") as handle:
            import json

            saved = json.load(handle)
        self.assertEqual(saved["reviewers"][0]["name"], "Alice")

    def test_import_with_reviewer_then_decisions(self):
        self.run_cli(["reviewer", "add", "Alice"])
        code, _ = self.run_cli(
            ["import", self.csv_path, "--source", "support", "--reviewer", "Alice"]
        )
        self.assertEqual(code, 0)
        code, out = self.run_cli(["decisions"])
        self.assertEqual(code, 0)
        self.assertIn("import", out)
        self.assertIn("Alice", out)

    def test_cluster_and_report_accept_reviewer_note(self):
        self.run_cli(["import", self.csv_path])
        code, _ = self.run_cli(
            ["cluster", "--reviewer", "Alice", "--note", "retune"]
        )
        self.assertEqual(code, 0)
        out_path = os.path.join(self.tmp.name, "report.json")
        code, _ = self.run_cli([
            "report", "--format", "json", "--out", out_path,
            "--reviewer", "Alice", "--note", "week",
        ])
        self.assertEqual(code, 0)
        with open(out_path, encoding="utf-8") as handle:
            contents = handle.read()
        self.assertIn("Alice", contents)
        self.assertIn("week", contents)

    def test_decisions_prints_discard_reviewer_and_note(self):
        from feedback_triage.store import TriageStore

        self.run_cli(["reviewer", "add", "Alice"])
        self.run_cli(["import", self.csv_path])
        store = TriageStore.load(self.workspace)
        reviewer_id = store.list_reviewers()[0].id
        cluster_id = store.active_clusters()[0].id
        store.discard(cluster_id, reviewer=reviewer_id, note="duplicate of PDF export")
        code, out = self.run_cli(["decisions"])
        self.assertEqual(code, 0)
        self.assertIn("discard", out)
        self.assertIn("Alice", out)
        self.assertIn("duplicate of PDF export", out)


if __name__ == "__main__":
    unittest.main()
