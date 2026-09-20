"""Real capture regressions and refusal tests for the first full-batch fixes."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
FIXTURES=ROOT/'tests/fixtures/recognition-v2'


class RecognitionV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            cls.scanner=CardDetailScanner()
        cls.reports={}
        for ordinal,slot in ((18,6),(25,3),(43,4),(69,6),(71,6),(81,6)):
            cls.reports[ordinal]=cls.scanner.scan(FIXTURES/f'{ordinal:03d}-card-tooltip-{slot}.png',
                client_area=True,owner_path=FIXTURES/f'{ordinal:03d}-card-before-{slot}.png')

    def test_fixture_integrity(self):
        for row in json.loads((FIXTURES/'provenance.json').read_text()):
            self.assertEqual(hashlib.sha256((FIXTURES/row['file']).read_bytes()).hexdigest(),row['sha256'])

    def test_actual_customizations_and_independent_held_out_memories(self):
        for ordinal,card,token in ((18,441,'35x1'),(25,87,''),(43,87,''),(69,772,'27x1'),(71,171,'33x1')):
            report=self.reports[ordinal]
            self.assertEqual(report['card_id'],card)
            self.assertEqual(report['status'],'matched',(ordinal,report))
            self.assertEqual(report['token'],token)
        # This same card's second held-out image still has an uncertain line.
        # It is not forced through merely because another memory was readable.
        self.assertEqual(self.reports[81]['status'],'needs-detail')
        self.assertIsNone(self.reports[81]['token'])

    def test_full_effect_text_rejects_missing_changed_and_extra_effects(self):
        from gkshot.recognition.card_detail import match_rule
        s=self.scanner
        report=self.reports[69]
        original=report['attempts'][0]['reads']
        for fragment in ('体力消費','以降','280','試験'):
            reads=[r for r in original if fragment not in r['text']]
            self.assertIsNone(match_rule(reads,772,s.catalog,s.profiles)['token'])
        reads=copy.deepcopy(original)
        for row in reads: row['text']=row['text'].replace('280','380')
        self.assertIsNone(match_rule(reads,772,s.catalog,s.profiles)['token'])
        self.assertIsNone(match_rule(original+[{'text':'スキルカードを引く','score':.99}],772,s.catalog,s.profiles)['token'])
        # An upgrade-hand addition cannot masquerade as the star's base draw.
        reads=copy.deepcopy(self.reports[71]['attempts'][0]['reads'])
        reads.append({'text':'手札をすべて強化','score':.99})
        self.assertIsNone(match_rule(reads,171,s.catalog,s.profiles)['token'])

    def test_unread_green_addition_is_not_inferred_absent(self):
        report=self.reports[69]
        im,_=self.scanner.memory.normalize(FIXTURES/'069-card-tooltip-6.png',client_area=True)
        crop=im.crop(report['popup_box'])
        reads=[r for r in report['attempts'][0]['reads'] if r['text']!='元気+4']
        self.assertFalse(self.scanner.green_text_coverage(crop,reads)['complete'])

    def test_rank_glyph_does_not_add_a_leading_digit(self):
        report=self.scanner.memory.page(FIXTURES/'018-overview-top.png',client_area=True)
        self.assertEqual(report['parameters'],{'vocal':1266,'dance':2018,'visual':2899,'stamina':35})
        self.assertEqual(report['number_reads']['visual']['method'],'neutral-digit-components')
        self.assertEqual(report['items'][0]['id'],206)
        from PIL import ImageDraw
        im,_=self.scanner.memory.normalize(FIXTURES/'018-overview-top.png',client_area=True)
        crop=im.crop(self.scanner.memory.layout['parameters']['visual'])
        # A glyph clipped at the crop boundary must not be reconstructed.
        clipped=crop.crop((25,0,crop.width,crop.height))
        self.assertNotEqual(self.scanner.memory.read_parameter(clipped).get('method'),'neutral-digit-components')

    def test_high_confidence_model_does_not_override_conflicting_name(self):
        s=self.scanner.memory
        record={'id':459,'candidates':[{'id':459,'score':.99999}],
                'template_candidates':[{'id':459,'distance':600}],'plus_glyph_distance':100}
        with patch.object(s,'text',return_value={'text':'せのびの魔法+','score':.98}):
            result=s.verify_card_label(record,None)
        self.assertIsNone(result['id'])
        self.assertEqual(result['method'],'conflicting-card-label')

    def test_title_punctuation_needs_a_visible_cross(self):
        from PIL import ImageDraw
        s=self.scanner
        report=self.reports[18]
        im,_=s.memory.normalize(FIXTURES/'018-card-tooltip-6.png',client_area=True)
        crop=im.crop(report['popup_box'])
        row=copy.deepcopy(report['attempts'][0]['reads'][0])
        row['text']='アイドルになります：'
        s.repair_title_plus(crop,row)
        self.assertEqual(row['text'],'アイドルになります+')
        row=copy.deepcopy(report['attempts'][0]['reads'][0])
        row['text']='アイドルになります：'
        b=row['box']; right=max(p[0] for p in b)
        erased=crop.copy()
        ImageDraw.Draw(erased).rectangle((right-22,0,right+6,85),fill='white')
        s.repair_title_plus(erased,row)
        self.assertEqual(row['text'],'アイドルになります：')


if __name__=='__main__': unittest.main()
