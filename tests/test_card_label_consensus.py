import copy
import json
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


class CardLabelConsensusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.memory import MemoryScanner
        cls.memory=MemoryScanner()
        folder=ROOT/'tests/fixtures/card-identity'
        images=json.loads((folder/'provenance.json').read_text(encoding='utf8'))['260']
        cls.report=cls.memory.scan([folder/i['path'] for i in images],client_area=True)

    def test_two_visible_exact_labels_and_close_templates_resolve_a_conflicting_model(self):
        from gkshot.recognition.native_resolution import card_accepted
        self.assertEqual(self.report['draft']['card_ids'][1],29)
        for p in self.report['pages']:
            card=next(c for c in p['cards'] if c['slot']==1)
            self.assertEqual(card['method'],'multi-page-card-label')
            self.assertTrue(card_accepted(card))
        self.assertIsNone(self.report['draft']['customizations'][1])

    def test_one_source_disagreement_low_score_or_unclear_mark_stay_unknown(self):
        from gkshot.recognition.card_label_consensus import refine
        from gkshot.recognition.native_resolution import card_accepted
        for mode in ('single','duplicate','different-name','low-score','unclear-mark','distant-template'):
            pages=copy.deepcopy(self.report['pages'])
            for page in pages:
                c=next(c for c in page['cards'] if c['slot']==1)
                c.update(c.pop('identity_before_consensus'));c.pop('label_consensus')
            first=next(c for c in pages[0]['cards'] if c['slot']==1)
            if mode=='single':pages=pages[:1]
            if mode=='duplicate':pages[1]['sha256']=pages[0]['sha256']
            if mode=='different-name':first['label_read']['text']='トークタイム+'
            if mode=='low-score':first['label_read']['score']=.79
            if mode=='unclear-mark':first['plus_glyph_distance']=4000
            if mode=='distant-template':
                for candidate in first['template_candidates']:
                    if candidate['id']==29:candidate['distance']=3500
            refine(pages,self.memory.catalog)
            self.assertFalse(any(card_accepted(c) for p in pages for c in p['cards'] if c['slot']==1),mode)


if __name__=='__main__':unittest.main()
