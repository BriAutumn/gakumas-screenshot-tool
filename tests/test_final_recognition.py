import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
FIX=ROOT/'tests/fixtures/final-recognition'

class FinalRecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.recognition.card_detail import CardDetailScanner
        cls.scanner=CardDetailScanner()

    def test_complete_neutral_digits_with_padding(self):
        result=self.scanner.memory.read_parameter(Image.open(FIX/'parameter-910.png').convert('RGB'))
        self.assertEqual(result['value'],910);self.assertGreaterEqual(len(result['reads']),2)
        self.assertEqual(result['method'],'neutral-digit-components')
        self.assertEqual(result['padding_retry'],6)

    def test_high_model_confidence_does_not_skip_contradicting_name_and_art(self):
        from gkshot.recognition.native_resolution import retry_card,card_accepted
        old=json.loads((FIX/'card-117-old.json').read_text(encoding='utf8'))
        result=retry_card(self.scanner.memory,Image.open(FIX/'card-117.png').convert('RGB'),Image.open(FIX/'card-117-label.png').convert('RGB'),copy.deepcopy(old))
        self.assertEqual(old['id'],99);self.assertEqual(result['id'],117);self.assertTrue(card_accepted(result))
        with patch.object(self.scanner.memory,'text',return_value={'text':'アイドル宣言+','score':.99}):
            result=retry_card(self.scanner.memory,Image.open(FIX/'card-117.png').convert('RGB'),Image.open(FIX/'card-117-label.png').convert('RGB'),copy.deepcopy(old))
        self.assertFalse(card_accepted(result))

    def test_short_popup_is_detected_without_accepting_a_tiny_button(self):
        panel=Image.open(FIX/'short-tooltip-normalized.png').convert('RGB')
        canvas=Image.new('RGB',(1080,1919),(200,200,200));canvas.paste(panel,(396,975))
        self.assertEqual(len(self.scanner.boxes(canvas)),1)
        canvas=Image.new('RGB',(1080,1919),(200,200,200));ImageDraw.Draw(canvas).rectangle((396,975,920,1065),fill='white')
        self.assertEqual(self.scanner.boxes(canvas),[])

    def test_omitted_plus_requires_cross_pixels(self):
        crop=Image.open(FIX/'short-tooltip-normalized.png').convert('RGB')
        rows=json.loads((FIX/'short-tooltip-reads.json').read_text(encoding='utf8'));row=copy.deepcopy(rows[0]);row['text']='イメチェン'
        self.scanner.repair_title_plus(crop,row);self.assertEqual(row['text'],'イメチェン+')
        row=copy.deepcopy(rows[0]);row['text']='イメチェン';box=row['box'];right=max(p[0] for p in box)
        erased=crop.copy();ImageDraw.Draw(erased).rectangle((right-25,0,right+40,85),fill='white')
        self.scanner.repair_title_plus(erased,row);self.assertEqual(row['text'],'イメチェン')

if __name__=='__main__':unittest.main()
