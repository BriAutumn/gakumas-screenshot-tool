import copy,hashlib,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
FIX=ROOT/'tests/fixtures/popup-boundary'

class PopupBoundaryTests(unittest.TestCase):
    def helper(self):
        import numpy as np
        from PIL import Image
        from gkshot.recognition.card_detail import CardDetailScanner
        scanner=CardDetailScanner.__new__(CardDetailScanner)
        scanner.memory=SimpleNamespace(np=np,Image=Image)
        return scanner

    def test_exclusions_recomputed_from_original_pixels(self):
        from gkshot.recognition.card_detail import verify_popup_boundary_evidence
        rules=json.loads((ROOT/'configs/recognition/card-detail-rules.json').read_text(encoding='utf8'))['rules']
        for n in (190,390):
            d=json.loads((FIX/f'{n}-report.json').read_text(encoding='utf8'))
            d['source_path']=str(FIX/f'{n}-tooltip.png')
            rule=next(r for r in rules if r['card_id']==d['card_id'])
            a=next(a for a in d['attempts'] if a['green_text_coverage'].get('outside_popup'))
            self.assertTrue(verify_popup_boundary_evidence(d,a,rule))
            self.assertFalse(verify_popup_boundary_evidence(d,a,dict(rule,verify_popup_boundary=False)))
            bad=copy.deepcopy(a);bad['green_text_coverage']['outside_popup']['hull'][0][0]+=1
            self.assertFalse(verify_popup_boundary_evidence(d,bad,rule))
            bad=copy.deepcopy(a);bad['green_text_coverage']['outside_popup']['components'][0][1]+=1
            self.assertFalse(verify_popup_boundary_evidence(d,bad,rule))
            self.assertFalse(verify_popup_boundary_evidence(dict(d,sha256='wrong'),a,rule))

    def test_interior_green_and_uncertain_panel_shape_remain_unread(self):
        from PIL import Image,ImageDraw
        s=self.helper()
        for rounded,position in [(True,(100,100,103,103)),(True,(0,100,1,103)),(False,(0,1,1,4))]:
            im=Image.new('RGB',(524,400),'gray');draw=ImageDraw.Draw(im)
            if rounded:draw.rounded_rectangle((0,0,523,399),radius=45,fill='white')
            else:draw.rectangle((0,0,523,399),fill='white')
            draw.rectangle(position,fill=(0,200,0))
            self.assertFalse(s.green_text_coverage(im,[],verify_popup_boundary=True)['complete'])
        im=Image.new('RGB',(524,400),'gray');draw=ImageDraw.Draw(im)
        draw.rounded_rectangle((0,0,523,399),radius=45,fill='white');draw.rectangle((0,10,1,12),fill=(0,200,0))
        self.assertFalse(s.green_text_coverage(im,[])['complete'])
        self.assertTrue(s.green_text_coverage(im,[],verify_popup_boundary=True)['complete'])

    def test_gou_requires_pixels_and_keeps_original_confidence(self):
        from PIL import Image,ImageDraw
        from gkshot.recognition.detail_fragments import repair_fragments,fragment_reliable
        d=json.loads((FIX/'37-report.json').read_text(encoding='utf8'))
        panel=Image.open(FIX/'37-tooltip.png').resize(tuple(d['frame']['normalized_size'])).crop(d['popup_box'])
        original=next(r for r in d['attempts'][0]['reads'] if r['text']=='合、')
        for erased,ratio in [(False,1),(True,1),(False,.25),('different-glyph',1)]:
            im=panel.copy();row=copy.deepcopy(original);row.pop('fragment_evidence',None)
            if erased:
                xs,ys=zip(*row['box']);ImageDraw.Draw(im).rectangle((min(xs)-4,min(ys)-4,max(xs)+4,max(ys)+4),fill='white')
                if erased=='different-glyph':
                    # Real 時 pixels must not substantiate an OCR guess of 合.
                    src=ROOT/'tests/fixtures/short-glyphs'
                    other=json.loads((src/'107-report.json').read_text(encoding='utf8'))
                    glyph=Image.open(src/'107-tooltip.png').resize(tuple(other['frame']['normalized_size'])).crop(other['popup_box']).crop((22,300,52,332))
                    im.paste(glyph,(round(min(xs)),round(min(ys))))
            repair_fragments(self.helper(),im,[row],1,source_ratio=ratio)
            self.assertEqual(row['score'],original['score'])
            self.assertEqual(fragment_reliable(row),not erased and ratio==1)
            if fragment_reliable(row):
                bad=copy.deepcopy(row);bad['text']='台、';self.assertFalse(fragment_reliable(bad))
                bad=copy.deepcopy(row);bad['fragment_evidence']['sha256']='wrong';self.assertFalse(fragment_reliable(bad))

    def test_held_out_generated_rule_panels_and_failed_state(self):
        import socket
        from gkshot.recognition.card_detail import CardDetailScanner
        with patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            scanner=CardDetailScanner()
            for n,expected in [(143,'80x1'),(193,'80x1'),(37,None)]:
                d=json.loads((FIX/f'{n}-report.json').read_text(encoding='utf8'))
                self.assertEqual(hashlib.sha256((FIX/f'{n}-tooltip.png').read_bytes()).hexdigest(),d['sha256'])
                result=scanner.scan(FIX/f'{n}-tooltip.png',expected_card_id=299,client_area=True,owner_path=FIX/f'{n}-owner.png')
                self.assertEqual(result['token'],expected)
                self.assertEqual(result['status'],'matched' if expected else 'needs-detail')
