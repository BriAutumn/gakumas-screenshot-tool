import base64
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from gkshot.adapters.gktools import GKToolsAdapter
from gkshot.cli import main
from gkshot.models import Loadout
from gkshot.pipeline import run
from gkshot.recognition.manual import ManualAnnotationRecognizer

FIXTURES = ROOT / "tests" / "fixtures"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((FIXTURES / "example-url.loadout.json").read_text())

    def test_reference_url_preserves_all_slots_and_tokens(self):
        actual = GKToolsAdapter().to_url(Loadout.from_dict(self.data))
        expected = (FIXTURES / "example-url.txt").read_text().strip()
        self.assertEqual(actual, expected)
        query = parse_qs(urlsplit(actual).query)
        self.assertEqual(query["support_bonus"], ["0.0472"])
        self.assertEqual(query["customizations"], ["-----_---31x1--"])

    def test_json_roundtrip(self):
        self.assertEqual(Loadout.from_dict(self.data).to_dict(), self.data)

    def test_invalid_schema_types_and_nonfinite_values_rejected(self):
        for field, value in (("schema_version", 2), ("schema_version", True),
                             ("stage_id", -1), ("stage_id", True),
                             ("support_bonus", float("nan")),
                             ("support_bonus", float("inf")), ("support_bonus", "4.72%")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                Loadout.from_dict({**self.data, field: value})

    def test_missing_and_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            Loadout.from_dict({k: v for k, v in self.data.items() if k != "stage_id"})
        with self.assertRaises(ValueError):
            Loadout.from_dict({**self.data, "typo": 1})

    def test_slot_mismatch_and_delimiter_injection_rejected(self):
        for field, value in (("item_ids", [293]), ("card_ids", [[567] * 6]),
                             ("customizations", [[""] * 6, ["31x1-extra"] * 6])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Loadout.from_dict({**self.data, field: value})

    def test_manual_pipeline_cli_and_image_binding(self):
        # A synthetic 1x1 PNG for plumbing only, separate from the real screenshot fixture.
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0l8AAAAASUVORK5CYII=")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            screenshot = folder / "synthetic.png"
            annotations = folder / "annotations.json"
            output = folder / "result.json"
            screenshot.write_bytes(png)
            annotations.write_text(json.dumps({"image_sha256": hashlib.sha256(png).hexdigest(),
                                               "loadout": self.data}), encoding="utf-8")
            recognizer = ManualAnnotationRecognizer(annotations)
            result = run(screenshot, recognizer, GKToolsAdapter())
            self.assertEqual(result.recognition.source, "manual-annotation")
            self.assertTrue(result.recognition.warnings)
            stdout, stderr = io.StringIO(), io.StringIO()
            command = ["recognize", str(screenshot), "--annotations", str(annotations), "--output", str(output)]
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(command), 0)
            self.assertEqual(Loadout.load(output).to_dict(), self.data)
            self.assertIn("Manual annotations", stderr.getvalue())
            self.assertEqual(stdout.getvalue().strip(), result.url)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 2)
            self.assertEqual(Loadout.load(output).to_dict(), self.data)
            screenshot.write_bytes(png + b"changed")
            with self.assertRaisesRegex(ValueError, "does not match"):
                recognizer.recognize(screenshot)

    def test_automatic_recognition_is_not_silently_faked(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(["recognize", "missing.png", "--output", "unused.json"])
        self.assertEqual(code, 2)
        self.assertIn("Use 'scan'", errors.getvalue())

    def test_real_entrypoint_runs_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--config",
                                     str(ROOT / "configs" / "default.toml"), "url",
                                     str(FIXTURES / "example-url.loadout.json")],
                                    cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), (FIXTURES / "example-url.txt").read_text().strip())


if __name__ == "__main__":
    unittest.main()
