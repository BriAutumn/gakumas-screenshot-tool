import copy,json,shutil,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from gkshot.catalog import Catalog
from gkshot.memory_library import MemoryLibrary,assess_automatic
from gkshot.recognition.vision_evidence import add_visual_detail,METHOD,supported
FIX=ROOT/'tests/fixtures/vision-evidence'

class VisionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.lib=MemoryLibrary(Path(self.tmp.name)/'library');shutil.copytree(FIX/'sources',self.lib.root/'sources')
        case=json.loads((FIX/'case.json').read_text(encoding='utf8'));self.row=case['decision'];self.rev=case['revision'];self.catalog=Catalog();self.rec=case['record']
        r=self.rec
        with self.lib.connect() as db:
            db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?,?,?)',(r['memory_id'],r['capture_key'],r['content_key'],r['created_at'],r['updated_at'],r['status'],json.dumps(r['report']),None))

    def add(self,row=None):
        return add_visual_detail(self.lib,self.row['memory_id'],row or self.row,revision=self.rev,catalog=self.catalog)

    def change_report(self,fn):
        r=self.lib.get(self.row['memory_id'])['report'];fn(r)
        with self.lib.connect() as db:db.execute('UPDATE memories SET report_json=?',(json.dumps(r),))

    def test_import_is_distinct_evidence_and_idempotent(self):
        original=copy.deepcopy(self.rec['report']['draft']);self.assertTrue(self.add()['applied'])
        self.assertEqual(self.add()['reason'],'already-attached')
        record=self.lib.get(self.row['memory_id']);report=record['report'];slot=self.row['slot']-1
        self.assertIsNone(record['review']);self.assertEqual(len(report['vision_details']),1)
        evidence=report['evidence'][f'customizations.{slot}'][-1]
        self.assertEqual(evidence['method'],METHOD);self.assertTrue(supported(report,evidence,slot,self.catalog))
        original['customizations'][slot]=self.row['token'];self.assertEqual(report['draft'],original)

    def test_wrong_slot_source_owner_and_revision_are_refused(self):
        for key,value in [('slot',1),('memory_id','other'),('source_sha256','bad'),('token',None),('badge',0)]:
            row=copy.deepcopy(self.row);row[key]=value
            with self.subTest(key=key),self.assertRaises((ValueError,KeyError)):self.add(row)
        row=copy.deepcopy(self.row);row['owner']['sha256']='bad'
        with self.assertRaises(ValueError):self.add(row)
        with self.assertRaises(ValueError):add_visual_detail(self.lib,self.row['memory_id'],self.row,revision='other',catalog=self.catalog)

    def test_catalog_definition_and_effect_tree_must_match(self):
        for key in ('catalog_card','selected_customizations','derived_effect_tree'):
            row=copy.deepcopy(self.row);row[key]={}
            with self.subTest(key=key),self.assertRaises(ValueError):self.add(row)

    def test_conflicts_explicit_values_and_human_review_preserved(self):
        slot=self.row['slot']-1
        self.change_report(lambda r:r['conflicts'].append(f'customizations.{slot}'))
        with self.assertRaises(ValueError):self.add()
        self.change_report(lambda r:r['conflicts'].clear())
        self.change_report(lambda r:r['draft']['customizations'].__setitem__(slot,''))
        with self.assertRaises(ValueError):self.add()
        self.change_report(lambda r:r['draft']['customizations'].__setitem__(slot,None))
        with self.lib.connect() as db:db.execute('UPDATE memories SET review_json=?',(json.dumps({'confirmed':True,'draft':self.rec['report']['draft']}),))
        with self.assertRaises(ValueError):self.add()
        self.assertTrue(self.lib.get(self.row['memory_id'])['review']['confirmed'])

    def test_archive_tamper_refused(self):
        detail=next(d for d in self.rec['report']['card_details'] if d['sha256']==self.row['source_sha256'])
        (self.lib.root/detail['archive_path']).write_bytes(b'broken')
        with self.assertRaises(ValueError):self.add()

    def test_method_name_alone_or_changed_payload_not_accepted(self):
        self.add();report=self.lib.get(self.row['memory_id'])['report'];slot=self.row['slot']-1
        ev=report['evidence'][f'customizations.{slot}'][-1]
        self.assertFalse(supported(report,{'method':METHOD,'value':self.row['token']},slot,self.catalog))
        report['vision_details'][0]['payload']['observation']['badge']=0
        self.assertFalse(supported(report,ev,slot,self.catalog))
        assess_automatic(report,self.catalog)
        self.assertIn(f'customizations.{slot}',report['automatic_check']['unresolved'])

    def test_other_memory_header_pixels_refused(self):
        from PIL import Image
        page=self.rec['report']['pages'][0];im=Image.new('RGB',(1081,1921),'black');path=self.lib.root/'wrong.png';im.save(path)
        import hashlib
        bad=dict(page,source_path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest());bad['archive_path']=self.lib.archive(bad)
        self.change_report(lambda r:r['pages'].__setitem__(0,bad))
        with self.assertRaises(ValueError):self.add()

    def test_copy_import_preserves_source_and_refuses_existing_destination(self):
        import importlib.util,contextlib,io,hashlib
        spec=importlib.util.spec_from_file_location('import_vision',ROOT/'tools/import_vision.py')
        tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
        batch=Path(self.tmp.name)/'batch.json'
        batch.write_text(json.dumps({'catalog_revision':self.rev,'decisions':[self.row]}),encoding='utf8')
        before=hashlib.sha256(self.lib.db_path.read_bytes()).hexdigest()
        dest=Path(self.tmp.name)/'copy'
        with contextlib.redirect_stdout(io.StringIO()):result=tool.apply(self.lib.root,dest,[batch])
        self.assertEqual(result['before']['unknown_customizations']-result['after']['unknown_customizations'],1)
        self.assertEqual(hashlib.sha256(self.lib.db_path.read_bytes()).hexdigest(),before)
        with self.assertRaises(ValueError):tool.apply(self.lib.root,dest,[batch])
