import json,sys,socket,hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
FIXTURES=ROOT/'tests/fixtures/customization-followup'

class FollowupTests(unittest.TestCase):
    def test_full_original_panels_including_held_out(self):
        from gkshot.recognition.card_detail import CardDetailScanner
        with patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            scanner=CardDetailScanner()
            for row in json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8')):
                with self.subTest(ordinal=row['ordinal']):
                    for key in ('tooltip','owner'):
                        self.assertEqual(hashlib.sha256((FIXTURES/row[key]).read_bytes()).hexdigest(),row[key+'_sha256'])
                    report=scanner.scan(FIXTURES/row['tooltip'],expected_card_id=555,client_area=True,owner_path=FIXTURES/row['owner'])
                    self.assertEqual(report['status'],'matched')
                    self.assertEqual(report['token'],row['token'])

    def test_wrong_values_missing_and_extra_effects_are_refused(self):
        from gkshot.recognition.card_detail import match_rule
        from gkshot.catalog import Catalog
        rules=json.loads((ROOT/'configs/recognition/card-detail-rules.json').read_text(encoding='utf8'))
        text='盛り上げ上手+アクティブスキルカードのスコア値増加+16・コスト値増加+1次のターン、スキルカードを2枚引く強気に変更重複不可試験・ステージ中1回'
        def match(t):return match_rule([{'text':t,'score':.99}],555,Catalog(),rules)
        self.assertEqual(match(text)['token'],'93x1')
        for bad in (text.replace('+16','+18'),text.replace('2枚引く','引く'),text+'元気+9',text.replace('重複不可','')):
            self.assertIsNone(match(bad)['token'])
