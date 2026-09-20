"""Read small visible text that the panel detector misses; never supply absent text."""
import hashlib
import re
from gkshot.catalog import PROJECT_ROOT


def fragment_reliable(row):
    proof=row.get('fragment_evidence',{})
    if proof.get('method')=='isolated-shou-template-v1':
        path=PROJECT_ROOT/'configs/recognition/glyphs/isolated-shou.png'
        return (row.get('text')=='昇' and proof.get('iou',0)>=.75
                and proof.get('coverage',0)>=.98 and proof.get('distance',999)<=1
                and proof.get('native_height',0)>=18
                and proof.get('sha256')==hashlib.sha256(path.read_bytes()).hexdigest())
    short_glyphs={'isolated-toki-template-v1':('時','toki'),'isolated-gou-template-v1':('合','gou')}
    if proof.get('method') in short_glyphs:
        char,stem=short_glyphs[proof['method']]
        path=PROJECT_ROOT/f'configs/recognition/glyphs/isolated-{stem}.png'
        return (row.get('text') in (char,char+'、') and row.get('score',0)>=.6
                and proof.get('iou',0)>=.75 and proof.get('coverage',0)>=.98
                and proof.get('distance',999)<=1 and proof.get('native_height',0)>=18
                and proof.get('sha256')==hashlib.sha256(path.read_bytes()).hexdigest())
    if proof.get('method')=='isolated-ku-template-v2':
        path=PROJECT_ROOT/'configs/recognition/glyphs/isolated-ku.png'
        return (row.get('text')=='く' and proof.get('iou',0)>=.7 and proof.get('coverage',0)>=.98
                and proof.get('distance',999)<=1 and proof.get('native_height',0)>=18
                and proof.get('sha256')==hashlib.sha256(path.read_bytes()).hexdigest())
    reads=row.get('text_consensus',[])
    word=re.sub(r'^[」ヶ・]','',row.get('text','')).rstrip('、')
    return (word=='以降' and row.get('score',0)>=.6 and len(reads)==2
            and {r.get('height') for r in reads}=={48,64}
            and all(r.get('text','').rstrip('、')=='以降' and r.get('score',0)>=.7 for r in reads))


def repair_fragments(scanner,crop,reads,scale,source_ratio=1):
    import cv2
    from PIL import ImageOps
    np=scanner.memory.np;Image=scanner.memory.Image
    for row in reads:
        x1,y1=np.asarray(row['box']).min(0);x2,y2=np.asarray(row['box']).max(0)
        if .6<=row['score']<.7 and row['text'] in ('時','時、','合','合、'):
            # Separate the tall character strokes from the short comma. Keep
            # all original text and confidence; add independently measured pixels.
            line=crop.crop((max(0,int(x1)-3),max(0,int(y1)-3),min(crop.width,int(x2)+3),min(crop.height,int(y2)+3)))
            a=np.asarray(line).astype(int)
            mask=((a.max(2)<160)&(np.ptp(a,axis=2)<25)).astype('uint8')
            stats=cv2.connectedComponentsWithStats(mask)[2][1:]
            if len(stats):
                tall=[b for b in stats if b[3]>=max(b[3] for b in stats)*.4 and b[4]>=max(b[4] for b in stats)*.3]
                if not tall:continue
                x=min(b[0] for b in tall);y=min(b[1] for b in tall)
                right=max(b[0]+b[2] for b in tall);bottom=max(b[1]+b[3] for b in tall)
                native_height=(bottom-y)/scale*source_ratio
                if native_height>=18 and .7<(right-x)/(bottom-y)<1.3:
                    stem='toki' if row['text'][0]=='時' else 'gou'
                    path=PROJECT_ROOT/f'configs/recognition/glyphs/isolated-{stem}.png'
                    with Image.open(path) as im:reference=np.asarray(im)>127
                    glyph=Image.fromarray(mask[y:bottom,x:right]*255).resize((32,32),Image.Resampling.NEAREST)
                    binary=np.asarray(glyph)>127
                    iou=float((binary&reference).sum()/max(1,(binary|reference).sum()))
                    to_ref=cv2.distanceTransform((~reference).astype('uint8'),cv2.DIST_L2,3)[binary]
                    to_glyph=cv2.distanceTransform((~binary).astype('uint8'),cv2.DIST_L2,3)[reference]
                    distance=float(max(to_ref.mean(),to_glyph.mean()))
                    coverage=float(min((to_ref<=2).mean(),(to_glyph<=2).mean()))
                    if iou>=.75 and distance<=1 and coverage>=.98:
                        row['fragment_evidence']={'method':f'isolated-{stem}-template-v1','iou':iou,'distance':distance,'coverage':coverage,'native_height':float(native_height),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        # Effect rows start after the left bullet. Reread actual text pixels;
        # a real two-digit number remains wholly inside this text region.
        if x1<65*scale and re.match(r'^[トヘ1`・]?[0-9０-９]+ターン後',row['text']):
            line=crop.crop((round(65*scale),max(0,int(y1)-3),min(crop.width,int(x2)+3),min(crop.height,int(y2)+3)))
            result=scanner.memory.text(line)
            if (result.get('score') or 0)>=.7 and re.match(r'^[・トヘ]?[0-9０-９]+ターン後',result.get('text') or ''):
                row['original_read']={'text':row['text'],'score':row['score']}
                row.update(result,text_crop_left=round(65*scale))
        if row['score']>=.7 or re.sub(r'^[」ヶ・]','',row['text']).rstrip('、')!='以降':continue
        x1=max(x1,76*scale)
        line=crop.crop((max(0,int(x1)-3),max(0,int(y1)-3),min(crop.width,int(x2)+3),min(crop.height,int(y2)+3)))
        reread=scanner.memory.text(line)
        if (reread.get('score') or 0)>=.6 and (reread.get('text') or '').rstrip('、')=='以降':
            row['original_read']={'text':row['text'],'score':row['score']}
            row.update(reread,text_crop_left=int(x1)-3)
        proof=[]
        for h in (48,64):
            pixels=ImageOps.expand(line.resize((round(line.width*h/line.height),h)),border=12,fill='white')
            result,_=scanner.memory.scanner.ocr(np.asarray(pixels))
            if result and len(result)==1:proof.append({'text':result[0][1],'score':float(result[0][2]),'height':h})
        row['text_consensus']=proof
    a=np.asarray(crop).astype(int)
    mask=((a.max(2)<160)&(a.max(2)-a.min(2)<25)).astype('uint8')
    mask[:round(85*scale)]=0;mask[:,:round(12*scale)]=0;mask[:,round(66*scale):]=0
    count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
    path=PROJECT_ROOT/'configs/recognition/glyphs/isolated-ku.png'
    with Image.open(path) as im:reference=np.asarray(im)>127
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    added=[]
    # Some CJK characters have disconnected radicals. Group the original dark
    # pixels into short horizontal bands before comparing a complete glyph.
    ys=np.flatnonzero(mask.any(axis=1))
    groups=np.split(ys,np.flatnonzero(np.diff(ys)>5*scale)+1) if len(ys) else []
    path_shou=PROJECT_ROOT/'configs/recognition/glyphs/isolated-shou.png'
    with Image.open(path_shou) as im:shou=np.asarray(im)>127
    for group in groups:
        if not len(group):continue
        y=int(group[0]);bottom=int(group[-1])+1;xs=np.flatnonzero(mask[y:bottom].any(axis=0))
        x=int(xs[0]);right=int(xs[-1])+1;w=right-x;h=bottom-y
        if not (18*scale<=w<=36*scale and 18*scale<=h<=36*scale and right<round(66*scale)):continue
        native_height=h/scale*source_ratio
        if native_height<18:continue
        if any(min(p[0] for p in r['box'])<=x+w/2<=max(p[0] for p in r['box'])
               and min(p[1] for p in r['box'])<=y+h/2<=max(p[1] for p in r['box']) for r in reads):continue
        glyph=Image.fromarray(mask[y:bottom,x:right]*255).resize((32,32),Image.Resampling.NEAREST)
        binary=np.asarray(glyph)>127;iou=float((binary&shou).sum()/max(1,(binary|shou).sum()))
        a_dist=cv2.distanceTransform((~shou).astype('uint8'),cv2.DIST_L2,3)[binary]
        b_dist=cv2.distanceTransform((~binary).astype('uint8'),cv2.DIST_L2,3)[shou]
        distance=float(max(a_dist.mean(),b_dist.mean()));coverage=float(min((a_dist<=2).mean(),(b_dist<=2).mean()))
        if iou>=.75 and distance<=1 and coverage>=.98:
            added.append({'text':'昇','score':0,'box':[[x,y],[right,y],[right,bottom],[x,bottom]],
                'fragment_evidence':{'method':'isolated-shou-template-v1','iou':iou,'distance':distance,'coverage':coverage,'native_height':native_height,'sha256':hashlib.sha256(path_shou.read_bytes()).hexdigest()}})
    for index in range(1,count):
        x,y,w,h,area=map(int,stats[index])
        if not (7*scale<=w<=33*scale and 18*scale<=h<=36*scale and area>=10*scale*scale):continue
        native_height=h/scale*source_ratio
        if native_height<18:continue
        if any(min(p[0] for p in r['box'])<=x+w/2<=max(p[0] for p in r['box'])
               and min(p[1] for p in r['box'])<=y+h/2<=max(p[1] for p in r['box']) for r in reads):continue
        glyph=Image.fromarray(((labels[y:y+h,x:x+w]==index)*255).astype('uint8')).resize((32,32),Image.Resampling.NEAREST)
        binary=np.asarray(glyph)>127;iou=float((binary&reference).sum()/max(1,(binary|reference).sum()))
        to_ref=cv2.distanceTransform((~reference).astype('uint8'),cv2.DIST_L2,3)[binary]
        to_glyph=cv2.distanceTransform((~binary).astype('uint8'),cv2.DIST_L2,3)[reference]
        distance=float(max(to_ref.mean(),to_glyph.mean()))
        coverage=float(min((to_ref<=2).mean(),(to_glyph<=2).mean()))
        if iou<.7 or distance>1 or coverage<.98:continue
        added.append({'text':'く','score':0,'box':[[x,y],[x+w,y],[x+w,y+h],[x,y+h]],
                      'fragment_evidence':{'method':'isolated-ku-template-v2','iou':iou,'distance':distance,'coverage':coverage,'sha256':digest,'native_height':native_height}})
    for row in added:
        center=(row['box'][0][1]+row['box'][2][1])/2
        index=next((i for i,r in enumerate(reads) if sum(p[1] for p in r['box'])/4>center+8*scale),len(reads))
        reads.insert(index,row)
    return reads
