import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from gkshot.recognition.short_text import reliable,token_read


class ShortTextTests(unittest.TestCase):
    def proof(self,text,probabilities):
        return {'method':'short-ctc-two-scales-v1','reads':[
            {'text':text,'characters':list(text),'probabilities':probabilities,'height':h}
            for h in (48,64)]}

    def test_two_actual_reads_required_without_legacy_score_inflation(self):
        p=self.proof('始時',[.995,.99])
        self.assertTrue(reliable(p,'始時'))
        p['reads'].pop()
        self.assertFalse(reliable(p,'始時'))

    def test_uncertain_numeric_operator_and_mismatching_read_refused(self):
        self.assertFalse(reliable(self.proof('+1',[.96,.999]),'+1'))
        self.assertFalse(reliable(self.proof('+2',[.999,.999]),'+1'))
        p=self.proof('+1',[.999,.999]);p['reads'][1]['text']='-1'
        self.assertFalse(reliable(p,'+1'))
        p=self.proof('+1',[.999,.999]);p['reads'][1]['height']=48
        self.assertFalse(reliable(p,'+1'))

    def test_optional_wrap_comma_never_removes_a_number(self):
        self.assertTrue(reliable(self.proof('始時、',[.995,.99,.6]),'始時'))
        self.assertFalse(reliable(self.proof('+12',[.999,.999,.6]),'+1'))
        for value in (float('nan'),float('inf'),1.1,-1):
            self.assertFalse(reliable(self.proof('昇',[value]),'昇'))

    def test_ctc_blank_and_duplicate_decode_preserves_real_token_probabilities(self):
        import numpy as np
        from types import SimpleNamespace
        class Decoder:
            character=['blank','始','時']
            def get_ignored_tokens(self):return [0]
            def __call__(self,preds):return [('始時',(.99+.98)/3)]
        p=np.asarray([[[.005,.99,.005],[.005,.99,.005],[.99,.005,.005],[.01,.01,.98]]],dtype=np.float32)
        rec=SimpleNamespace(postprocess_op=Decoder(),resize_norm_img=lambda image,ratio:image,session=lambda image:[p])
        result=token_read(SimpleNamespace(text_recognizer=rec,load_img=lambda image:image),np.zeros((8,20,3),dtype=np.uint8))
        self.assertEqual(result['characters'],['始','時'])
        self.assertAlmostEqual(result['legacy_score'],(.99+.98)/3)
        self.assertAlmostEqual(result['probabilities'][1],.98,places=6)


if __name__=='__main__':unittest.main()
