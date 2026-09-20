import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests/fixtures/memory-library"
AVAILABLE = all(importlib.util.find_spec(name) for name in ("PIL", "numpy", "onnxruntime", "rapidocr_onnxruntime", "cv2"))


@unittest.skipUnless(AVAILABLE, "Use the project virtual environment")
class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        cls.scanner = MemoryScanner()
        cls.paths = [FIXTURES / "detail-top.png", FIXTURES / "detail-bottom.png"]
        cls.report = cls.scanner.scan(cls.paths, context_path=FIXTURES / "list.png")

    def test_two_scroll_positions_merge_into_one_memory(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(self.report["identity"], expected["identity"])
        for key in ("parameters", "item_ids", "card_ids", "p_idol_id"):
            self.assertEqual(self.report["draft"][key], expected[key])
        self.assertEqual(self.report["draft"]["customizations"], ["", "", "", "", None, ""])
        self.assertEqual(self.report["status"], "needs-review")
        self.assertEqual(len(self.report["pages"][0]["cards"]), 4)
        self.assertEqual(len(self.report["pages"][1]["cards"]), 6)
        self.assertEqual(self.report["pages"][1]["cards"][4]["method"], "onnx-with-template-correction")
        self.assertEqual(len(self.report["evidence"]["card_ids.0"]), 2)
        self.assertEqual(len(self.report["evidence"]["card_ids.4"]), 1)

    def test_incomplete_top_page_does_not_guess_occluded_cards(self):
        report = self.scanner.scan(self.paths[:1])
        self.assertEqual(report["draft"]["card_ids"][4:], [None, None])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("card_ids.4", report["missing_fields"])

    def test_list_and_contest_screens_are_not_memory_details(self):
        for path in (FIXTURES / "list.png", ROOT / "tests/fixtures/original-screenshot.png"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.scanner.scan([path])

    def test_resize_and_reverse_page_order(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for original in reversed(self.paths):
                path = Path(directory) / original.name
                with Image.open(original) as image:
                    image.resize((image.width // 2, image.height // 2)).save(path)
                paths.append(path)
            result = self.scanner.scan(paths)
        self.assertEqual(result["draft"], self.report["draft"])

    def test_mixed_identity_rejected_and_conflicts_remain_unknown(self):
        first, second = [self.scanner.page(path) for path in self.paths]
        bad = copy.deepcopy(second)
        bad["identity"]["power"] += 1
        with patch.object(self.scanner, "page", side_effect=[copy.deepcopy(first), bad]), self.assertRaises(ValueError):
            self.scanner.scan(self.paths)
        bad = copy.deepcopy(second)
        bad["cards"][0]["id"] = 731
        with patch.object(self.scanner, "page", side_effect=[copy.deepcopy(first), bad]):
            report = self.scanner.scan(self.paths)
        self.assertIsNone(report["draft"]["card_ids"][0])
        self.assertIn("card_ids.0", report["conflicts"])
        self.assertEqual(report["status"], "incomplete")

    def test_archive_deduplication_and_review_gate(self):
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            library = MemoryLibrary(Path(directory))
            first = library.import_report(self.report)
            again = library.import_report(self.report)
            self.assertTrue(first["created"])
            self.assertFalse(again["created"])
            self.assertEqual(first["memory_id"], again["memory_id"])
            self.assertEqual(len(library.list()), 1)
            self.assertEqual(library.list(reviewed_only=True), [])
            record = library.get(first["memory_id"])
            for source in record["report"]["pages"] + [record["report"]["list_context"]]:
                self.assertEqual(library.source_path(source).read_bytes(), Path(source["source_path"]).read_bytes())
            with self.assertRaises(ValueError):
                library.save_review(first["memory_id"], self.report["draft"], confirmed=True, catalog=self.scanner.catalog)
            reviewed = copy.deepcopy(self.report["draft"])
            reviewed["customizations"] = [""] * 6
            library.save_review(first["memory_id"], reviewed, confirmed=True, catalog=self.scanner.catalog)
            library.import_report(self.report)
            self.assertEqual(len(library.list(reviewed_only=True)), 1)
            self.assertEqual(library.get(first["memory_id"])["review"]["draft"], reviewed)
            # Explicitly distinct owned copies may have identical content; don't collapse them by stats.
            other = library.import_report(self.report, capture_id="another-owned-copy")
            self.assertTrue(other["created"])
            self.assertEqual(len(library.list()), 2)

    def test_badge_unknown_and_conflicting_pages_never_mean_no_customization(self):
        first,second=[self.scanner.page(path) for path in self.paths]
        changed=copy.deepcopy(second)
        changed["cards"][0]["customization_badge"]["state"]="present"
        with patch.object(self.scanner,"page",side_effect=[copy.deepcopy(first),changed]):
            report=self.scanner.scan(self.paths)
        self.assertIsNone(report["draft"]["customizations"][0])
        self.assertIn("customizations.0",report["conflicts"])
        for page in (first,second):
            for card in page["cards"]:card["customization_badge"]["state"]="unknown"
        with patch.object(self.scanner,"page",side_effect=[first,second]):
            report=self.scanner.scan(self.paths)
        self.assertEqual(report["draft"]["customizations"],[None]*6)

    def test_complete_data_with_uncertain_entity_is_not_automatically_resolved(self):
        from gkshot.memory_library import assess_automatic
        report=copy.deepcopy(self.report)
        report["draft"]["customizations"][4]="106x1"
        for page in report["pages"]:
            for card in page["cards"]:
                if card["slot"]==0:
                    card["method"]="onnx"
                    for candidate in card["candidates"]:candidate["score"]=0.1
        self.assertEqual(assess_automatic(report,self.scanner.catalog),"needs-review")
        self.assertIn("card_ids.0",report["automatic_check"]["unresolved"])

    def test_filled_customizations_without_evidence_do_not_pass_automatic_check(self):
        from gkshot.memory_library import assess_automatic
        report=copy.deepcopy(self.report)
        report["draft"]["customizations"][4]="106x1"
        self.assertEqual(assess_automatic(report,self.scanner.catalog),"needs-review")
        self.assertIn("customizations.4",report["automatic_check"]["unresolved"])

    def test_archive_beyond_windows_260_character_limit(self):
        from gkshot.memory_library import MemoryLibrary
        from gkshot.paths import io_path
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            # Directory and database fit legacy limits; full hash image paths do not.
            root=Path(directory)/("deep-library-"+"x"*(220-len(directory)-14))
            library=MemoryLibrary(root)
            try:
                result=library.import_report(self.report)
                self.assertFalse(library.import_report(self.report)["created"])
                record=library.get(result["memory_id"])
                for source in record["report"]["pages"]:
                    self.assertGreater(len(str(root/source["archive_path"])),260)
                    archived=library.source_path(source)
                    self.assertEqual(archived.read_bytes(),Path(source["source_path"]).read_bytes())
                    with Image.open(archived) as image:self.assertGreater(image.width,0)
                bad=dict(record["report"]["pages"][0],archive_path="../outside.png")
                with self.assertRaises(ValueError):library.source_path(bad)
            finally:
                # Remove only this test's files with long-path-aware I/O; normal
                # TemporaryDirectory cleanup can then remove the short directories.
                for file in io_path(root/"sources").glob("*"):file.unlink()

    def test_batch_import_keeps_groups_separate_and_can_resume(self):
        from gkshot.memory_batch import import_batch
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = MemoryLibrary(root / "library")
            manifest = root / "batch.json"
            manifest.write_text(json.dumps({"schema_version": 1, "memories": [
                {"capture_id": "sample", "details": [str(p) for p in self.paths]},
                {"capture_id": "bad", "details": [str(FIXTURES / "list.png")]}
            ]}), encoding="utf-8")
            result = import_batch(manifest, self.scanner, library)
            self.assertTrue(result[0]["created"])
            self.assertIn("error", result[1])
            rerun = import_batch(manifest, self.scanner, library)
            self.assertFalse(rerun[0]["created"])
            self.assertEqual(len(library.list()), 1)

    def test_memory_window_and_review_editor(self):
        import tkinter as tk
        from gkshot.memory_gui import MemoryLibraryWindow, MemoryEditor
        from gkshot.memory_library import MemoryLibrary
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as directory:
                library = MemoryLibrary(Path(directory))
                memory_id = library.import_report(self.report)["memory_id"]
                window = MemoryLibraryWindow(root, library)
                self.assertEqual(len(window.table.get_children()), 1)
                editor = MemoryEditor(root, library, memory_id, self.scanner.catalog, window.refresh)
                editor.window.withdraw()
                self.assertEqual(editor.parameters["vocal"].get(), "2853")
                with patch("gkshot.memory_gui.messagebox.showerror") as error:
                    editor.save()
                    self.assertTrue(error.called)
                editor.clear_unknown_custom()
                editor.confirmed.set(True)
                editor.card_vars[0].set(editor.card_vars[0].get())
                self.assertFalse(editor.confirmed.get())
                self.assertIsNone(editor.draft["customizations"][0])
                editor.clear_unknown_custom()
                editor.confirmed.set(True)
                editor.save()
                self.assertEqual(len(library.list(reviewed_only=True)), 1)
        finally:
            root.destroy()
