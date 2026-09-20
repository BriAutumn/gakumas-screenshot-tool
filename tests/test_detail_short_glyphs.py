import copy,json,sys,hashlib
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))

class ShortGlyphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        cls.scanner=CardDetailScanner()

    def panel(self,ordinal):
        from PIL import Image
        p=ROOT/'tests/fixtures/short-glyphs'
        d=json.loads((p/f'{ordinal}-report.json').read_text(encoding='utf8'))
        im=Image.open(p/f'{ordinal}-tooltip.png').resize(tuple(d['frame']['normalized_size'])).crop(d['popup_box'])
        return im,copy.deepcopy(d['attempts'][0]['reads'])

    def test_visible_toki_is_supported_without_changing_score(self):
        from gkshot.recognition.detail_fragments import repair_fragments,fragment_reliable
        im,reads=self.panel(107);row=next(r for r in reads if r['text']=='時、');score=row['score']
        repair_fragments(self.scanner,im,reads,1)
        self.assertEqual(row['score'],score);self.assertTrue(fragment_reliable(row))

    def test_erased_toki_and_upscaled_low_resolution_refused(self):
        from PIL import ImageDraw
        from gkshot.recognition.detail_fragments import repair_fragments,fragment_reliable
        for erase in (True,False):
            im,reads=self.panel(107);row=next(r for r in reads if r['text']=='時、')
            if erase:
                xs,ys=zip(*row['box']);ImageDraw.Draw(im).rectangle((min(xs)-4,min(ys)-4,max(xs)+4,max(ys)+4),fill='white')
            repair_fragments(self.scanner,im,reads,1,source_ratio=1 if erase else .25)
            self.assertFalse(fragment_reliable(row))

    def test_missing_shou_requires_actual_character_pixels(self):
        from PIL import ImageDraw
        from gkshot.recognition.detail_fragments import repair_fragments,fragment_reliable
        im,reads=self.panel(149);repair_fragments(self.scanner,im,reads,1)
        added=[r for r in reads if r['text']=='昇'];self.assertEqual(len(added),1)
        self.assertEqual(added[0]['score'],0);self.assertTrue(fragment_reliable(added[0]))
        im,reads=self.panel(149);ImageDraw.Draw(im).rectangle((12,157,66,198),fill='white')
        repair_fragments(self.scanner,im,reads,1);self.assertFalse(any(r['text']=='昇' for r in reads))

    def test_wrong_letter_and_tampered_proof_are_refused(self):
        from PIL import ImageDraw
        from gkshot.recognition.detail_fragments import repair_fragments,fragment_reliable
        im,reads=self.panel(107);repair_fragments(self.scanner,im,reads,1)
        row=next(r for r in reads if r['text']=='時、');bad=copy.deepcopy(row)
        bad['fragment_evidence']['sha256']='wrong';self.assertFalse(fragment_reliable(bad))
        bad=copy.deepcopy(row);bad['text']='待、';self.assertFalse(fragment_reliable(bad))
        im,reads=self.panel(149);repair_fragments(self.scanner,im,reads,1,source_ratio=.25)
        self.assertFalse(any(r['text']=='昇' for r in reads))
        # A different real CJK glyph in the expected area must not be read as 昇.
        source,_=self.panel(107);im,reads=self.panel(149)
        ImageDraw.Draw(im).rectangle((12,157,66,198),fill='white')
        im.paste(source.crop((22,300,52,332)),(18,162))
        repair_fragments(self.scanner,im,reads,1)
        self.assertFalse(any(r['text']=='昇' for r in reads))

    def test_real_panels_and_held_out_failures(self):
        from unittest.mock import patch
        import socket
        path=ROOT/'tests/fixtures/short-glyphs'
        with patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            for row in json.loads((path/'provenance.json').read_text(encoding='utf8')):
                with self.subTest(ordinal=row['ordinal']):
                    for key in ('tooltip','owner'):
                        self.assertEqual(hashlib.sha256((path/row[key]).read_bytes()).hexdigest(),row[key+'_sha256'])
                    d=self.scanner.scan(path/row['tooltip'],expected_card_id=row['card_id'],client_area=True,owner_path=path/row['owner'])
                    self.assertEqual(d['status'],row['expected_status'])
                    self.assertEqual(d['token'],row['token'] if row['expected_status']=='matched' else None)
