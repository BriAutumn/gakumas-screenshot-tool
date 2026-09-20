import copy
import importlib.util
import json
import socket
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
class CardDetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        # Model initialization and actual image recognition must work without network.
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
            cls.scanner = CardDetailScanner()
            cls.report = cls.scanner.scan(FIXTURES / "card-detail-bishitto.png")
        cls.memory_report = cls.scanner.memory.scan([FIXTURES / "detail-top.png", FIXTURES / "detail-bottom.png"])

    def test_real_screenshot_offline(self):
        self.assertEqual(self.report["status"], "matched")
        self.assertEqual(self.report["card_id"], 778)
        self.assertEqual(self.report["token"], "106x1")
        self.assertEqual(self.report["observed"], {"threshold": 10, "score_percent": 130, "buff_percent": 200, "buff_turns": 4, "effect_turns": 4, "limit": 1})
        self.assertEqual(len(self.report["attempts"]), 2)

    def test_text_constraints_distinguish_states_and_missing_fields(self):
        from gkshot.recognition.card_detail import match_rule
        reads = self.report["attempts"][0]["reads"]
        def match(rows, card=778):
            return match_rule(rows, card, self.scanner.catalog, self.scanner.profiles)
        for threshold, percent, expected in ((10, 100, ""), (6, 100, "105x1"), (10, 130, "106x1"), (6, 130, "105x1e106x1")):
            modified = copy.deepcopy(reads)
            for row in modified:
                row["text"] = row["text"].replace("10以上", f"{threshold}以上").replace("130", str(percent))
            self.assertEqual(match(modified)["token"], expected)
        for fragment in ("10以上", "130", "200", "以降", "中1回", "びしっと"):
            rows = [r for r in reads if fragment not in r["text"]]
            result = match(rows)
            self.assertIsNone(result["token"])
            self.assertEqual(result["status"], "needs-detail")
        bad = copy.deepcopy(reads)
        for row in bad:
            row["text"] = row["text"].replace("130", "180")
        self.assertEqual(match(bad)["status"], "conflict")
        self.assertEqual(match(reads, 89)["status"], "unsupported")
        extra = reads + [{"text": "の100%分スコア上昇", "score": 0.99}]
        self.assertEqual(match(extra)["status"], "conflict")

    def test_rule_changes_fail_closed(self):
        from gkshot.recognition.card_detail import match_rule
        rules = copy.deepcopy(self.scanner.profiles)
        rules["rules"][0]["catalog_fingerprint"] = "different"
        result = match_rule(self.report["attempts"][0]["reads"], 778, self.scanner.catalog, rules)
        self.assertEqual(result["status"], "rules-changed")
        self.assertIsNone(result["token"])

    def test_wrong_card_and_no_popup_do_not_guess(self):
        wrong = self.scanner.scan(FIXTURES / "card-detail-bishitto.png", expected_card_id=89)
        self.assertEqual(wrong["status"], "conflict")
        self.assertIsNone(wrong["token"])
        no_popup = self.scanner.scan(FIXTURES / "detail-bottom.png")
        self.assertEqual(no_popup["status"], "needs-detail")
        self.assertIsNone(no_popup["token"])

    def test_popup_position_and_window_scale(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as directory:
            with Image.open(FIXTURES / "card-detail-bishitto.png") as source:
                enlarged = source.resize((source.width * 2, source.height * 2), Image.Resampling.LANCZOS)
                scaled = Path(directory) / "scaled.png"
                enlarged.save(scaled)
                result = self.scanner.scan(scaled)
                self.assertEqual(result["token"], "106x1", result)
                # Relocate the entire popup within the game viewport, preserving the fixed header.
                changed = source.copy()
                popup = source.crop((15, 523, 290, 758))
                ImageDraw.Draw(changed).rectangle((18, 527, 284, 753), fill=(240, 240, 240))
                changed.paste(popup, (250, 544))
                moved = Path(directory) / "moved.png"
                changed.save(moved)
                result = self.scanner.scan(moved)
                self.assertEqual(result["card_id"], 778, result)
                self.assertEqual(result["token"], "106x1", result)
                # Remove the bottom part of the tooltip. A single remaining candidate is insufficient.
                clipped = source.copy()
                ImageDraw.Draw(clipped).rectangle((18, 696, 284, 753), fill=(240, 240, 240))
                truncated = Path(directory) / "truncated.png"
                clipped.save(truncated)
                self.assertIsNone(self.scanner.scan(truncated)["token"])

    def test_attach_archive_idempotence_and_owner_checks(self):
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            library = MemoryLibrary(Path(directory))
            memory_id = library.import_report(self.memory_report)["memory_id"]
            result = library.add_card_detail(memory_id, self.report, catalog=self.scanner.catalog)
            self.assertTrue(result["applied"])
            record = library.get(memory_id)
            self.assertEqual(record["report"]["draft"]["customizations"], ["", "", "", "", "106x1", ""])
            self.assertEqual(record["status"], "auto-resolved")
            self.assertIsNone(record["review"])
            self.assertEqual(len(library.list(ready_only=True)),1)
            self.assertEqual(library.list(reviewed_only=True),[])
            saved = record["report"]["card_details"][0]
            self.assertEqual(library.source_path(saved).read_bytes(), (FIXTURES / "card-detail-bishitto.png").read_bytes())
            self.assertEqual(library.add_card_detail(memory_id, self.report, catalog=self.scanner.catalog)["detail_status"], "already-attached")
            self.assertEqual(len(library.get(memory_id)["report"]["card_details"]), 1)
            wrong = copy.deepcopy(self.report)
            wrong["identity"]["power"] += 1
            with self.assertRaises(ValueError): library.add_card_detail(memory_id, wrong, catalog=self.scanner.catalog)
            with self.assertRaises(ValueError): library.add_card_detail(memory_id, self.report, catalog=self.scanner.catalog, slot=1)
            wrong = copy.deepcopy(self.report)
            wrong["token"] = ""
            with self.assertRaises(ValueError): library.add_card_detail(memory_id, wrong, catalog=self.scanner.catalog)

    def test_human_review_not_overwritten(self):
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            library = MemoryLibrary(Path(directory))
            memory_id = library.import_report(self.memory_report)["memory_id"]
            draft = copy.deepcopy(self.memory_report["draft"])
            draft["customizations"] = [""] * 6
            library.save_review(memory_id, draft, confirmed=True, catalog=self.scanner.catalog)
            result = library.add_card_detail(memory_id, self.report, catalog=self.scanner.catalog)
            self.assertTrue(result["human_review_preserved"])
            self.assertFalse(result["applied"])
            self.assertEqual(library.get(memory_id)["review"]["draft"], draft)

    def test_conflicting_prior_effect_and_repeated_slots(self):
        from gkshot.memory_library import MemoryLibrary
        with tempfile.TemporaryDirectory() as directory:
            library = MemoryLibrary(Path(directory))
            old = copy.deepcopy(self.memory_report)
            old["draft"]["customizations"][4] = ""
            memory_id = library.import_report(old)["memory_id"]
            result = library.add_card_detail(memory_id, self.report, catalog=self.scanner.catalog)
            self.assertEqual(result["detail_status"], "conflict")
            record = library.get(memory_id)
            self.assertIsNone(record["report"]["draft"]["customizations"][4])
            self.assertEqual(record["status"], "incomplete")
            repeated = copy.deepcopy(self.memory_report)
            repeated["draft"]["card_ids"][3] = 778
            other_id = library.import_report(repeated, capture_id="repeated-slots")["memory_id"]
            with self.assertRaises(ValueError):
                library.add_card_detail(other_id, self.report, catalog=self.scanner.catalog)
            result = library.add_card_detail(other_id, self.report, catalog=self.scanner.catalog, slot=4)
            self.assertTrue(result["applied"])
            self.assertEqual(library.get(other_id)["report"]["draft"]["customizations"][3],"")

    def test_gui_detail_import_background_flow(self):
        import tkinter as tk
        import time
        from gkshot.memory_gui import MemoryLibraryWindow
        from gkshot.memory_library import MemoryLibrary
        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as directory:
                library = MemoryLibrary(Path(directory))
                memory_id = library.import_report(self.memory_report)["memory_id"]
                window = MemoryLibraryWindow(root, library)
                window.detail_scanner = self.scanner
                window.scanner = self.scanner.memory
                with patch.object(self.scanner, "scan", return_value=self.report):
                    window.begin_card_details(memory_id, [FIXTURES / "card-detail-bishitto.png"])
                    deadline = time.monotonic() + 5
                    while window.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                self.assertFalse(window.busy)
                self.assertIn("已自动识别", window.status.get())
                self.assertEqual(library.get(memory_id)["report"]["draft"]["customizations"][4], "106x1")
        finally:
            root.destroy()
