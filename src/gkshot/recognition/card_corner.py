"""Read a negative, one-digit cost from the original, capture-bound card corner."""
import hashlib
import re
from pathlib import Path
from gkshot.recognition.card_names import canonical_name
from gkshot.recognition.native_resolution import native_crop,card_accepted


def owner_supported(card,card_id,name):
    read=card.get('label_read',{});observed=canonical_name(read.get('text') or '')
    family=canonical_name(name).rstrip('+')
    label=(read.get('score') or 0)>=.7 and (observed==family or observed in {
        family+suffix for suffix in ('+','。','・',';',':','ト','ャ','ゃ')})
    candidate=max((c.get('score',0) for c in card.get('candidates',[]) if c.get('id')==card_id),default=0)
    return label and card.get('plus_glyph_distance',9999)<2000 and (
        (card.get('id')==card_id and card_accepted(card)) or candidate>=.2)


def valid_cost(proof,card_id,name=None):
    reads=proof.get('reads',[])
    geometry=proof.get('geometry',[])
    if len(geometry)!=2 or {g.get('threshold') for g in geometry}!={210,230}:return False
    for row in geometry:
        minus=row.get('minus',[]);digit=row.get('digit',[])
        if len(minus)!=4 or len(digit)!=4:return False
        x,y,w,h=minus;dx,dy,dw,dh=digit
        if not (w>=h*2.5 and h>0 and dh>=20 and dh>dw>0 and x+w<dx and dy+.35*dh<=y<=dy+.9*dh):return False
    return (proof.get('method')=='bound-native-cost-v1' and proof.get('card_id')==card_id
            and proof.get('unique_owner_card') is True
            and owner_supported(proof.get('owner_card',{}),card_id,name or proof.get('card_name',''))
            and type(proof.get('slot')) is int and 0<=proof['slot']<6
            and type(proof.get('value')) is int and 1<=proof['value']<=9
            and len(reads)==4 and {r.get('threshold') for r in reads}=={210,230}
            and {r.get('reader') for r in reads}=={'japanese','numeric'}
            and all(r.get('text')==f"-{proof['value']}" and r.get('score',0)>=.6 for r in reads)
            and len({(r.get('threshold'),r.get('reader')) for r in reads})==4
            and bool(proof.get('owner_sha256')))


def read_cost(memory,owner_path,card_id):
    import cv2
    np=memory.np;owner_path=Path(owner_path)
    image,frame=memory.normalize(owner_path,client_area=True)
    matches=[]
    for slot,box in memory.boxes(image):
        x1,y1,x2,y2=box;bottom=min(y2+52,image.height-memory.layout['navigation_exclusion_bottom'])
        if bottom-y2<40:continue
        label=image.crop((max(0,x1-5),y2,min(image.width,x2+15),bottom))
        read=memory.text(label)
        # The tiny label may lose its trailing '+'. Require the whole family
        # name here; separately verify the actual plus glyph and card identity.
        family=canonical_name(memory.catalog.skill_cards[card_id]['name']).rstrip('+')
        observed=canonical_name(read.get('text') or '')
        if read.get('score',0) is not None and read.get('score',0)>=.7 and (observed==family or observed in {family+suffix for suffix in ('+','。','・',';',':','ト','ャ','ゃ')}):
            matches.append((slot,box,label))
    if len(matches)!=1:return {'reason':'owner-card-not-unique','value':None}
    slot,box,label=matches[0]
    card=memory.verify_card_label(memory.card(image.crop(box)),label)
    if not owner_supported(card,card_id,memory.catalog.skill_cards[card_id]['name']):
        return {'reason':'owner-card-uncertain','value':None}
    with memory.Image.open(owner_path) as source:
        tile,bounds=native_crop(source.convert('RGB'),frame,box)
    if min(tile.size)<180:return {'reason':'insufficient-native-pixels','value':None}
    w,h=tile.size;roi=(round(w*.68),round(h*.72),round(w*.99),round(h*.96))
    crop=tile.crop(roi);a=np.asarray(crop).astype(int);reads=[];geometry=[]
    for threshold in (210,230):
        mask=((a.min(2)>=threshold)&(a.max(2)-a.min(2)<25)).astype('uint8')
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        ids=[i for i in range(1,count) if stats[i,4]>10 and stats[i,0]>0 and stats[i,1]>0
             and stats[i,0]+stats[i,2]<crop.width and stats[i,1]+stats[i,3]<crop.height]
        if len(ids)!=2:return {'reason':'cost-glyph-components','value':None}
        left,right=sorted(ids,key=lambda i:stats[i,0]);x,y,bw,bh,_=stats[left];dx,dy,dw,dh,_=stats[right]
        if not (bw>=bh*2.5 and dh>=20 and dh>dw and x+bw<dx and dy+.35*dh<=y<=dy+.9*dh):
            return {'reason':'cost-minus-or-digit-unclear','value':None}
        geometry.append({'threshold':threshold,'minus':stats[left,:4].tolist(),'digit':stats[right,:4].tolist()})
        clean=memory.Image.fromarray(np.where(np.isin(labels,ids),0,255).astype('uint8')).convert('RGB')
        text=memory.text(clean.resize((clean.width*3,clean.height*3)))
        if not text.get('text') or text.get('score',0)<.6 or not re.fullmatch(r'-[1-9]',text['text']):
            return {'reason':'cost-ocr-incomplete','value':None}
        reads.append(dict(text,threshold=threshold,reader='japanese'))
        from PIL import ImageOps
        number_crop=ImageOps.expand(clean.crop((int(x),int(min(y,dy)),int(dx+dw),int(max(y+bh,dy+dh)))),border=4,fill='white')
        number=memory.scanner.read_number(number_crop,signed=True)
        candidates=[r for r in number['reads'] if re.fullmatch(r'-[1-9]',r['text']) and r['score']>=.6]
        if not candidates:return {'reason':'cost-numeric-ocr-incomplete','value':None}
        if any(r['text']!=text['text'] for r in candidates):return {'reason':'cost-ocr-conflict','value':None}
        reads.append({**max(candidates,key=lambda r:r['score']),'threshold':threshold,'reader':'numeric'})
    proof={'method':'bound-native-cost-v1','card_id':card_id,'card_name':memory.catalog.skill_cards[card_id]['name'],'slot':slot,'unique_owner_card':True,
           'owner_card':card,'owner_sha256':hashlib.sha256(owner_path.read_bytes()).hexdigest(),
           'owner_path':str(owner_path.resolve()),'tile_bounds':bounds,'roi':list(roi),'geometry':geometry,
           'reads':reads,'value':-int(reads[0]['text'])}
    return proof if valid_cost(proof,card_id) else {'reason':'cost-ocr-conflict','value':None,'reads':reads}
