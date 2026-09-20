"""Native fallback must use raw pixels and retain uncertainty."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class NativeResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        from gkshot.recognition.native_resolution import NativeTemplates
        cls.memory = MemoryScanner()
        cls.matcher = NativeTemplates(cls.memory.scanner, 'item')

    def test_crop_preserves_raw_pixels_and_client_offset(self):
        from PIL import Image
        from gkshot.recognition.native_resolution import native_crop
        raw = Image.new('RGB', (220, 430))
        raw.putpixel((30, 50), (123, 45, 67))
        frame = {'left': 10, 'right': 210, 'top': 30, 'normalized_size': [100, 200]}
        crop, bounds = native_crop(raw, frame, (10, 10, 30, 40))
        self.assertEqual(bounds, [30, 50, 70, 110])
        self.assertEqual(crop.size, (40, 60))
        self.assertEqual(crop.getpixel((0, 0)), (123, 45, 67))
        with self.assertRaises(ValueError):
            native_crop(raw, frame, (0, 0, 120, 220))

    def test_native_singleton_and_evidence_validation(self):
        from gkshot.recognition.native_resolution import item_accepted
        image = self.matcher.entries[141][1]
        result = self.matcher.fit(image)
        self.assertTrue(result['accepted'], result)
        self.assertEqual(result['id'], 141)
        self.assertEqual(result['source_size'], list(image.size))
        self.assertGreater(min(image.size), 64)
        record = {'id': 141, 'method': 'native-single-template', 'native_retry': result}
        self.assertTrue(item_accepted(record))
        for key, value in [('native_pixels', False), ('source_size', [48, 48]),
                           ('full_template_distance', 1200), ('candidate_ids', [141, 142])]:
            bad = copy.deepcopy(record)
            bad['native_retry'][key] = value
            self.assertFalse(item_accepted(bad), key)

    def test_base_upgrade_and_occlusion(self):
        from PIL import ImageDraw
        for entity_id in (422, 423):
            image = self.matcher.entries[entity_id][1]
            result = self.matcher.fit(image)
            self.assertTrue(result['accepted'], result)
            self.assertEqual(result['id'], entity_id)
            covered = image.copy()
            ImageDraw.Draw(covered).rectangle((image.width*.63, 0, image.width, image.height*.35), fill='black')
            self.assertFalse(self.matcher.fit(covered)['accepted'])
        self.assertFalse(self.matcher.fit(image.resize((48, 48)))['accepted'])

    def test_method_without_evidence_cannot_pass(self):
        from gkshot.recognition.native_resolution import item_accepted, card_accepted
        self.assertFalse(item_accepted({'id': 141, 'method': 'native-single-template'}))
        self.assertFalse(item_accepted({'id': 423, 'method': 'native-template-and-upgrade-mark'}))
        self.assertFalse(card_accepted({'id': 171, 'method': 'native-card-and-label', 'native_label_agrees': True}))

    def test_unread_detail_retries_raw_but_stays_unknown(self):
        from gkshot.recognition.card_detail import CardDetailScanner
        folder = ROOT / 'tests/fixtures/recognition-v2'
        scanner = CardDetailScanner(self.memory)
        report = scanner.scan(folder/'081-card-tooltip-6.png', client_area=True,
                              owner_path=folder/'081-card-before-6.png')
        self.assertEqual(report['status'], 'needs-detail')
        self.assertIsNone(report['token'])
        self.assertIn('native_retry', report)
        self.assertIsNotNone(report['native_retry']['native_popup_bounds'])

    def test_real_raw_crops_and_unclear_upgrade(self):
        from PIL import Image
        folder = ROOT / 'tests/fixtures/native-resolution'
        for row in json.loads((folder/'provenance.json').read_text(encoding='utf8')):
            path = folder/row['file']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])
            with Image.open(path) as source:
                result = self.matcher.fit(source.convert('RGB'))
            if row['expected_id'] is None:
                self.assertFalse(result['accepted'], result)
            else:
                self.assertTrue(result['accepted'], result)
                self.assertEqual(result['id'], row['expected_id'])


if __name__ == '__main__':
    unittest.main()
