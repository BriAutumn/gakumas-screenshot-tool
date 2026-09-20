import copy,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
import test_vision_evidence as base_fixture
from gkshot.recognition import vision_fields as vf
from gkshot.memory_library import assess_automatic

class VisionFieldsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base_fixture.VisionEvidenceTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.lib=self.fixture.lib;self.catalog=self.fixture.catalog;self.rev=self.fixture.rev
        self.row=copy.deepcopy(self.fixture.row)
        self.row.update(field=f"customizations.{self.row['slot']-1}",old_value=None,value=self.row['token'],box=self.row['crop_bounds'],observation='直接读取同一原图详情与卡图。')

    def add(self,row=None):
        return vf.add(self.lib,self.row['memory_id'],row or self.row,revision=self.rev,catalog=self.catalog)

    def test_preserves_other_fields_and_attaches_independent_evidence(self):
        before=copy.deepcopy(self.fixture.rec['report']['draft'])
        self.assertTrue(self.add()['applied']);self.assertEqual(self.add()['reason'],'already-attached')
        r=self.lib.get(self.row['memory_id']);self.assertIsNone(r['review'])
        before['customizations'][self.row['slot']-1]=self.row['token'];self.assertEqual(r['report']['draft'],before)
        ev=r['report']['evidence'][self.row['field']][-1]
        self.assertTrue(vf.supported(r['report'],ev,self.row['field'],self.catalog))
        r['report']['vision_fields'][0]['payload']['observation']['badge']=99
        self.assertFalse(vf.supported(r['report'],ev,self.row['field'],self.catalog))

    def test_wrong_source_region_slot_and_old_value_rejected(self):
        for key,value in [('source_sha256','wrong'),('box',[0,0,100,100]),('field','customizations.0'),('old_value','already-known')]:
            row=copy.deepcopy(self.row);row[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.add(row)

    def test_no_overwrite_of_resolved_conflicting_missing_or_human_fields(self):
        for marker in ('conflicts','missing_fields'):
            self.fixture.change_report(lambda r:r[marker].append(self.row['field']))
            with self.assertRaises(ValueError):self.add()
            self.fixture.change_report(lambda r:r[marker].clear())
        with self.lib.connect() as db:db.execute('UPDATE memories SET review_json=?',(json.dumps({'confirmed':True}),))
        with self.assertRaises(ValueError):self.add()

    def test_ocr_title_failure_can_be_superseded_without_changing_ocr_history(self):
        self.fixture.change_report(lambda r:r['card_details'][0].update(status='conflict'))
        original=copy.deepcopy(self.lib.get(self.row['memory_id'])['report']['card_details'])
        self.add();report=self.lib.get(self.row['memory_id'])['report']
        self.assertEqual(report['card_details'],original)
        assess_automatic(report,self.catalog);self.assertNotIn(self.row['field'],report['automatic_check']['unresolved'])

    def test_parameter_can_be_confirmed_but_resolved_value_cannot_be_overwritten(self):
        from PIL import Image
        from gkshot.recognition.native_resolution import native_crop
        page=self.fixture.rec['report']['pages'][0]
        layout=json.loads((ROOT/'configs/layouts/memory-detail-pc-v1.json').read_text(encoding='utf8'))
        with Image.open(self.lib.source_path(page)) as im:_,bounds=native_crop(im,page['frame'],layout['parameters']['vocal'])
        value=self.fixture.rec['report']['draft']['parameters']['vocal']
        row=dict(memory_id=self.row['memory_id'],field='parameters.vocal',old_value=value,value=value,visible_number=str(value),observation='可见完整数字。',source_sha256=page['sha256'],box=bounds)
        with self.assertRaises(ValueError):self.add(row)
        self.fixture.change_report(lambda r:r['pages'][0]['number_reads'].__setitem__('vocal',{'reads':[]}))
        self.assertTrue(self.add(row)['applied'])
        report=self.lib.get(self.row['memory_id'])['report'];assess_automatic(report,self.catalog)
        self.assertNotIn('parameters.vocal',report['automatic_check']['unresolved'])

    def test_new_detail_requires_matching_before_image_and_identity(self):
        source=copy.deepcopy(next(d for d in self.fixture.rec['report']['card_details'] if d['sha256']==self.row['source_sha256']))
        # Use archived fixture paths for replaying an unbound original report.
        source['source_path']=str(self.lib.source_path(source))
        source['ownership_evidence']['source_path']=str(self.lib.source_path(source['ownership_evidence']))
        self.fixture.change_report(lambda r:r.__setitem__('card_details',[]))
        row=copy.deepcopy(self.row);row['additional_detail']=source
        bad=copy.deepcopy(row);bad['additional_detail']['identity']['power']+=1
        with self.assertRaises(ValueError):self.add(bad)
        self.assertTrue(self.add(row)['applied'])
        report=self.lib.get(self.row['memory_id'])['report']
        self.assertEqual(len(report['vision_source_details']),1)
        self.assertEqual(self.add(row)['reason'],'already-attached')

if __name__=='__main__':unittest.main()
