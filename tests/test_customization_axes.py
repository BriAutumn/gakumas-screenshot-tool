import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
FIXTURES=ROOT/'tests/fixtures/customization-axes'


class StateTemplateTests(unittest.TestCase):
    def setUp(self):
        from gkshot.catalog import Catalog
        self.catalog=Catalog()
        self.rules=json.loads((ROOT/'configs/recognition/card-detail-rules.json').read_text(encoding='utf8'))

    def test_levels_are_total_modifiers_not_cumulative_sums(self):
        from gkshot.recognition.customization_rules import numeric_deltas
        self.assertEqual(list(map(int,numeric_deltas(self.catalog.customizations[52],'goodConditionTurns'))),[0,1,3])
        self.assertEqual(list(map(int,numeric_deltas(self.catalog.customizations[59],'genki'))),[0,4,13])

    def test_complex_or_partial_catalog_modifiers_are_refused(self):
        from gkshot.recognition.customization_rules import numeric_deltas
        for change in ('effects','actions'):
            bad=copy.deepcopy(self.catalog.customizations[52]);bad[change]+='; score+=99'
            with self.assertRaises(ValueError):numeric_deltas(bad,'goodConditionTurns')
        with self.assertRaises(ValueError):numeric_deltas(self.catalog.customizations[52],'genki')

    def test_all_axes_are_accounted_for_and_unsupported_is_not_empty(self):
        from gkshot.recognition.customization_rules import compile_states
        rule=next(r for r in self.rules['rules'] if r['card_id']==129)
        states=compile_states(rule,self.catalog)
        self.assertEqual({c['token'] for c in states},{'','36x1','29x1','36x1e29x1'})
        self.assertEqual({c['corner_cost'] for c in states},{1,3})
        bad=copy.deepcopy(rule);bad['state_template']['axes'].pop()
        with self.assertRaises(ValueError):compile_states(bad,self.catalog)

    def test_full_text_distinguishes_joint_increments_and_added_effect(self):
        from gkshot.recognition.card_detail import match_rule
        text='静かな意志+試験・ステージ開始時手札に入る集中+7好調6ターン元気+9試験・ステージ中1回'
        match=lambda t:match_rule([{'text':t,'score':.99}],111,self.catalog,self.rules)
        self.assertEqual(match(text)['token'],'28x1e52x2e54x2')
        self.assertIsNone(match(text.replace('好調6','好調7'))['token'])
        self.assertIsNone(match(text.replace('試験・ステージ中1回',''))['token'])
        self.assertIsNone(match(text+'好印象+2')['token'])


class CustomizationAxesImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        cls.cases=json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8'));cls.reports={}
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            cls.scanner=CardDetailScanner()
            for row in cls.cases:
                cls.reports[row['ordinal']]=cls.scanner.scan(FIXTURES/row['tooltip'],expected_card_id=row['card_id'],
                    client_area=True,owner_path=FIXTURES/row['owner'])

    def test_raw_development_and_held_out_images(self):
        for row in self.cases:
            with self.subTest(ordinal=row['ordinal']):
                for file in row['files']:
                    self.assertEqual(hashlib.sha256((FIXTURES/file['file']).read_bytes()).hexdigest(),file['sha256'])
                report=self.reports[row['ordinal']]
                self.assertEqual(report['status'],row.get('expected_status','matched'))
                self.assertEqual(report['token'],row['token'] if row.get('expected_status','matched')=='matched' else None)

    def test_cost_evidence_disambiguates_identical_tooltip_text(self):
        from gkshot.recognition.card_detail import match_rule
        from gkshot.recognition.card_corner import valid_cost
        s=self.scanner;r=self.reports[71];reads=r['attempts'][0]['reads'];proof=r['corner_evidence']
        self.assertTrue(valid_cost(proof,129,s.catalog.skill_cards[129]['name']))
        self.assertEqual(proof['value'],3)
        self.assertEqual(match_rule(reads,129,s.catalog,s.profiles,proof)['token'],'36x1')
        self.assertIsNone(match_rule(reads,129,s.catalog,s.profiles)['token'])
        for key,value in [('reads',proof['reads'][:2]),('geometry',[]),('value',1),('slot',6),('owner_sha256','')]:
            bad=copy.deepcopy(proof);bad[key]=value
            self.assertIsNone(match_rule(reads,129,s.catalog,s.profiles,bad)['token'])

    def test_erased_cost_and_duplicate_owner_card_do_not_guess(self):
        from gkshot.recognition.card_corner import read_cost
        from PIL import Image,ImageDraw
        s=self.scanner.memory;r=self.reports[71];proof=r['corner_evidence']
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'erased.png'
            with Image.open(FIXTURES/'071-owner-4.png') as image:
                x,y,_,_=proof['tile_bounds'];a,b,c,d=proof['roi']
                ImageDraw.Draw(image).rectangle((x+a,y+b,x+c,y+d),fill='white');image.save(path)
            self.assertIsNone(read_cost(s,path,129)['value'])
        im,_=s.normalize(FIXTURES/'071-owner-4.png',client_area=True)
        box=next(b for slot,b in s.boxes(im) if slot==3)
        with patch.object(s,'boxes',return_value=[(3,box),(4,box)]):
            self.assertEqual(read_cost(s,FIXTURES/'071-owner-4.png',129)['reason'],'owner-card-not-unique')

    def test_missing_wrapped_character_is_not_filled_from_rule(self):
        from PIL import ImageDraw
        from gkshot.recognition.detail_fragments import repair_fragments
        from gkshot.recognition.card_detail import match_rule
        s=self.scanner;r=self.reports[128]
        im,_=s.memory.normalize(FIXTURES/'128-tooltip-6.png',client_area=True);crop=im.crop(r['popup_box'])
        reads=s.read(crop);complete=repair_fragments(s,crop,copy.deepcopy(reads),1)
        self.assertTrue(any(row.get('fragment_evidence') for row in complete))
        enlarged_low_resolution=repair_fragments(s,crop,copy.deepcopy(reads),1,source_ratio=.5)
        self.assertFalse(any(row.get('fragment_evidence') for row in enlarged_low_resolution))
        erased=crop.copy();ImageDraw.Draw(erased).rectangle((12,154,65,198),fill='white')
        incomplete=repair_fragments(s,erased,copy.deepcopy(reads),1)
        self.assertFalse(any(row.get('fragment_evidence') for row in incomplete))
        self.assertIsNone(match_rule(incomplete,301,s.catalog,s.profiles)['token'])

    def test_numeric_mutation_and_extra_green_effect_are_refused(self):
        from gkshot.recognition.card_detail import match_rule
        s=self.scanner;r=self.reports[64];reads=copy.deepcopy(r['attempts'][0]['reads'])
        for row in reads:row['text']=row['text'].replace('好調9','好調10')
        self.assertIsNone(match_rule(reads,295,s.catalog,s.profiles)['token'])
        reads=copy.deepcopy(r['attempts'][0]['reads'])+[{'text':'集中+99','score':.99}]
        self.assertIsNone(match_rule(reads,295,s.catalog,s.profiles)['token'])

    def test_library_refuses_cost_from_wrong_slot_or_owner(self):
        from gkshot.memory_library import MemoryLibrary
        s=self.scanner;r=self.reports[71]
        memory=s.memory.scan([FIXTURES/'071-owner-4.png'],client_area=True)
        with tempfile.TemporaryDirectory() as directory:
            library=MemoryLibrary(Path(directory));identity=library.import_report(memory)['memory_id']
            self.assertTrue(library.add_card_detail(identity,r,catalog=s.catalog,slot=3)['applied'])
            for key,value in [('slot',4),('owner_sha256','another-owner')]:
                bad=copy.deepcopy(r);bad['corner_evidence'][key]=value
                with self.assertRaisesRegex(ValueError,'归属'):
                    library.add_card_detail(identity,bad,catalog=s.catalog,slot=3)


if __name__=='__main__':unittest.main()
