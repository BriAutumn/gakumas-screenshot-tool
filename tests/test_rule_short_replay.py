import hashlib,json,socket,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
FIXTURES=ROOT/'tests/fixtures/rule-short-text'


class RuleShortReplayTests(unittest.TestCase):
    def test_actual_tooltip_sources_without_network_or_vision_import(self):
        from gkshot.recognition.card_detail import CardDetailScanner
        from gkshot.recognition.short_text import reliable
        with patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            scanner=CardDetailScanner()
            for row in json.loads((FIXTURES/'provenance.json').read_text(encoding='utf8')):
                with self.subTest(ordinal=row['ordinal'],slot=row['slot']):
                    for key in ('tooltip','owner'):
                        self.assertEqual(hashlib.sha256((FIXTURES/row[key]).read_bytes()).hexdigest(),row[key+'_sha256'])
                    result=scanner.scan(FIXTURES/row['tooltip'],client_area=True,owner_path=FIXTURES/row['owner'])
                    self.assertEqual(result['card_id'],row['card_id'])
                    self.assertEqual(result['status'],'matched')
                    self.assertEqual(result['token'],row['token'])
                    for attempt in result['attempts']:
                        self.assertTrue(attempt['green_text_coverage']['complete'])
                        for read in attempt['reads']:
                            if 'short_text_evidence' in read:self.assertTrue(reliable(read['short_text_evidence'],read['text']))


if __name__=='__main__':unittest.main()
