import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
AVAILABLE=all(importlib.util.find_spec(n) for n in ('PIL','numpy','onnxruntime','rapidocr_onnxruntime','cv2'))


@unittest.skipUnless(AVAILABLE,'Use the project virtual environment')
class ItemVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        cls.memory=MemoryScanner()
        cls.scanner=cls.memory.scanner

    def test_clear_absent_upgrade_mark_resolves_a_complete_no_custom_memory(self):
        from gkshot.memory_library import assess_automatic
        f=ROOT/'tests/fixtures/offline-replay'
        report=self.memory.scan([f/'05-overview-top.png',f/'05-overview-cards.png'],client_area=True)
        self.assertEqual(report['draft']['customizations'],['']*6)
        self.assertEqual(report['draft']['item_ids'],[422,0,0,0])
        self.assertEqual(report['pages'][0]['items'][0]['method'],'template-and-upgrade-mark')
        self.assertEqual(assess_automatic(report,self.scanner.catalog),'auto-resolved')
        # A method name alone must not clear an issue.
        bad=copy.deepcopy(report)
        bad['pages'][0]['items'][0]['variant_evidence']['distance']=99999
        self.assertEqual(assess_automatic(bad,self.scanner.catalog),'needs-review')
        self.assertIn('item_ids.0',bad['automatic_check']['unresolved'])

    def test_scale_alignment_recovers_clear_real_upgrade_corners(self):
        f=ROOT/'tests/fixtures/offline-replay'
        page=self.memory.page(f/'11-overview-top.png',client_area=True)
        self.assertEqual(page['items'][0]['id'],356)
        self.assertEqual(page['items'][0]['method'],'template-and-upgrade-mark')
        self.assertEqual(page['items'][0]['variant_evidence']['rule'],'pitem-corner-v2')
        unclear=self.memory.page(f/'12-overview-top.png',client_area=True)
        self.assertEqual(unclear['items'][0]['id'],88)
        self.assertEqual(unclear['items'][0]['variant_evidence']['rule'],'pitem-corner-v2')

    def test_reference_variants_and_occluded_marks(self):
        from PIL import Image,ImageDraw
        from gkshot.recognition.item_variant import verify_item_variant
        s=self.scanner
        for item_id in (87,88,356,357,422,423):
            with Image.open(s.catalog.verified_file(f'pitem-icons/{item_id}.png')) as source:
                crop=source.convert('RGB')
            result=verify_item_variant(s,crop,s.classify_item(crop))
            self.assertEqual(result['id'],item_id)
            self.assertEqual(result['method'],'template-and-upgrade-mark')
            small=crop.resize((40,40))
            self.assertNotIn('variant_evidence',verify_item_variant(s,small,s.classify_item(small)))
            covered=crop.copy()
            ImageDraw.Draw(covered).rectangle((crop.width*.65,0,crop.width,crop.height*.3),fill='black')
            self.assertNotIn('variant_evidence',verify_item_variant(s,covered,s.classify_item(covered)))
        # Real PC crops: include cross-family errors, not only shared-art pairs.
        for ordinal,item_id in ((33,50),(34,284),(127,88),(141,402)):
            with Image.open(ROOT/f'tests/fixtures/recognition-v2/{ordinal:05d}-item-0.png') as image:
                crop=image.convert('RGB')
            result=verify_item_variant(s,crop,s.classify_item(crop))
            self.assertEqual(result['id'],item_id,(ordinal,result))
            self.assertEqual(result['variant_evidence']['rule'],'pitem-corner-v2')

    def test_star_draw_is_already_a_base_upgraded_action(self):
        cards=self.scanner.catalog.skill_cards
        self.assertIn('drawCard',cards[171]['actions'])
        self.assertNotIn('drawCard',cards[170]['actions'])


@unittest.skipUnless(importlib.util.find_spec('PIL'),'Pillow required by GUI')
class PendingLabelTests(unittest.TestCase):
    def test_labels_distinguish_items_from_customizations_and_respect_reviews(self):
        from gkshot.memory_gui import unresolved_label
        r={'status':'needs-review','report':{'automatic_check':{'unresolved':['item_ids.0','loadout-validation']}}}
        self.assertEqual(unresolved_label(r),'待解决：道具 1')
        r['report']['missing_fields']=['parameters.dance','customizations.4']
        self.assertEqual(unresolved_label(r),'待解决：道具 1、属性 Da、卡片自定义 5')
        for status,label in [('reviewed','已人工核对'),('auto-resolved','已自动识别')]:
            r['status']=status
            self.assertEqual(unresolved_label(r),label)


if __name__=='__main__':unittest.main()
