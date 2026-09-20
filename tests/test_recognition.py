import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from gkshot.catalog import Catalog
from gkshot.models import Loadout
from gkshot.review import reviewed_loadout

FIXTURES = ROOT / "tests/fixtures"
HAS_RECOGNITION = all(importlib.util.find_spec(name) for name in ("PIL", "numpy", "onnxruntime", "rapidocr_onnxruntime"))


@unittest.skipUnless(HAS_RECOGNITION, "Use the project's .venv to run image recognition tests")
class RecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.automatic import ScreenshotScanner
        cls.scanner = ScreenshotScanner()
        cls.report = cls.scanner.scan(FIXTURES / "original-screenshot.png")

    def test_real_pixels_match_manually_verified_entities(self):
        expected = json.loads((FIXTURES / "original-screenshot.expected-recognition.json").read_text())
        for key in ("parameters", "item_ids", "card_ids"):
            self.assertEqual(self.report["draft"][key], expected[key])
        self.assertEqual(self.report["displayed_deltas"]["dance"]["value"], 456)
        self.assertEqual(self.report["cards"][1][0]["method"], "same-image-duplicate")
        self.assertEqual(self.report["cards"][1][3]["method"], "onnx-plus-glyph")

    def test_unseen_settings_and_customizations_are_not_guessed(self):
        draft = self.report["draft"]
        self.assertIsNone(draft["stage_id"])
        self.assertIsNone(draft["support_bonus"])
        self.assertEqual(draft["customizations"], [[None] * 6, [None] * 6])
        with self.assertRaises(ValueError):
            Loadout.from_dict(draft)

    def test_scaled_image_uses_pixels_not_fixture_identity(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scaled.jpg"
            with Image.open(FIXTURES / "original-screenshot.png") as image:
                image.convert("RGB").resize((1064, 774)).save(path, quality=95)
            report = self.scanner.scan(path)
        self.assertNotEqual(report["image_sha256"], self.report["image_sha256"])
        for key in ("parameters", "item_ids", "card_ids"):
            self.assertEqual(report["draft"][key], self.report["draft"][key])

    def test_reordered_cards_are_actually_recognized(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "swapped.png"
            with Image.open(FIXTURES / "original-screenshot.png") as source:
                image = source.convert("RGB")
            a, b = self.scanner.layout["cards"][0][:2]
            left, right = image.crop(a), image.crop(b)
            image.paste(right, a[:2])
            image.paste(left, b[:2])
            image.save(path)
            report = self.scanner.scan(path)
        self.assertEqual(report["draft"]["card_ids"][0][:2], [585, 619])
        self.assertEqual(report["cards"][1][0]["duplicate_of"], 2)
        self.assertEqual(report["cards"][1][1]["duplicate_of"], 1)

    def test_blank_and_wrong_layout_images_are_rejected(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            for size in ((532, 387), (500, 900)):
                path = Path(directory) / f"{size[0]}x{size[1]}.png"
                Image.new("RGB", size, "white").save(path)
                with self.assertRaises(ValueError):
                    self.scanner.scan(path)


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def setUp(self):
        self.draft = json.loads((FIXTURES / "example-url.loadout.json").read_text())

    def test_review_required_and_unknown_customizations_block_export(self):
        with self.assertRaises(ValueError):
            reviewed_loadout(self.draft, confirmed=False, catalog=self.catalog)
        self.draft["customizations"][1][0] = None
        with self.assertRaisesRegex(ValueError, "自定义"):
            reviewed_loadout(self.draft, confirmed=True, catalog=self.catalog)

    def test_valid_reviewed_sample_and_invalid_catalog_ids(self):
        result = reviewed_loadout(self.draft, confirmed=True, catalog=self.catalog)
        self.assertEqual(result.stage_id, 160)
        for key, value in (("stage_id", 999999), ("item_ids", [999999, 0, 0, 0])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                reviewed_loadout({**self.draft, key: value}, confirmed=True, catalog=self.catalog)

    def test_incompatible_customization_and_missing_external_settings(self):
        for field in ("stage_id", "support_bonus"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                reviewed_loadout({**self.draft, field: None}, confirmed=True, catalog=self.catalog)
        self.draft["customizations"][0][0] = "99999x1"
        with self.assertRaises(ValueError):
            reviewed_loadout(self.draft, confirmed=True, catalog=self.catalog)
