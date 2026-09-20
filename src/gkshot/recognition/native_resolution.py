"""Native-pixel fallback. Reference PNGs are never loaded through 48/64px caches."""
import copy
import hashlib
import math


def native_crop(raw, frame, box):
    sx=(frame['right']-frame['left'])/frame['normalized_size'][0]
    sy=(raw.height-frame['top'])/frame['normalized_size'][1]
    bounds=[math.floor(frame['left']+box[0]*sx),math.floor(frame['top']+box[1]*sy),
            math.ceil(frame['left']+box[2]*sx),math.ceil(frame['top']+box[3]*sy)]
    if bounds[0]<0 or bounds[1]<0 or bounds[2]>raw.width or bounds[3]>raw.height:
        raise ValueError('原始分辨率裁剪区域不完整。')
    return raw.crop(bounds),bounds


def item_accepted(record):
    if record.get('method')=='empty-slot' and record.get('id')==0:return True
    if record.get('method') in ('template-and-upgrade-mark','native-template-and-upgrade-mark'):
        return variant_accepted(record.get('variant_evidence',{}),record.get('id'))
    if record.get('method')=='native-single-template':
        return singleton_accepted(record.get('native_retry',{}),record.get('id'))
    c=record.get('candidates',[])
    return bool(len(c)>=2 and c[0]['id']==record.get('id') and c[0]['distance']<1200
                and c[0]['distance']<c[1]['distance']*.9)


def variant_accepted(e,entity_id):
    allowed=('pitem-corner-v1','pitem-corner-v2','native-pitem-v1','native-card-v1')
    return (e.get('rule') in allowed and e.get('id')==entity_id and entity_id is not None
            and e.get('pixel_count',0)>=25 and e.get('alignment_distance',99999)<1200
            and e.get('distance',99999)<2000 and e.get('distance',99999)<e.get('alternative_distance',0)*.35
            and e.get('family_distance',99999)<1200 and e.get('family_distance',99999)<e.get('other_family_distance',0)*.7
            and (not e['rule'].startswith('native-') or (e.get('native_pixels') is True and min(e.get('source_size',[0,0]))>=80)))


def singleton_accepted(e,entity_id):
    return (e.get('rule')=='native-single-pitem-v1' and e.get('id')==entity_id and entity_id is not None
            and e.get('candidate_ids')==[entity_id] and e.get('native_pixels') is True
            and min(e.get('source_size',[0,0]))>=80 and e.get('full_template_distance',99999)<1200
            and e.get('family_distance',99999)<1200 and e.get('family_distance',99999)<e.get('other_family_distance',0)*.7)


def card_accepted(record):
    entity_id=record.get('id')
    if entity_id is None:return False
    if record.get('method')=='multi-page-card-label':
        from gkshot.recognition.card_label_consensus import accepted
        return accepted(record)
    if record.get('method')=='bound-tooltip-identity':
        from gkshot.recognition.bound_card_identity import accepted
        return accepted(record)
    if record.get('method')=='native-card-and-label':
        return variant_accepted(record.get('native_retry',{}),entity_id) and record.get('native_label_agrees') is True
    if record.get('native_disagreement_retry'):return False
    return (any(c.get('id')==entity_id and c.get('score',0)>=.95 for c in record.get('candidates',[]))
            or record.get('method')=='onnx-with-template-correction'
            or (record.get('method')=='label-and-template' and record.get('label_match',{}).get('art_agrees')
                and record.get('label_match',{}).get('similarity',0)>=.88 and record.get('label_read',{}).get('score',0)>=.7))


class NativeTemplates:
    def __init__(self,scanner,kind):
        import cv2
        self.cv2=cv2;self.np=scanner.np;self.Image=scanner.Image;self.catalog=scanner.catalog
        self.kind=kind
        catalog=self.catalog.p_items if kind=='item' else self.catalog.skill_cards
        folder='pitem-icons' if kind=='item' else 'card-icons'
        self.entries={};self.families={};self.cache={}
        ids=scanner.item_ids if kind=='item' else [int(n.split('/')[-1][:-4]) for n in self.catalog.manifest['files'] if n.startswith(folder+'/')]
        for entity_id in ids:
            row=catalog[entity_id]
            key=(row['name'].removesuffix('+'),row.get('pIdolId'),row.get('mode'),row.get('plan'))
            with self.Image.open(self.catalog.verified_file(f'{folder}/{entity_id}.png')) as im:
                pixels=im.convert('RGB').copy()
            self.entries[entity_id]=(row,pixels)
            self.families.setdefault(key,[]).append(entity_id)

    def fit(self,crop):
        """Search every art family using equally bounded scale/translation fits."""
        np,cv2=self.np,self.cv2
        key=hashlib.sha256(crop.tobytes()+str(crop.size).encode()).hexdigest()
        if key in self.cache:return copy.deepcopy(self.cache[key])
        if min(crop.size)<80 or max(crop.size)/min(crop.size)>1.12:
            return {'accepted':False,'reason':'insufficient-native-pixels','source_size':list(crop.size)}
        pixels=np.asarray(crop.convert('RGB'),dtype=np.float32)
        h,w=pixels.shape[:2];pad=math.ceil(max(w,h)*.16)
        search=cv2.copyMakeBorder(pixels,pad,pad,pad,pad,cv2.BORDER_CONSTANT,value=(255,255,255))
        fits=[]
        for family,ids in self.families.items():
            # Keep all art alternatives; a singleton still competes with paired families.
            representative=min(ids,key=lambda i:self.entries[i][0]['upgraded'])
            image=self.entries[representative][1]
            best=None
            for scale in (.88,.92,.96,1.,1.04,1.08,1.12):
                tw,th=round(w*scale),round(h*scale)
                template=np.asarray(image.resize((tw,th),self.Image.Resampling.LANCZOS),dtype=np.float32)
                mask=np.zeros((th,tw),dtype=np.uint8)
                if self.kind=='item':
                    mask[round(th*.15):round(th*.85),round(tw*.15):round(tw*.85)]=1
                    mask[:round(th*.35),round(tw*.63):]=0
                else:
                    mask[round(th*.22):round(th*.65),round(tw*.22):round(tw*.70)]=1
                cx,cy=pad+(w-tw)//2,pad+(h-th)//2
                radius=max(2,round(min(w,h)*.045))
                x1,x2=max(0,cx-radius),min(search.shape[1]-tw+1,cx+radius+1)
                y1,y2=max(0,cy-radius),min(search.shape[0]-th+1,cy+radius+1)
                # Evaluate exactly the same pixels and candidate offsets. The
                # unused masked borders and never-inspected translations need
                # not participate in the expensive correlation calculation.
                my,mx=np.nonzero(mask)
                ax,bx=int(mx.min()),int(mx.max())+1
                ay,by=int(my.min()),int(my.max())+1
                patch=template[ay:by,ax:bx]
                active=mask[ay:by,ax:bx]
                window=search[y1+ay:y2-1+by,x1+ax:x2-1+bx]
                region=cv2.matchTemplate(window,patch,cv2.TM_SQDIFF,
                                         mask=None if active.all() else active)/(float(mask.sum())*3)
                _,_,loc,_=cv2.minMaxLoc(region)
                x,y=x1+loc[0],y1+loc[1]
                value=max(0.,float(region[loc[1],loc[0]]))
                if best is None or value<best[0]:best=(value,tw,th,x-pad,y-pad)
            fits.append((best[0],family,ids,best))
        fits.sort(key=lambda r:r[0])
        distance,family,ids,(_,tw,th,x,y)=fits[0]
        other=fits[1][0]
        result={'accepted':False,'native_pixels':True,'source_size':[w,h],
                'family_distance':round(distance,3),'other_family_distance':round(other,3),
                'candidate_ids':ids,'template_sizes':{str(i):list(self.entries[i][1].size) for i in ids},
                'fit_size':[tw,th],'offset':[x,y],'searched_families':len(fits)}
        if distance>=1200 or distance>=other*.7:
            result['reason']='ambiguous-native-art'
        elif len(ids)==1 and self.kind=='item':
            template=np.asarray(self.entries[ids[0]][1].resize((tw,th),self.Image.Resampling.LANCZOS),dtype=np.float32)
            yy,xx=np.mgrid[round(th*.1):round(th*.9),round(tw*.1):round(tw*.9)]
            if (xx+x<0).any() or (xx+x>=w).any() or (yy+y<0).any() or (yy+y>=h).any():
                result['reason']='clipped-native-template'
            else:
                full=float(((template[yy,xx]-pixels[yy+y,xx+x])**2).mean())
                result.update(id=ids[0],rule='native-single-pitem-v1',full_template_distance=round(full,3))
                result['accepted']=singleton_accepted(result,ids[0])
                result['reason']='matched' if result['accepted'] else 'unclear-native-template'
        elif len(ids)!=2 or self.entries[ids[0]][0]['upgraded']==self.entries[ids[1]][0]['upgraded']:
            result['reason']='no-unique-variant-pair'
        else:
            pair=[np.asarray(self.entries[i][1].resize((tw,th),self.Image.Resampling.LANCZOS),dtype=np.float32) for i in ids]
            mark=np.max(abs(pair[0]-pair[1]),axis=2)>70
            region=np.zeros(mark.shape,dtype=bool)
            if self.kind=='item':region[:round(th*.35),round(tw*.63):]=True
            else:region[round(th*.30):round(th*.70),round(tw*.66):round(tw*.99)]=True
            mark &= region
            yy,xx=np.nonzero(mark)
            if len(xx)<25 or (xx+x<0).any() or (xx+x>=w).any() or (yy+y<0).any() or (yy+y>=h).any():
                result['reason']='missing-or-clipped-variant-mark'
            else:
                observed=pixels[yy+y,xx+x]
                errors=[float(((template[yy,xx]-observed)**2).mean()) for template in pair]
                winner=int(np.argmin(errors));win,lose=errors[winner],errors[1-winner]
                result.update(id=ids[winner],rule='native-pitem-v1' if self.kind=='item' else 'native-card-v1',
                    alignment_distance=round(distance,3),pixel_count=len(xx),distance=round(win,3),alternative_distance=round(lose,3))
                result['accepted']=variant_accepted(result,ids[winner])
                result['reason']='matched' if result['accepted'] else 'unclear-native-variant-mark'
        if len(self.cache)>=256:self.cache.pop(next(iter(self.cache)))
        self.cache[key]=copy.deepcopy(result)
        return result


def retry_item(scanner,crop,record):
    if item_accepted(record):return record
    if not hasattr(scanner,'_native_items'):scanner._native_items=NativeTemplates(scanner,'item')
    evidence=scanner._native_items.fit(crop)
    record['native_retry']=evidence
    if evidence['accepted']:
        if evidence['rule']=='native-single-pitem-v1':record.update(id=evidence['id'],method='native-single-template')
        else:record.update(id=evidence['id'],method='native-template-and-upgrade-mark',variant_evidence=evidence)
    return record


def retry_card(memory,crop,label,record):
    from gkshot.recognition.card_names import canonical_name
    read=record.get('label_read') or {};templates=record.get('template_candidates') or []
    def family(entity):return canonical_name(memory.catalog.skill_cards[entity]['name']).rstrip('+')
    conflict=bool(templates and record.get('id') in memory.catalog.skill_cards
        and (read.get('score') or 0)>=.55 and read.get('text')
        and family(templates[0]['id'])!=family(record['id'])
        and canonical_name(read['text']).rstrip('+')==family(templates[0]['id']))
    if card_accepted(record) and not conflict:return record
    if conflict:record['native_disagreement_retry']=True
    if not hasattr(memory,'_native_cards'):memory._native_cards=NativeTemplates(memory.scanner,'card')
    evidence=memory._native_cards.fit(crop)
    record['native_retry']=evidence
    read=memory.text(label) if label is not None else {'text':None,'score':None}
    record['native_label_read']=read
    if not evidence['accepted'] or not read['text'] or read['score']<.7:return record
    from gkshot.recognition.card_names import art_backed_name_matches
    name=memory.catalog.skill_cards[evidence['id']]['name']
    # Independent full name: artwork alone must not resolve a conflicting label.
    if not art_backed_name_matches(read['text'],name,memory.catalog):return record
    previous=record.get('label_read',{})
    if previous.get('score',0) is not None and previous.get('score',0)>=.7 and previous.get('text'):
        if not art_backed_name_matches(previous['text'],name,memory.catalog):
            return record
    record.update(id=evidence['id'],method='native-card-and-label',native_label_agrees=True)
    return record
