"""Full original screenshots, held-out memories, and adversarial effect evidence."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
FIXTURES=ROOT/'tests/fixtures/customization'


class CustomizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        cls.cases=json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8'))
        cls.reports={}
        with patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')):
            cls.scanner=CardDetailScanner()
            for row in cls.cases:
                cls.reports[row['ordinal']]=cls.scanner.scan(FIXTURES/row['tooltip'],
                    expected_card_id=row['card_id'],client_area=True,owner_path=FIXTURES/row['owner'])

    def test_original_sources_and_held_out_results(self):
        for row in self.cases:
            with self.subTest(ordinal=row['ordinal'],role=row['role']):
                for source in row['files']:
                    self.assertEqual(hashlib.sha256((FIXTURES/source['file']).read_bytes()).hexdigest(),source['sha256'])
                report=self.reports[row['ordinal']]
                self.assertEqual(report['card_id'],row['card_id'])
                self.assertEqual(report['status'],row['expected_status'],report.get('reason'))
                self.assertEqual(report['token'],row['token'] if row['expected_status']=='matched' else None)
                if report['status']=='matched':
                    self.assertTrue(all(a['green_text_coverage']['complete'] for a in report['attempts']))

    def test_different_effect_values_cannot_match(self):
        from gkshot.recognition.card_detail import match_rule
        s=self.scanner
        for ordinal,old,new in [(44,'17','18'),(6,'500','550'),(127,'170','180'),(65,'2回','3回'),(81,'元気+4','元気+7')]:
            with self.subTest(ordinal=ordinal):
                report=self.reports[ordinal];reads=copy.deepcopy(report['attempts'][0]['reads'])
                from gkshot.recognition.card_detail import canonical
                for row in reads:row['text']=canonical(row['text']).replace(old,new)
                self.assertIsNone(match_rule(reads,report['card_id'],s.catalog,s.profiles)['token'])

    def test_extra_effect_and_unread_line_are_not_discarded(self):
        from gkshot.recognition.card_detail import match_rule
        s=self.scanner
        for report in self.reports.values():
            reads=report['attempts'][0]['reads']
            for extra in ({'text':'好印象+999','score':.99},{'text':'不明な追加効果','score':.1}):
                self.assertIsNone(match_rule(reads+[extra],report['card_id'],s.catalog,s.profiles)['token'])

    def test_green_addition_removed_from_ocr_stays_unresolved(self):
        from gkshot.recognition.native_resolution import native_crop
        s=self.scanner;report=self.reports[81]
        with s.memory.Image.open(FIXTURES/'081-tooltip-2.png') as source:
            crop=native_crop(source.convert('RGB'),report['frame'],report['popup_box'])[0]
        reads=s.read(crop,coordinate_scale=crop.width/(report['popup_box'][2]-report['popup_box'][0]))
        reads=[r for r in reads if '元気' not in r['text']]
        self.assertFalse(s.green_text_coverage(crop,reads,border=2)['complete'])

    def test_multiscale_keeps_failed_reads_and_requires_distinct_sizes(self):
        reports=[r for r in self.reports.values() if r.get('resolution_consensus')]
        self.assertTrue(reports)
        for report in reports:
            widths=[a['pixel_width'] for a in report['attempts']]
            self.assertGreaterEqual(max(widths),min(widths)*1.15)
            self.assertGreater(len(report['resolution_attempts']),2)
            self.assertTrue(all(a in report['resolution_attempts'] for a in report['attempts']))

    def test_cost_heart_exception_does_not_hide_green_effect(self):
        s=self.scanner;report=self.reports[230]
        image,_=s.memory.normalize(FIXTURES/'230-tooltip-6.png',client_area=True)
        crop=image.crop(report['popup_box']);reads=s.read(crop)
        rule=next(r for r in s.profiles['rules'] if r['card_id']==559)
        coverage=s.green_text_coverage(crop,reads,border=2,icon_templates=rule['green_icon_templates'])
        self.assertTrue(coverage['complete'])
        self.assertTrue(coverage['verified_nontext_icons'])
        without_effect=[r for r in reads if '元気' not in r['text']]
        self.assertFalse(s.green_text_coverage(crop,without_effect,border=2,
                         icon_templates=rule['green_icon_templates'])['complete'])
        broken=copy.deepcopy(rule['green_icon_templates']);broken[0]['sha256']='invalid'
        self.assertFalse(s.green_text_coverage(crop,reads,border=2,icon_templates=broken)['complete'])

    def test_conflicting_complete_resolution_is_not_outvoted(self):
        s=self.scanner
        report=copy.deepcopy(self.reports[44])
        report['status']='needs-detail';report['token']=None
        other=copy.deepcopy(report['attempts'][0])
        for row in other['reads']:row['text']=row['text'].replace('17','8')
        from gkshot.recognition.card_detail import match_rule
        other['match']=match_rule(other['reads'],163,s.catalog,s.profiles)
        self.assertEqual(other['match']['token'],'')
        report['native_retry']={'attempts':[other]}
        rule=next(r for r in s.profiles['rules'] if r['card_id']==163)
        with patch.object(s,'read',return_value=report['attempts'][0]['reads']),patch.object(s,'green_text_coverage',return_value={'complete':True}):
            result=s.rescaled_consensus(report,FIXTURES/'044-tooltip-6.png',rule,client_area=True)
        self.assertEqual(result['status'],'conflict')
        self.assertIsNone(result['token'])

    def test_library_rechecks_resolution_history_before_import(self):
        from gkshot.memory_library import MemoryLibrary
        s=self.scanner
        memory=s.memory.scan([FIXTURES/'044-owner-6.png'],client_area=True)
        with tempfile.TemporaryDirectory() as directory:
            library=MemoryLibrary(Path(directory))
            memory_id=library.import_report(memory)['memory_id']
            report=copy.deepcopy(self.reports[44])
            width=report['popup_box'][2]-report['popup_box'][0]
            for attempt,scale in zip(report['attempts'],(1,1.25)):
                attempt['pixel_width']=round(width*scale)
            report['resolution_consensus']={'method':'complete-panel-multiscale-v1'}
            report['resolution_attempts']=copy.deepcopy(report['attempts'])
            result=library.add_card_detail(memory_id,report,catalog=s.catalog,slot=5)
            self.assertTrue(result['applied'])
            same=copy.deepcopy(report)
            for attempt in same['attempts']:attempt['pixel_width']=width
            with self.assertRaisesRegex(ValueError,'分辨率'):
                library.add_card_detail(memory_id,same,catalog=s.catalog,slot=5)
            bad=copy.deepcopy(report)
            extra=copy.deepcopy(bad['attempts'][0])
            for row in extra['reads']:row['text']=row['text'].replace('17','8')
            bad['resolution_attempts'].append(extra)
            with self.assertRaisesRegex(ValueError,'冲突'):
                library.add_card_detail(memory_id,bad,catalog=s.catalog,slot=5)


if __name__=='__main__':unittest.main()
