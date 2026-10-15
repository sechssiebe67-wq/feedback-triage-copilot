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


if __name__ == "__main__":
    unittest.main()
