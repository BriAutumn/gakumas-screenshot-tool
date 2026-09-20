"""Auditable short-line CTC evidence without changing legacy OCR scores.

RapidOCR 1.2.3 adds a zero to the mean of emitted-character probabilities.
Keep that score for compatibility; separately inspect the emitted tokens and
require two high-confidence reads of the same actual short line.
"""
import re
import unicodedata
import math


def canonical(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def token_read(ocr, pixels):
    import numpy as np
    rec=ocr.text_recognizer
    decoder=rec.postprocess_op
    image=ocr.load_img(np.asarray(pixels))
    normalized=rec.resize_norm_img(image,image.shape[1]/image.shape[0])
    preds=rec.session(normalized[np.newaxis].astype(np.float32))[0]
    indices=preds.argmax(axis=2)[0];probabilities=preds.max(axis=2)[0]
    ignored=set(decoder.get_ignored_tokens());chars=[];scores=[]
    for i,index in enumerate(indices):
        if int(index) in ignored or (i and index==indices[i-1]):continue
        chars.append(decoder.character[int(index)]);scores.append(float(probabilities[i]))
    # Decode through the installed implementation as well. Unknown decoder
    # semantics must not produce new acceptance evidence.
    legacy=decoder(preds)[0]
    if legacy[0]!=''.join(chars):return None
    return {'text':legacy[0],'legacy_score':float(legacy[1]),'characters':chars,'probabilities':scores}


def reliable(proof,text):
    reads=proof.get('reads',[])
    expected=canonical(text).rstrip('、・')
    if (proof.get('method')!='short-ctc-two-scales-v1' or not 1<=len(expected)<=6
            or len(reads)!=2 or {r.get('height') for r in reads}!={48,64}):return False
    for read in reads:
        chars=read.get('characters',[]);scores=read.get('probabilities',[])
        if (not chars or len(chars)!=len(scores)
                or any(not isinstance(s,(int,float)) or not math.isfinite(s) or not 0<=s<=1 for s in scores)):return False
        # A Japanese comma at a wrap boundary carries no numeric semantics.
        # Preserve it in raw evidence but do not let its faint dot hide two
        # independently clear characters. No digit/operator is removable.
        lexical=[(c,s) for c,s in zip(chars,scores)]
        while lexical and lexical[-1][0] in ('、','・'):lexical.pop()
        if not lexical:return False
        content=''.join(c for c,_ in lexical);scores=[s for _,s in lexical]
        if (canonical(content)!=expected
                or canonical(read.get('text','')).rstrip('、・')!=expected
                or min(scores)<.97 or sum(scores)/len(scores)<.985):return False
    return True


def consensus(scanner,crop,text):
    from PIL import Image,ImageOps
    reads=[]
    for height in (48,64):
        pixels=crop.resize((max(1,round(crop.width*height/crop.height)),height),Image.Resampling.LANCZOS)
        read=token_read(scanner.ocr,ImageOps.expand(pixels,border=12,fill='white'))
        if read is None:return None
        reads.append(dict(read,height=height))
    proof={'method':'short-ctc-two-scales-v1','reads':reads}
    return proof if reliable(proof,text) else None


def repair(scanner,crop,reads,scale):
    """Reread observed short rows and isolated wrap-line glyphs, not guesses."""
    import cv2
    np=scanner.memory.np
    for row in reads:
        if canonical(row['text'])=='重複不司':
            points=np.asarray(row['box']);left,top=points.min(0);right,bottom=points.max(0)
            bounds=(max(0,int(left)-2),max(0,int(top)-2),min(crop.width,int(right)+3),min(crop.height,int(bottom)+3))
            proof=consensus(scanner,crop.crop(bounds),'重複不可')
            if proof:
                row.update(original_read={'text':row['text'],'score':row['score']},text='重複不可',short_text_evidence=proof)
        if row['score']>=.7 or not 1<=len(canonical(row['text']))<=6:continue
        points=np.asarray(row['box']);left,top=points.min(0);right,bottom=points.max(0)
        bounds=(max(0,int(left)-2),max(0,int(top)-2),min(crop.width,int(right)+3),min(crop.height,int(bottom)+3))
        proof=consensus(scanner,crop.crop(bounds),row['text'])
        if proof:row['short_text_evidence']=proof
    # A one-character continuation at the left edge is often never detected.
    # Use real neutral/green ink and exclude every existing OCR rectangle.
    colors=np.asarray(crop).astype(int);r,g,b=colors.transpose(2,0,1)
    ink=(((colors.max(2)<160)&(np.ptp(colors,axis=2)<25))|((g>100)&(g-r>50)&(g-b>35)&(r<150))).astype('uint8')
    ink[:round(85*scale)]=0;ink[:,:round(12*scale)]=0;ink[:,round(66*scale):]=0
    for row in reads:cv2.fillPoly(ink,[np.rint(row['box']).astype('int32')],0)
    ys=np.flatnonzero(ink.any(axis=1))
    groups=np.split(ys,np.flatnonzero(np.diff(ys)>5*scale)+1) if len(ys) else []
    added=[]
    for band in groups:
        top,bottom=int(band[0]),int(band[-1])+1
        xs=np.flatnonzero(ink[top:bottom].any(axis=0))
        left,right=int(xs[0]),int(xs[-1])+1
        if not (18*scale<=bottom-top<=40*scale and .55<(right-left)/(bottom-top)<2):continue
        bounds=(max(0,left-3),max(0,top-3),min(crop.width,right+3),min(crop.height,bottom+3))
        line=crop.crop(bounds);read=token_read(scanner.ocr,line)
        if read is None or len(canonical(read['text']).rstrip('、・'))!=1:continue
        proof=consensus(scanner,line,read['text'])
        if proof:
            x1,y1,x2,y2=bounds
            added.append({'text':read['text'],'score':read['legacy_score'],
                'box':[[x1,y1],[x2,y1],[x2,y2],[x1,y2]],'short_text_evidence':proof})
    from .text_order import order_text_rows
    return order_text_rows(reads + added)
