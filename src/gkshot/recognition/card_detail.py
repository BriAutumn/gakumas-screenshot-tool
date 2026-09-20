"""Offline tooltip OCR and version-bound, explicit customization rules.

Recognition is field evidence, not certification of an entire owned memory.
Unsupported cards and incomplete tooltips never become an empty customization.
"""
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from gkshot.catalog import PROJECT_ROOT
from gkshot.recognition.customization_rules import complete_cases,is_complete_rule


def canonical(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def rule_fingerprint(card, effects):
    payload = {"card": card, "effects": effects}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reliable_line(row, threshold):
    if row['score'] >= threshold:
        return True
    if row.get('short_text_evidence'):
        from gkshot.recognition.short_text import reliable
        if reliable(row['short_text_evidence'],row['text']):return True
    if row.get('fragment_evidence') or row.get('text_consensus'):
        from gkshot.recognition.detail_fragments import fragment_reliable
        if fragment_reliable(row):return True
    # Very short '+1' lines have systematically low Japanese OCR confidence.
    # Require agreement with two numeric OCR reads, retaining the original score.
    proof=row.get('numeric_consensus',[])
    return (row['score']>=0.65 and re.fullmatch(r'\+\d+',canonical(row['text'])) is not None
            and len(proof)==2 and all(r['score']>=0.6 and r['text']==canonical(row['text']) for r in proof))


def match_rule(reads, card_id, catalog, profiles, corner_evidence=None):
    """Require every distinguishing field and invariant; do not fill missing numbers."""
    rule = next((r for r in profiles["rules"] if r["card_id"] == card_id), None)
    result = {"status": "unsupported", "token": None, "observed": {}, "missing": [], "candidates": []}
    if rule is None:
        return result
    card = catalog.skill_cards[card_id]
    effects = [catalog.customizations[int(k)] for k in card["availableCustomizations"].split(",") if k]
    if rule_fingerprint(card, effects) != rule["catalog_fingerprint"]:
        return dict(result, status="rules-changed")
    usable = [canonical(row["text"]) for row in reads if reliable_line(row,rule["min_line_score"])]
    joined = "\n".join(usable)
    if is_complete_rule(rule):
        # Full-panel grammars account for every recognized line, including base
        # effects. Never discard a low-confidence line to make a case match.
        if any(not reliable_line(row,rule['min_line_score']) for row in reads):
            return dict(result, status='needs-detail', missing=['low-confidence-line'])
        text = ''.join(usable)
        try:
            cases=complete_cases(rule,catalog)
        except (ValueError,KeyError,TypeError):
            return dict(result,status='rules-changed',missing=['unsupported-state-template'])
        # These kana pairs have the same glyph in this UI font. Apply the same
        # established name equivalence to both sides, never fuzzy text matching.
        kana=str.maketrans('ヘベペ','へべぺ')
        matches = [case for case in cases if re.fullmatch(case['pattern'].translate(kana),text.translate(kana))]
        if any('corner_cost' in case for case in cases):
            from gkshot.recognition.card_corner import valid_cost
            if not valid_cost(corner_evidence or {},card_id,card['name']):
                return dict(result,status='needs-detail',missing=['bound-corner-cost'])
            result['observed']['corner_cost']=corner_evidence['value']
            matches=[case for case in matches if case['corner_cost']==corner_evidence['value']]
        result['candidates'] = [case['token'] for case in matches]
        if len(matches) == 1:
            result.update(status='matched',token=matches[0]['token'])
        else:
            result.update(status='needs-detail',missing=['complete-effect-text'])
        return result
    # Title must be recognized independently of an expected slot/card ID.
    if canonical(card["name"]) not in usable:
        result["missing"].append("card_title")
    conflicts = []
    for key, pattern in rule["fields"].items():
        values = {int(value) for value in re.findall(pattern, joined)}
        if len(values) > 1:
            conflicts.append(key)
        elif values:
            result["observed"][key] = next(iter(values))
        else:
            result["missing"].append(key)
    for key, pattern in rule["required_text"].items():
        if not re.search(pattern, joined):
            result["missing"].append(key)
    result["candidates"] = [case["token"] for case in rule["cases"] if all(
        case["fields"].get(key) == value for key, value in result["observed"].items())]
    if conflicts or not result["candidates"]:
        result.update(status="conflict", conflicting_fields=conflicts)
    elif result["missing"] or len(result["candidates"]) != 1:
        result["status"] = "needs-detail"
    else:
        result.update(status="matched", token=result["candidates"][0])
    return result


def verify_popup_boundary_evidence(detail,attempt,rule):
    """Recompute any boundary exemption from hash-verified original pixels.

    This uses no OCR models and is also called at the library boundary.
    """
    coverage=attempt.get('green_text_coverage',{})
    if not coverage.get('outside_popup'):return True
    if not rule.get('verify_popup_boundary'):return False
    import numpy as np
    from PIL import Image
    from types import SimpleNamespace
    from gkshot.recognition.native_resolution import native_crop
    try:
        path=Path(detail['source_path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=detail['sha256']:return False
        with Image.open(path) as image:raw=image.convert('RGB')
        frame=detail['frame'];box=detail['popup_box'];variant=attempt['variant']
        if variant.startswith('native-'):
            scale=float(variant.removeprefix('native-'))
            if scale not in rule.get('fallback_scales',[]):return False
            base=native_crop(raw,frame,box)[0]
        else:
            if variant not in ('normalized','enlarged'):return False
            scale=1 if variant=='normalized' else 1.25
            if detail.get('pixel_mode')=='native':base=native_crop(raw,frame,box)[0]
            else:base=raw.crop((frame['left'],frame['top'],frame['right'],raw.height)).resize(tuple(frame['normalized_size']),Image.Resampling.LANCZOS).crop(box)
        pixels=base.resize((round(base.width*scale),round(base.height*scale))) if scale!=1 else base
        helper=CardDetailScanner.__new__(CardDetailScanner);helper.memory=SimpleNamespace(np=np,Image=Image)
        observed=helper.green_text_coverage(pixels,attempt['reads'],border=rule.get('green_ocr_border',0)*pixels.width/(box[2]-box[0]),
            icon_templates=rule.get('green_icon_templates',()),verify_popup_boundary=True)
        return observed.get('outside_popup')==coverage['outside_popup'] and observed['complete'] is True
    except (OSError,ValueError,KeyError,TypeError):return False


class CardDetailScanner:
    def __init__(self, memory_scanner=None):
        from rapidocr_onnxruntime import RapidOCR
        from gkshot.recognition.memory import MemoryScanner
        self.memory = memory_scanner or MemoryScanner()
        self.catalog = self.memory.catalog
        self.profile_path = PROJECT_ROOT / "configs/recognition/card-detail-rules.json"
        self.profiles = json.loads(self.profile_path.read_text(encoding="utf-8"))
        model_root = PROJECT_ROOT / "assets/ocr"
        manifest = json.loads((model_root / "manifest.json").read_text(encoding="utf-8"))
        name = "japan_PP-OCRv4_rec_mobile.onnx"
        path = model_root / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError("本地日文 OCR 模型校验失败，请恢复模型文件。")
        self.ocr = RapidOCR(use_angle_cls=False, rec_model_path=str(path))
        self.model_sha256 = manifest["files"][name]["sha256"]

    def boxes(self, image):
        """Find isolated white tooltip panels, without assuming left/right placement."""
        import cv2
        np = self.memory.np
        pixels = np.asarray(image)
        mask = (pixels.min(2) > 252).astype(np.uint8) * 255
        # A floating tooltip may extend above the Stage tab into the header.
        start = 620
        mask[:start] = 0
        boxes = []
        for x, y, w, h, area in cv2.connectedComponentsWithStats(mask)[2][1:]:
            if (350 < w < 750 and 140 < h < 900 and area > w * h * 0.55
                    and y > start + 2 and y + h < image.height - 4 and x > 1 and x + w < image.width - 1):
                boxes.append([int(x), int(y), int(x + w), int(y + h)])
        return boxes

    def read(self, crop, *, coordinate_scale=1.0):
        from PIL import ImageOps
        records, _ = self.ocr(self.memory.np.asarray(crop))
        rows = [{"text": text, "score": round(float(score), 5), "box": box} for box, text, score in (records or [])]
        for row in rows:
            self.repair_title_plus(crop,row,coordinate_scale=coordinate_scale)
            if row['score'] < 0.7:
                box=row['box']
                left,top=min(p[0] for p in box),min(p[1] for p in box)
                right,bottom=max(p[0] for p in box),max(p[1] for p in box)
                line=crop.crop((left,top,right,bottom))
                if re.fullmatch(r'[+]\d+',canonical(row['text'])):
                    enlarged=line.resize((round(line.width*58/line.height),58))
                    proof=[]
                    for margin in (10,30):
                        records,_=self.memory.scanner.ocr(self.memory.np.asarray(ImageOps.expand(enlarged,border=margin,fill='white')))
                        if records and len(records)==1:
                            proof.append({'text':canonical(records[0][1]),'score':float(records[0][2]),'margin':margin})
                    row['numeric_consensus']=proof
                else:
                    reread=self.memory.text(line)
                    if not reread['text'] or reread['score']<0.7:
                        reread=self.memory.text(line.resize((round(line.width*48/line.height),48)))
                    if (reread['text'] and reread['score']>=0.7 and
                        canonical(row['text']).lstrip('・ヶト')==canonical(reread['text']).lstrip('・ヶト')):
                        row.update(original_read={'text':row['text'],'score':row['score']},**reread)
        return rows

    def repair_title_plus(self,crop,row,*,coordinate_scale=1.0):
        """Repair an OCR punctuation suffix only when the final pixels form '+'."""
        import cv2
        np=self.memory.np
        text=canonical(row['text'])
        if row['score']<0.7 or max(p[1] for p in row['box'])>=85*coordinate_scale:
            return
        omitted=not text.endswith((':','ャ','ゃ',';'))
        name=text+'+' if omitted else text[:-1]+'+'
        ids=[c['id'] for c in self.catalog.skill_cards.values() if canonical(c['name'])==name]
        if len(ids)!=1:
            return
        box=row['box']
        line=crop.crop((max(0,min(p[0] for p in box)-2),max(0,min(p[1] for p in box)-2),
                        min(crop.width,max(p[0] for p in box)+(40*coordinate_scale if omitted else 6)),max(p[1] for p in box)+2))
        colors=np.asarray(line).astype(int)
        mask=((colors.max(2)<150)&(np.ptp(colors,axis=2)<35)).astype(np.uint8)
        _,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        components=[(i,b) for i,b in enumerate(stats[1:],1) if b[4]>=8]
        if not components:return
        index,(x,y,w,h,area)=max(components,key=lambda r:r[1][0]+r[1][2])
        if not (0.7<w/h<1.3 and h>=line.height*.35 and (omitted or x>line.width-40*coordinate_scale)):return
        if omitted and x+min(p[0] for p in box)-2 < max(p[0] for p in box)-max(8*coordinate_scale,w*1.25):return
        glyph=(labels[y:y+h,x:x+w]==index)
        if glyph.sum(0).max()<h*.85 or glyph.sum(1).max()<w*.85:return
        qx,qy=max(1,w//4),max(1,h//4)
        corners=np.concatenate([glyph[:qy,:qx].ravel(),glyph[:qy,-qx:].ravel(),glyph[-qy:,:qx].ravel(),glyph[-qy:,-qx:].ravel()])
        if corners.mean()>.1:return
        row.update(original_read={'text':row['text'],'score':row['score']},text=self.catalog.skill_cards[ids[0]]['name'],
                   title_plus_evidence={'method':'terminal-cross-glyph-v1','bounds':list(map(int,(x,y,w,h)))})

    def green_text_coverage(self,crop,reads,*,border=0,icon_templates=(),verify_popup_boundary=False):
        """An omitted green customization line must not look like no addition."""
        import cv2
        np=self.memory.np
        colors=np.asarray(crop).astype(int)
        r,g,b=colors[:,:,0],colors[:,:,1],colors[:,:,2]
        mask=((g>100)&(g-r>50)&(g-b>35)&(r<150)).astype(np.uint8)
        covered=np.zeros(mask.shape,dtype=np.uint8)
        for row in reads:
            if reliable_line(row,0.7):
                cv2.fillPoly(covered,[np.rint(row['box']).astype(np.int32)],1)
        # OCR polygons can cut through a glyph's antialiased edge. A bounded
        # two-pixel border at canonical scale covers that edge, never another
        # effect row. Low-confidence/unread rows still contribute no polygon.
        radius=max(0,round(border))
        if radius:
            covered=cv2.dilate(covered,np.ones((radius*2+1,radius*2+1),dtype=np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        outside=None;boundary=None;excluded=[]
        if verify_popup_boundary:
            # A convex hull fills text holes but preserves the white panel's
            # rounded corners. Require four substantial rounded corners, not
            # a rectangle dented by a clipped green character.
            white=(colors.min(2)>252).astype(np.uint8)
            contours,_=cv2.findContours(white,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                contour=max(contours,key=cv2.contourArea);hull=cv2.convexHull(contour)
                scale=crop.width/524
                corners=[(0,0),(crop.width-1,0),(0,crop.height-1),(crop.width-1,crop.height-1)]
                distances=[-cv2.pointPolygonTest(hull,point,True) for point in corners]
                if cv2.contourArea(contour)>.85*crop.width*crop.height and all(12*scale<=d<=40*scale for d in distances):
                    inside=np.zeros(mask.shape,dtype=np.uint8);cv2.fillConvexPoly(inside,hull,1)
                    outside=inside==0
                    boundary={'method':'white-rounded-popup-hull-v1','size':list(crop.size),
                              'hull':hull.reshape(-1,2).tolist(),'corner_distances':distances}
        missing=[]
        icons=[]
        for index in range(1,count):
            if stats[index,cv2.CC_STAT_AREA]>=5:
                component=labels==index
                if float(covered[component].mean())<0.9:
                    x,y,w,h=map(int,stats[index,:4])
                    if (outside is not None and (x==0 or x+w==crop.width)
                            and w<=3*scale and stats[index,cv2.CC_STAT_AREA]<=12*scale*scale
                            and bool(outside[component].all())):
                        excluded.append([x,y,w,h]);continue
                    # The rainbow cost heart contains green but is not text.
                    # Exempt only a full-color match to a hash-pinned glyph,
                    # never a region selected by position or green hue alone.
                    matched_icon=False
                    if .7<=w/max(h,1)<=1.4:
                        px,py=round(w*.3),round(h*.3)
                        bounds=(x-px,y-py,x+w+px,y+h+py)
                        if bounds[0]>=0 and bounds[1]>=0 and bounds[2]<=crop.width and bounds[3]<=crop.height:
                            candidate=np.asarray(crop.crop(bounds).resize((48,48)),dtype=float)
                            for spec in icon_templates:
                                path=PROJECT_ROOT/spec['path']
                                if hashlib.sha256(path.read_bytes()).hexdigest()!=spec['sha256']:
                                    continue
                                with self.memory.Image.open(path) as reference:
                                    distance=float(((candidate-np.asarray(reference.convert('RGB'),dtype=float))**2).mean())
                                if distance<spec['max_distance']:
                                    icons.append({'bounds':list(bounds),'template_sha256':spec['sha256'],'distance':round(distance,3)})
                                    matched_icon=True
                                    break
                    if matched_icon:
                        continue
                    missing.append(stats[index,:4].tolist())
        return {'complete':not missing,'unread_components':missing,**({'ocr_border_pixels':radius} if radius else {}),
                **({'outside_popup':dict(boundary,components=excluded)} if excluded else {}),
                **({'verified_nontext_icons':icons} if icons else {})}

    def scan(self, path, *, expected_card_id=None, client_area=False, owner_path=None):
        first=self._scan(path,expected_card_id=expected_card_id,client_area=client_area,owner_path=owner_path)
        retryable=(first['status']=='needs-detail' or (first['status']=='conflict' and first.get('reason')=='弹框卡名与目标卡槽不一致。'))
        if not retryable:return first
        retry=self._scan(path,expected_card_id=expected_card_id,client_area=client_area,owner_path=owner_path,native=True)
        prior={k:first.get(k) for k in ('status','card_id','token','reason','attempts')}
        if retry['status']=='matched' or (first['card_id'] is None and retry['status']=='unsupported'):
            retry['normalized_attempt']=prior
            return retry
        first['native_retry']={k:retry.get(k) for k in ('status','card_id','token','reason','attempts','native_popup_bounds')}
        rule=next((r for r in self.profiles['rules'] if r['card_id']==first.get('card_id')), {})
        if rule.get('fallback_scales') and first['status']=='needs-detail' and retry['status']!='conflict':
            return self.rescaled_consensus(first, path, rule, client_area=client_area)
        return first

    def rescaled_consensus(self, report, path, rule, *, client_area):
        """Retry full raw panels, retaining every failed read and any conflict.

        A failed enlargement is not evidence that a successful original read is
        wrong. Two sufficiently different resolutions must nevertheless agree
        on the complete text, and no complete read may name a different token.
        """
        import copy
        from gkshot.recognition.native_resolution import native_crop
        if not report.get('popup_box'):
            return report
        with self.memory.Image.open(path) as source:
            crop=native_crop(source.convert('RGB'),report['frame'],report['popup_box'])[0]
        width=report['popup_box'][2]-report['popup_box'][0]
        attempts=copy.deepcopy(report['attempts'])
        for attempt,scale in zip(attempts,(1,1.25)):
            attempt['pixel_width']=round(width*scale)
        # Native reads already made by scan must also veto contradictory tokens.
        prior=report.get('native_retry',{}).get('attempts',[])
        def tokens():
            return {a['match']['token'] for a in attempts+prior if a['match']['status']=='matched'}
        for scale in rule['fallback_scales']:
            pixels=crop.resize((round(crop.width*scale),round(crop.height*scale)))
            reads=self.read(pixels,coordinate_scale=pixels.width/width)
            if rule.get('small_fragments'):
                from gkshot.recognition.detail_fragments import repair_fragments
                reads=repair_fragments(self,pixels,reads,pixels.width/width,
                    (report['frame']['right']-report['frame']['left'])/report['frame']['normalized_size'][0])
            if rule.get('short_ctc'):
                from gkshot.recognition.short_text import repair
                reads=repair(self,pixels,reads,pixels.width/width)
            match=match_rule(reads,report['card_id'],self.catalog,self.profiles,report.get('corner_evidence'))
            coverage=self.green_text_coverage(pixels,reads,border=rule.get('green_ocr_border',0)*pixels.width/width,
                                              icon_templates=rule.get('green_icon_templates',()),verify_popup_boundary=rule.get('verify_popup_boundary',False))
            if not coverage['complete']:
                match.update(status='needs-detail',token=None)
                match['missing'].append('unread-green-text')
            attempts.append({'variant':f'native-{scale:g}', 'pixel_width':pixels.width,
                             'reads':reads,'match':match,'green_text_coverage':coverage})
            report['resolution_attempts']=attempts
            if len(tokens())>1 or any(a['match']['status']=='conflict' for a in attempts+prior):
                report.update(status='conflict',token=None,reason='不同分辨率的完整强化读数冲突，不自动填写。')
                return report
            good=[a for a in attempts if a['match']['status']=='matched' and a.get('green_text_coverage',{}).get('complete')]
            for first in good:
                for second in good:
                    if second['pixel_width']>=first['pixel_width']*1.15:
                        report.update(status='matched',token=first['match']['token'],attempts=[first,second],
                                      reason='原图详情在两种分辨率下完整匹配，绿色内容均已读取。',
                                      resolution_consensus={'method':'complete-panel-multiscale-v1',
                                          'pixel_widths':[first['pixel_width'],second['pixel_width']]})
                        return report
        return report

    def _scan(self, path, *, expected_card_id=None, client_area=False, owner_path=None, native=False):
        path = Path(path)
        image, frame = self.memory.normalize(path,client_area=client_area)
        ownership = None
        owner_image = image
        if owner_path is not None:
            if not client_area:
                raise ValueError("采集归属证据必须使用客户区原图。")
            owner_path = Path(owner_path)
            owner_image, _ = self.memory.normalize(owner_path,client_area=True)
            boxes = self.boxes(image)
            if len(boxes) != 1 or owner_image.size != image.size:
                raise ValueError("无法验证详情框之外的采集画面。")
            np = self.memory.np
            mask = np.ones((image.height,image.width),dtype=bool)
            x1,y1,x2,y2 = boxes[0]
            mask[max(0,y1-24):y2+24,max(0,x1-24):x2+24] = False
            delta = np.asarray(image,dtype=float)-np.asarray(owner_image,dtype=float)
            rms = float(np.sqrt((delta[mask]**2).mean()))
            if rms > 15:
                raise ValueError("浮动详情与展开前画面不一致，不能确认归属。")
            ownership = {"source_path":str(owner_path.resolve()),
                         "sha256":hashlib.sha256(owner_path.read_bytes()).hexdigest(),
                         "method":"capture-before-and-after-with-visible-background", "background_rms":round(rms,3)}
        if not self.memory.stage_active(owner_image):
            raise ValueError("请使用记忆详情的『ステージ』页，并点击卡片展开浮动详情。")
        title = self.memory.text(owner_image.crop(self.memory.layout["title"]))["text"]
        name = self.memory.text(owner_image.crop(self.memory.layout["idol_name"]))["text"]
        power = self.memory.scanner.read_number(owner_image.crop(self.memory.layout["power"]))["value"]
        if not title or not name or power is None:
            raise ValueError("无法读取记忆页头，不能确认详情归属。请保留完整游戏窗口。")
        report = {
            "kind": "card-detail", "report_version": 1,
            "source_path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "frame": frame, "identity": {"title": title, "idol_name": name, "power": power},
            "header_signature": self.memory.signature(owner_image, self.memory.layout["header_signature"]),
            "ownership_evidence": ownership,
            "recognition_revision": self.catalog.manifest["revision"], "ocr_model_sha256": self.model_sha256,
            "rules_sha256": hashlib.sha256(self.profile_path.read_bytes()).hexdigest(),
            "status": "needs-detail", "card_id": None, "token": None, "attempts": [],
        }
        panels = []
        raw=None
        if native:
            from gkshot.recognition.native_resolution import native_crop
            with self.memory.Image.open(path) as source:raw=source.convert('RGB')
        for box in self.boxes(image):
            crop = native_crop(raw,frame,box)[0] if native else image.crop(box)
            coordinate_scale=crop.width/(box[2]-box[0])
            reads = self.read(crop,coordinate_scale=coordinate_scale)
            # Only names at the top of a detected panel can identify its card.
            from gkshot.recognition.card_names import canonical_name
            titles = {canonical_name(row["text"]) for row in reads
                      if row["score"] >= 0.7 and max(p[1] for p in row["box"]) < 85*coordinate_scale}
            ids = [c["id"] for c in self.catalog.skill_cards.values() if canonical_name(c["name"]) in titles]
            if len(ids) == 1:
                panels.append((box, crop, reads, ids[0]))
        if len(panels) != 1:
            report["reason"] = "未找到唯一且卡名清楚的详情框，请重新展开并完整截图。"
            return report
        box, crop, first, card_id = panels[0]
        report.update(card_id=card_id, popup_box=box)
        coordinate_scale=crop.width/(box[2]-box[0])
        if native:
            report.update(pixel_mode='native',native_popup_bounds=native_crop(raw,frame,box)[1],
                          native_popup_size=list(crop.size))
        if expected_card_id is not None and expected_card_id != card_id:
            report.update(status="conflict", reason="弹框卡名与目标卡槽不一致。")
            return report
        second = self.read(crop.resize((round(crop.width * 1.25), round(crop.height * 1.25))),coordinate_scale=coordinate_scale*1.25)
        title_reads=[]
        for reads,scale in ((first,coordinate_scale),(second,coordinate_scale*1.25)):
            matching=[r for r in reads if r['score']>=.7 and max(p[1] for p in r['box'])<85*scale
                      and canonical_name(r['text'])==canonical_name(self.catalog.skill_cards[card_id]['name'])]
            if len(matching)==1:title_reads.append({'text':matching[0]['text'],'score':matching[0]['score']})
        report['identity_evidence']={'rule':'two-tooltip-title-reads-v1','card_id':card_id,
                                     'consistent':len(title_reads)==2,'reads':title_reads}
        rule=next((r for r in self.profiles['rules'] if r['card_id']==card_id),{})
        if rule.get('corner_cost_required'):
            from gkshot.recognition.card_corner import read_cost
            if owner_path is not None:
                cache=getattr(self,'_corner_cache',{})
                key=(ownership['sha256'],card_id)
                if key not in cache:cache[key]=read_cost(self.memory,owner_path,card_id)
                self._corner_cache=cache
                report['corner_evidence']=cache[key]
            else:
                report['corner_evidence']={'value':None,'reason':'missing-bound-owner'}
        for label, reads, pixels in (("normalized", first,crop), ("enlarged", second,crop.resize((round(crop.width*1.25),round(crop.height*1.25))))):
            if rule.get('small_fragments'):
                from gkshot.recognition.detail_fragments import repair_fragments
                reads=repair_fragments(self,pixels,reads,pixels.width/(box[2]-box[0]),
                    (frame['right']-frame['left'])/frame['normalized_size'][0])
            if rule.get('short_ctc'):
                from gkshot.recognition.short_text import repair
                reads=repair(self,pixels,reads,pixels.width/(box[2]-box[0]))
            match=match_rule(reads,card_id,self.catalog,self.profiles,report.get('corner_evidence'))
            attempt={"variant":label,"reads":reads,"match":match}
            if is_complete_rule(rule):
                coverage=self.green_text_coverage(pixels,reads,border=rule.get('green_ocr_border',0)*pixels.width/(box[2]-box[0]),
                                                  icon_templates=rule.get('green_icon_templates',()),verify_popup_boundary=rule.get('verify_popup_boundary',False))
                attempt['green_text_coverage']=coverage
                if not coverage['complete']:
                    match.update(status='needs-detail',token=None)
                    match['missing'].append('unread-green-text')
            report['attempts'].append(attempt)
        matches = [attempt["match"] for attempt in report["attempts"]]
        if all(m["status"] == "matched" for m in matches) and matches[0]["token"] == matches[1]["token"]:
            report.update(status="matched", token=matches[0]["token"], observed=matches[0]["observed"])
        elif any(m["status"] == "conflict" for m in matches) or len({m["token"] for m in matches if m["status"] == "matched"}) > 1:
            report["status"] = "conflict"
        elif all(m["status"] == "unsupported" for m in matches):
            report["status"] = "unsupported"
        elif any(m["status"] == "rules-changed" for m in matches):
            report["status"] = "rules-changed"
        report["reason"] = {"matched": "两种缩放读数一致，完整匹配当前卡片规则；不代表整份记忆已核验。",
                            "unsupported": "已读出卡名，但这张卡尚未配置并验证详情识别规则。",
                            "rules-changed": "本地卡片规则已变化，需要更新详情识别规则。",
                            "conflict": "详情读数存在冲突，不自动填写强化。",
                            "needs-detail": "关键字段未读齐或两次读取不一致，需要补采。"}[report["status"]]
        return report
