"""Regression images from the 2026-09-18 collector batch; no game or network."""
import copy
import hashlib
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
FIXTURES = ROOT/'tests/fixtures/offline-replay'


@unittest.skipUnless(all(importlib.util.find_spec(n) for n in ('PIL','numpy','onnxruntime','rapidocr_onnxruntime','cv2')), 'Use the project virtual environment')
class OfflineReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        from gkshot.recognition.card_detail import CardDetailScanner
        with patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
            cls.memory = MemoryScanner()
            cls.detail = CardDetailScanner(cls.memory)
            cls.report = cls.memory.scan([FIXTURES/'08-overview-top.png',FIXTURES/'08-overview-cards.png'],client_area=True)

    def test_raw_fixture_integrity(self):
        for row in json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8')):
            slot = '-'+str(row['slot']) if row['slot'] else ''
            path = FIXTURES/f"{row['ordinal']:02d}-{row['role']}{slot}.png"
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),row['sha256'])

    def test_card_names_and_art_correct_two_reported_false_matches(self):
        self.assertEqual(self.report['draft']['card_ids'],[633,364,98,772,171,521])
        for slot in (3,4):
            card = next(c for c in self.report['pages'][1]['cards'] if c['slot']==slot)
            self.assertEqual(card['method'],'label-and-template')
            self.assertTrue(card['label_match']['art_agrees'])
        self.assertEqual(self.report['draft']['customizations'],['','','',None,None,''])

    def test_art_and_text_disagreement_does_not_fill_another_guess(self):
        card = copy.deepcopy(self.report['pages'][1]['cards'][3])
        card.update(id=459,template_candidates=[{'id':459,'distance':600}])
        with patch.object(self.memory,'text',return_value={'text':'せのびの魔法+','score':.98}):
            result = self.memory.verify_card_label(card,None)
        self.assertIsNone(result['id'])
        self.assertEqual(result['method'],'conflicting-card-label')

    def test_nonpurple_banner_client_images_and_japanese_identity(self):
        expected = {5:('月村手毬',{'vocal':2807,'dance':1890,'visual':1238,'stamina':37}),
                    12:('花海 佑芽',{'vocal':1961,'dance':3081,'visual':1226,'stamina':35})}
        for ordinal,(name,parameters) in expected.items():
            with self.subTest(ordinal=ordinal):
                page = self.memory.page(FIXTURES/f'{ordinal:02d}-overview-top.png',client_area=True)
                self.assertEqual(page['identity']['idol_name'].replace(' ',''),name.replace(' ',''))
                self.assertEqual(page['parameters'],parameters)
                self.assertEqual(page['frame']['source'],'capture-client-area')

    def test_event_logo_shifts_parameters_without_changing_card_identity(self):
        report = self.memory.scan([FIXTURES/'11-overview-top.png',FIXTURES/'11-overview-cards.png'],client_area=True)
        self.assertEqual(report['draft']['parameters'],{'vocal':1847,'dance':2574,'visual':1172,'stamina':34})
        self.assertEqual(report['draft']['card_ids'],[633,759,128,772,154,160])
        self.assertEqual(report['conflicts'],[])
        self.assertEqual(report['pages'][0]['content_offset'],35)

    def test_popup_covering_stage_uses_verified_background_and_archives_owner(self):
        from gkshot.memory_library import MemoryLibrary
        popup = FIXTURES/'08-card-tooltip-4.png'
        owner = FIXTURES/'08-card-before-4.png'
        detail = self.detail.scan(popup,client_area=True,owner_path=owner,expected_card_id=772)
        self.assertEqual(detail['card_id'],772)
        self.assertEqual(detail['status'],'matched')
        self.assertEqual(detail['token'],'27x1')
        self.assertLess(detail['ownership_evidence']['background_rms'],15)
        with tempfile.TemporaryDirectory() as directory:
            library = MemoryLibrary(Path(directory))
            mid = library.import_report(self.report)['memory_id']
            attached = library.add_card_detail(mid,detail,catalog=self.memory.catalog,slot=3)
            self.assertEqual(attached['detail_status'],'matched')
            self.assertTrue(attached['applied'])
            self.assertEqual(library.get(mid)['report']['draft']['customizations'][3],'27x1')
            evidence = library.get(mid)['report']['card_details'][0]['ownership_evidence']
            self.assertEqual(library.source_path(evidence).read_bytes(),owner.read_bytes())
        with self.assertRaises(ValueError):
            self.detail.scan(popup,client_area=True,owner_path=FIXTURES/'11-overview-cards.png')
        with self.assertRaises(ValueError):
            self.detail.scan(popup,client_area=True)


if __name__=='__main__':
    unittest.main()
