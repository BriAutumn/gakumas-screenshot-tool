import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

spec=importlib.util.spec_from_file_location('rule_eval',Path(__file__).resolve().parents[1]/'tools/evaluate_rule_recognition.py')
evaluation=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluation)


class RuleEvaluationTests(unittest.TestCase):
    def record(self,source='one',card=1):
        return {'pages':[{'source_path':str(Path(source).resolve()),'sha256':'a'*64}],
                'draft':{'p_idol_id':1,'parameters':{'vocal':1},'card_ids':[card],
                         'item_ids':[1],'customizations':['2x1e1x2']}}

    def write(self,path,rows):
        path.mkdir()
        with closing(sqlite3.connect(path/'library.sqlite3')) as db,db:
            db.execute('create table memories(memory_id text, report_json text, review_json text)')
            db.executemany('insert into memories values(?,?,?)',[(str(i),json.dumps(report),review) for i,(report,review) in enumerate(rows)])

    def test_reference_values_never_backfill_missing_or_wrong_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);before=self.record(card=2)
            self.write(root/'candidate',[(before,None)])
            self.write(root/'reference',[(self.record(),None),(self.record('two'),None)])
            def assess(report,catalog):report['automatic_check']={'unresolved':[]};return 'auto-resolved'
            with patch.object(evaluation,'assess_automatic',side_effect=assess):
                result=evaluation.evaluate(root/'candidate',root/'reference')
            self.assertEqual(result['counts'],{'ready-disagreement':1,'missing':1})
            self.assertEqual(result['rule_only_correct_complete'],0)
            self.assertEqual(evaluation.records(root/'candidate')['0'][0],before)

    def test_visual_or_human_evidence_is_not_counted_as_rule_success(self):
        for reviewed in (False,True):
            with self.subTest(reviewed=reviewed),tempfile.TemporaryDirectory() as root:
                root=Path(root);candidate=self.record()
                if not reviewed:candidate['evidence']={'customizations.0':[{'method':'assistant-vision-field-check-v1'}]}
                self.write(root/'candidate',[(candidate,'{}' if reviewed else None)])
                self.write(root/'reference',[(self.record(),None)])
                with patch.object(evaluation,'assess_automatic',side_effect=AssertionError('must reject before assess')):
                    result=evaluation.evaluate(root/'candidate',root/'reference')
                self.assertEqual(result['counts'],{'non-rule-evidence':1})


if __name__=='__main__':unittest.main()
