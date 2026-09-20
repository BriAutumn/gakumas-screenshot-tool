import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from gkshot.capture.diagnostics import write_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_field_counts_slot_indexes_and_missing_rule_are_traceable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'memory-library').mkdir()
            report={'draft':{'card_ids':[772]*6},'pages':[{'source_path':'raw/example.png'}],
                    'evidence':{'customizations.3':[{'value':None,'sha256':'source-hash'}]}}
            with closing(sqlite3.connect(root/'memory-library/library.sqlite3')) as db:
                db.execute('CREATE TABLE memories(memory_id TEXT, report_json TEXT)')
                db.execute('INSERT INTO memories VALUES (?,?)',('one',json.dumps(report)))
                db.commit()
            batch={'run_id':'test','results':[
                {'ordinal':8,'capture_id':'copy-a','memory_id':'one','memory_status':'needs-review',
                 'status':'processed-with-issues','unresolved':['customizations.3'],'errors':[]},
                {'ordinal':9,'capture_id':'copy-b','status':'failed','errors':[{'error':'missing source'}]}]}
            (root/'results.json').write_text(json.dumps(batch))
            (root/'00008-card-4-4.json').write_text(json.dumps({'card_id':772,'status':'unsupported'}))
            result=write_diagnostics(root)
            self.assertEqual((result['processed'],result['imported'],result['ready'],result['failed']),(2,1,0,1))
            self.assertEqual(result['unresolved_fields'],{'customizations.3':1})
            self.assertEqual(result['issues'][0]['category'],'missing-detail-rule')
            self.assertEqual(result['issues'][0]['slot_index'],3)
            self.assertEqual(result['issues'][0]['card_id'],772)
            self.assertEqual(result['issues'][0]['sources'],['raw/example.png'])
            self.assertFalse(result['accuracy_measured'])
            self.assertEqual(json.loads((root/'diagnostics.json').read_text(encoding='utf8')),result)


if __name__=='__main__':unittest.main()
