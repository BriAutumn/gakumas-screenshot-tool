import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
F=ROOT/'tests/fixtures/card-identity'


class FixtureSources:
    def source(self,image):
        path=F/image['path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=image['sha256']:raise ValueError('Changed source')
        return path


class CardIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        from gkshot.recognition.card_detail import CardDetailScanner
        from gkshot.recognition.bound_card_identity import refine
        cls.memory=MemoryScanner();cls.detail=CardDetailScanner(cls.memory)
        cls.images=json.loads((F/'provenance.json').read_text(encoding='utf8'))
        cls.source=FixtureSources();cls.before={};cls.after={}
        for ordinal in ('72','192'):
            images=cls.images[ordinal]
            report=cls.memory.scan([cls.source.source(i) for i in images if i['role'] in ('overview-top','overview-cards')],client_area=True)
            cls.before[ordinal]=copy.deepcopy(report)
            refine(report,cls.source,images,cls.memory,cls.detail)
            cls.after[ordinal]=report

    def test_white_card_with_broken_corner_contour_has_six_slots(self):
        page=self.memory.page(self.source.source(self.images['56'][0]),client_area=True)
        self.assertEqual([c['slot'] for c in page['cards']],list(range(6)))
        self.assertEqual([c['id'] for c in page['cards']],[816,620,36,593,117,163])

    def test_blank_or_missing_edge_is_not_recovered_from_spacing(self):
        from PIL import ImageDraw
        from gkshot.recognition.card_frames import complete_card_frames
        image,_=self.memory.normalize(self.source.source(self.images['56'][0]),client_area=True)
        anchors=[(79,1202,211,209),(315,1202,211,209),(786,1202,212,209)]
        for box in ((540,1190,775,1425),(544,1260,558,1370)):
            changed=image.copy();ImageDraw.Draw(changed).rectangle(box,fill='white')
            found=complete_card_frames(changed,anchors,top=985,bottom=1749)
            self.assertFalse(any(abs(x-551)<10 and abs(y-1202)<10 for x,y,w,h in found))

    def test_bound_title_repairs_visual_conflict_and_low_probability(self):
        from gkshot.memory_library import assess_automatic
        for ordinal,expected in [('72',99),('192',711)]:
            report=copy.deepcopy(self.after[ordinal])
            self.assertEqual(report['draft']['card_ids'][2],expected)
            assess_automatic(report,self.memory.catalog)
            self.assertNotIn('card_ids.2',report['automatic_check']['unresolved'])
            self.assertTrue(report['identity_sources'])
            self.assertNotEqual(self.before[ordinal]['draft']['card_ids'][2],expected)
        self.assertEqual(self.after['192']['draft']['card_ids'][3],305)

    def test_incomplete_or_wrong_slot_proof_cannot_authorize_identity(self):
        from gkshot.recognition.native_resolution import card_accepted
        card=next(c for p in self.after['72']['pages'] for c in p['cards'] if c['slot']==2)
        self.assertTrue(card_accepted(card))
        for key,value in [('owner_slot',3),('source_sha256',''),('header_rms',16),('owner_support',None)]:
            bad=copy.deepcopy(card);bad['bound_identity'][key]=value
            self.assertFalse(card_accepted(bad),key)
        bad=copy.deepcopy(card);bad['bound_identity']['title']['reads'][1]['text']='成長痛+'
        self.assertFalse(card_accepted(bad))

    def test_same_header_tooltip_cannot_be_assigned_to_another_slot(self):
        from gkshot.recognition.bound_card_identity import refine
        report=copy.deepcopy(self.before['72']);images=copy.deepcopy(self.images['72'])
        for page in report['pages']:
            original=next(c for c in page['cards'] if c['slot']==2)
            for c in page['cards']:
                if c['slot']==3:c.clear();c.update(copy.deepcopy(original));c['slot']=3
        report['draft']['card_ids'][3]=None
        for i in images:
            if i['role'] in ('card-before','card-tooltip'):i['slot']=4
        refine(report,self.source,images,self.memory,self.detail)
        self.assertIsNone(report['draft']['card_ids'][3])

    def test_identity_sources_are_archived_and_unknown_customization_stays_unknown(self):
        from gkshot.memory_library import MemoryLibrary
        report=copy.deepcopy(self.after['72'])
        self.assertIsNone(report['draft']['customizations'][2])
        with tempfile.TemporaryDirectory() as folder:
            library=MemoryLibrary(Path(folder))
            result=library.import_report(report,capture_id='identity-fixture')
            saved=library.get(result['memory_id'])['report']
            self.assertIsNone(saved['draft']['customizations'][2])
            for source in saved['identity_sources']:self.assertTrue(library.source_path(source).is_file())

    def test_kana_equivalence_and_art_backed_single_error_are_not_arbitrary_fuzzy_names(self):
        from gkshot.recognition.card_names import canonical_name,art_backed_name_matches
        self.assertEqual(canonical_name('輝くキミヘ＋'),canonical_name('輝くキミへ+'))
        self.assertTrue(art_backed_name_matches('天真燗漫+','天真爛漫+',self.memory.catalog))
        self.assertFalse(art_backed_name_matches('アイドル魂+','アイドル宣言+',self.memory.catalog))

    def test_matching_label_can_corroborate_a_low_score_without_changing_the_id(self):
        from unittest.mock import patch
        from gkshot.recognition.native_resolution import card_accepted
        original={'id':436,'method':'onnx','candidates':[{'id':436,'score':.93}],
                  'template_candidates':[{'id':436,'distance':1600}],'plus_glyph_distance':5520}
        with patch.object(self.memory,'text',return_value={'text':'クライマックス','score':.87}):
            result=self.memory.verify_card_label(copy.deepcopy(original),None)
            self.assertEqual(result['id'],436)
            self.assertTrue(card_accepted(result))
            for distance in (3500,4500):
                bad=copy.deepcopy(original);bad['plus_glyph_distance']=distance
                self.assertFalse(card_accepted(self.memory.verify_card_label(bad,None)))


if __name__=='__main__':unittest.main()
