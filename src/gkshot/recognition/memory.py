"""Combine explicitly grouped PC memory-detail pages; keep provenance and unknowns."""
import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from gkshot.catalog import PROJECT_ROOT
from gkshot.recognition.automatic import ScreenshotScanner
from gkshot.recognition.customization_badge import detect_badge
from gkshot.recognition.item_variant import verify_item_variant
from gkshot.recognition.native_resolution import native_crop, retry_item, retry_card


class MemoryScanner:
    def __init__(self, scanner=None):
        self.scanner = scanner or ScreenshotScanner()
        self.np, self.Image, self.catalog = self.scanner.np, self.scanner.Image, self.scanner.catalog
        self.layout = json.loads((PROJECT_ROOT / "configs/layouts/memory-detail-pc-v1.json").read_text(encoding="utf-8"))
        self.template_ids, templates = [], []
        for name in sorted(self.catalog.manifest["files"]):
            if name.startswith("card-icons/"):
                with self.Image.open(self.catalog.verified_file(name)) as image:
                    templates.append(self.np.asarray(image.convert("RGB").resize((64, 64)), dtype=self.np.float32))
                self.template_ids.append(int(Path(name).stem))
        self.templates = self.np.stack(templates)
        self._line_ocr = None

    def normalize(self, path, *, client_area=False):
        np = self.np
        with self.Image.open(path) as source:
            if source.width * source.height > 30_000_000:
                raise ValueError("截图过大，请只截取游戏窗口。")
            image = source.convert("RGB")
        if client_area:
            if image.width < 400 or abs(image.height/image.width-16/9) > 0.03:
                raise ValueError("采集客户区尺寸不符合 PC 竖版布局，不能套用边界。")
            width = self.layout["canonical_width"]
            height = round(image.height*width/image.width)
            frame = {"left": 0, "top": 0, "right": image.width,
                     "normalized_size": [width,height], "source": "capture-client-area"}
            return image.resize((width,height),self.Image.Resampling.LANCZOS),frame
        pixels = np.asarray(image).astype(np.int16)
        saturation = pixels.max(2) - pixels.min(2)
        rows = np.flatnonzero((saturation > 40).mean(1) > 0.75)
        runs = np.split(rows, np.where(np.diff(rows) > 1)[0] + 1)
        banner = next((run for run in runs if len(run) > max(20, image.width * 0.035)), None)
        if banner is None:
            raise ValueError("未找到记忆页顶部画面；当前需要包含整个游戏窗口的竖版截图。")
        top = int(banner[0])
        columns = np.flatnonzero((saturation[top + 5:top + 25] > 40).mean(0) > 0.8)
        if not len(columns):
            raise ValueError("无法确定游戏窗口边界。")
        left, right = int(columns[0]), int(columns[-1]) + 1
        width = self.layout["canonical_width"]
        height = round((image.height - top) * width / (right - left))
        if not self.layout["min_normalized_height"] <= height <= self.layout["max_normalized_height"]:
            raise ValueError("记忆详情目前仅支持示例的 PC 竖版完整窗口及等比例缩放。")
        image = image.crop((left, top, right, image.height)).resize((width, height), self.Image.Resampling.LANCZOS)
        return image, {"left": left, "top": top, "right": right, "normalized_size": [width, height]}

    def text(self, crop):
        from PIL import ImageOps
        if self._line_ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            root = PROJECT_ROOT / "assets/ocr"
            name = "japan_PP-OCRv4_rec_mobile.onnx"
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            path = root / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
                raise ValueError("本地日文 OCR 模型校验失败。")
            self._line_ocr = RapidOCR(use_text_det=False,use_angle_cls=False,rec_model_path=str(path))
        # These are known single-line fields; detection on a tightly cropped line
        # splits Japanese names and loses characters. Give recognition a margin.
        result, _ = self._line_ocr(self.np.asarray(ImageOps.expand(crop,border=10,fill="white")))
        if result and len(result) == 1 and float(result[0][2]) >= 0.55:
            return {"text": result[0][1].strip(), "score": round(float(result[0][2]), 4)}
        return {"text": None, "score": None}

    def verify_card_label(self, record, label):
        read = self.text(label)
        record["label_read"] = read
        if not read["text"] or read["score"] < 0.7:
            return record
        def family(name):
            from gkshot.recognition.card_names import canonical_name
            return canonical_name(name).rstrip("+・;:")
        observed = family(read["text"])
        families = {}
        for card in self.catalog.skill_cards.values():
            families.setdefault(family(card["name"]),[]).append(card)
        ranked = sorted(((SequenceMatcher(None,observed,name).ratio(),name) for name in families),reverse=True)
        similarity,name = ranked[0]
        if similarity < 0.88 or similarity-ranked[1][0] < 0.08:
            return record
        selected = self.catalog.skill_cards[record["id"]]
        if family(selected["name"]) == name:
            # Matching the current model guess still supplies useful independent
            # evidence; previously this early return discarded that agreement.
            best=record['template_candidates'][0]
            mark=record['plus_glyph_distance']
            clear_variant=mark<3000 if selected['upgraded'] else mark>5000
            if (record.get('method')!='onnx-with-template-correction' and record['candidates'][0]['score']<.95 and best['distance']<3500 and clear_variant
                    and family(self.catalog.skill_cards[best['id']]['name'])==name):
                record.update(method='label-and-template',label_match={'family':name,'similarity':round(similarity,4),'art_agrees':True})
            return record
        best = record["template_candidates"][0]
        corroborated = best["distance"] < 3500 and family(self.catalog.skill_cards[best["id"]]["name"]) == name
        upgraded = record["plus_glyph_distance"] < 3000
        candidates = [c for c in families[name] if bool(c["upgraded"]) == upgraded]
        record["label_match"] = {"family":name,"similarity":round(similarity,4),"art_agrees":corroborated}
        record["visual_id_before_label"] = record["id"]
        if corroborated and len(candidates)==1:
            record.update(id=candidates[0]["id"],method="label-and-template")
        else:
            # A clear conflicting label must not leave the visual guess filled in.
            record.update(id=None,method="conflicting-card-label")
        return record

    def signature(self, image, region):
        return self.np.asarray(image.crop(region).resize((32, 16)), dtype=self.np.uint8).tolist()

    def stage_active(self, image):
        pixels = self.np.asarray(image.crop(self.layout["stage_tab"]))
        return int(((pixels[:, :, 0] > 220) & (pixels[:, :, 1] > 50) &
                    (pixels[:, :, 1] < 190) & (pixels[:, :, 2] < 90)).sum()) > 180

    def content_offset(self, image):
        """The optional event emblem adds height above the fixed Stage tab."""
        p = self.np.asarray(image.crop((600,880,880,1040)))
        ys = self.np.where((p[:,:,0]>220)&(p[:,:,1]>50)&(p[:,:,1]<190)&(p[:,:,2]<90))[0]
        if len(ys) < 180:
            return 0
        offset = round(float(self.np.median(ys))+880-936)
        return offset if 25 <= offset <= 55 else 0

    def card(self, crop):
        np = self.np
        result = self.scanner.classify_card(crop)
        pixels = np.asarray(crop.resize((64, 64)), dtype=np.float32)
        differences = (self.templates - pixels) ** 2
        errors = differences[:, 8:48, 14:49].mean((1, 2, 3)) + 0.15 * differences.mean((1, 2, 3))
        best = np.argsort(errors)[:8]
        result["template_candidates"] = [{"id": self.template_ids[i], "distance": round(float(errors[i]), 2)} for i in best[:5]]
        first_id = self.template_ids[best[0]]
        first = self.catalog.skill_cards[first_id]
        # Compare distinct art families; upgraded/unupgraded siblings often share the same center.
        other = next((float(errors[i]) for i in best if
                      self.catalog.skill_cards[self.template_ids[i]]["name"].removesuffix("+") != first["name"].removesuffix("+")), float("inf"))
        if (result["candidates"][0]["score"] < 0.95 and float(errors[best[0]]) < 1300
                and float(errors[best[0]]) < other * 0.7):
            chosen = first_id
            if result["plus_glyph_distance"] < 3000 and not first["upgraded"]:
                sibling = next((card for card in self.catalog.skill_cards.values()
                                if card["name"] == first["name"] + "+" and card["pIdolId"] == first["pIdolId"]), None)
                if sibling:
                    chosen = sibling["id"]
            result.update(id=chosen, method="onnx-with-template-correction")
        return result

    def boxes(self, image, *, with_grid=False):
        import cv2
        from gkshot.recognition.card_frames import complete_card_frames, complete_card_grid, detail_at_bottom
        np = self.np
        pixels = np.asarray(image).astype(np.int16)
        mask = ((pixels.max(2) - pixels.min(2) > 40) & (pixels.max(2) > 150)).astype(np.uint8) * 255
        bottom = image.height - self.layout["navigation_exclusion_bottom"]
        mask[:self.layout["scroll_viewport_top"]] = 0
        mask[bottom:] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        stats = cv2.connectedComponentsWithStats(mask)[2]
        found = []
        rectangles=[(int(x),int(y),int(w),int(h)) for x,y,w,h,area in stats[1:]
                    if 190<w<235 and 190<h<235 and area>10000 and y+h+3<bottom]
        rectangles=complete_card_frames(image,rectangles,top=self.layout["scroll_viewport_top"],bottom=bottom)
        grid=complete_card_grid(image,rectangles,at_bottom=detail_at_bottom(image),read_heading=self.text)
        if grid:
            result=[(slot,[x-2,y-2,x+w+2,y+h+2]) for slot,(x,y,w,h) in enumerate(grid['boxes'])]
            return (result,grid) if with_grid else result
        for x,y,w,h in rectangles:
            box=[x-2,y-2,x+w+2,y+h+2]
            column=min(range(4),key=lambda c:abs(box[0]-self.layout["card_columns"][c]))
            if abs(box[0]-self.layout["card_columns"][column])<15:
                found.append((box,column))
        groups = []
        for box, column in sorted(found, key=lambda pair: (pair[0][1], pair[0][0])):
            if not groups or abs(box[1] - groups[-1][0][0][1]) > 20:
                groups.append([])
            groups[-1].append((box, column))
        # A four-column first row establishes slot numbers. Never guess positions from two isolated cards.
        anchor = next((i for i, group in enumerate(groups) if sorted(c for _, c in group) == [0, 1, 2, 3]), None)
        result = []
        if anchor is not None:
            result.extend((column, box) for box, column in groups[anchor])
            if anchor + 1 < len(groups):
                second = groups[anchor + 1]
                if sorted(c for _, c in second) == [0, 1]:
                    result.extend((4 + column, box) for box, column in second)
        return (result,None) if with_grid else result

    def read_parameter(self,crop):
        """Retry black parameter digits without the neighboring colored rank badge."""
        import cv2
        original=self.scanner.read_number(crop)
        if original['value'] is not None and len(original['reads'])>=2:
            return original
        np=self.np
        pixels=np.asarray(crop).astype(int)
        dark=((pixels.max(2)<160)&(np.ptp(pixels,axis=2)<35)).astype(np.uint8)
        count,labels,stats,_=cv2.connectedComponentsWithStats(dark)
        selected=[]
        for index in range(1,count):
            x,y,w,h,area=stats[index]
            if h>=crop.height*.45 and area>=25:
                # A cut-off glyph cannot be completed from a plausible number.
                if x==0 or y==0 or x+w>=crop.width or y+h>=crop.height:
                    return original
                selected.append(index)
        if not 1<=len(selected)<=5:
            return original
        boxes=[stats[i] for i in selected]
        if max(y+h for x,y,w,h,a in boxes)-min(y+h for x,y,w,h,a in boxes)>crop.height*.15:
            return original
        clean=np.full(dark.shape,255,dtype=np.uint8)
        clean[np.isin(labels,selected)]=0
        clean_image=self.Image.fromarray(clean).convert('RGB')
        result=self.scanner.read_number(clean_image)
        if result['value'] is None or len(result['reads'])<2:
            # Keep only fully contained neutral glyphs before adding margin;
            # padding the original crop can turn a clipped rank into a digit.
            from PIL import ImageOps
            padded=self.scanner.read_number(ImageOps.expand(clean_image,border=6,fill='white'))
            if (padded['value'] is not None and len(padded['reads'])>=2
                    and result['value'] in (None,padded['value'])):
                result=dict(padded,padding_retry=6,unpadded_reads=result['reads'])
        if (result['value'] is None or len(result['reads'])<2 or
                (original['value'] is not None and original['value']!=result['value'])):
            return original
        result.update(method='neutral-digit-components',original_reads=original['reads'],
                      digit_components=[list(map(int,b[:4])) for b in boxes])
        return result

    def page(self, path, *, client_area=False):
        image, frame = self.normalize(path,client_area=client_area)
        with self.Image.open(path) as source:
            raw=source.convert('RGB')
        if not self.stage_active(image):
            raise ValueError(f"{path.name}：需要记忆详情的『ステージ』页，不能用记忆列表或编成页替代。")
        title = self.text(image.crop(self.layout["title"]))
        name = self.text(image.crop(self.layout["idol_name"]))
        power = self.scanner.read_number(image.crop(self.layout["power"]))
        if not title['text']:title=self.text(native_crop(raw,frame,self.layout['title'])[0])
        if not name['text']:name=self.text(native_crop(raw,frame,self.layout['idol_name'])[0])
        if power['value'] is None:power=self.scanner.read_number(native_crop(raw,frame,self.layout['power'])[0])
        if not title["text"] or not name["text"] or power["value"] is None:
            raise ValueError(f"{path.name}：无法读齐固定页头的身份信息，暂不能安全合并。")
        offset = self.content_offset(image)
        def shifted(box):
            return (box[0],box[1]+offset,box[2],box[3]+offset)
        numbers = {key: self.read_parameter(image.crop(shifted(box))) for key, box in self.layout["parameters"].items()}
        for key,box in self.layout['parameters'].items():
            old=numbers[key]
            if old['value'] is None or len(old['reads'])<2:
                crop,bounds=native_crop(raw,frame,shifted(box))
                retry=self.read_parameter(crop)
                proof={'source_size':list(crop.size),'bounds':bounds,'reads':retry,'original_reads':old['reads']}
                if retry['value'] is not None and len(retry['reads'])>=2 and old['value'] in (None,retry['value']):
                    numbers[key]={**retry,'native_retry':proof}
                else:old['native_retry']=proof
        # These regions only apply when the parameter row is still in view.
        parameters_visible = all(value["value"] is not None for value in numbers.values())
        items = None
        if parameters_visible:
            items = []
            for box in self.layout["item_slots"]:
                crop = image.crop(shifted(box))
                item = self.scanner.classify_item(crop)
                # Respect original resolution before allowing corner evidence.
                if crop.width*(frame["right"]-frame["left"])/image.width >= 80:
                    item = verify_item_variant(self.scanner,crop,item)
                tile,bounds=native_crop(raw,frame,shifted(box))
                item=retry_item(self.scanner,tile,item)
                if item.get('native_retry'):item['native_retry']['source_bounds']=bounds
                items.append(item)
        cards = []
        boxes,grid = self.boxes(image,with_grid=True)
        for slot, box in boxes:
            record = self.card(image.crop(box))
            x1,y1,x2,y2 = box
            label_bottom = min(y2+52,image.height-self.layout["navigation_exclusion_bottom"])
            if label_bottom-y2 >= 40:
                record = self.verify_card_label(record,image.crop((max(0,x1-5),y2,min(image.width,x2+15),label_bottom)))
            tile,bounds=native_crop(raw,frame,box)
            label=(native_crop(raw,frame,(max(0,x1-5),y2,min(image.width,x2+15),label_bottom))[0]
                   if label_bottom-y2>=40 else None)
            record=retry_card(self,tile,label,record)
            if record.get('native_retry'):record['native_retry']['source_bounds']=bounds
            # Preserve the original pixel budget; normalization must not turn a
            # tiny unreadable badge into seemingly high-resolution evidence.
            scale=min(1,(frame['right']-frame['left'])/image.width)
            normalized_tile=image.crop(box)
            normalized_tile=normalized_tile.resize((max(1,round(normalized_tile.width*scale)),max(1,round(normalized_tile.height*scale))))
            badge=detect_badge(normalized_tile,self.np)
            if badge['state']=='unknown':
                native_badge=detect_badge(tile,self.np)
                native_badge['native_pixels']=True
                native_badge['source_size']=list(tile.size)
                if native_badge['state']!='unknown':badge=native_badge
                else:badge['native_retry']=native_badge
            record.update(slot=slot, box=box, customization_badge=badge)
            cards.append(record)
        return {
            "source_path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "frame": frame, "identity": {"title": title["text"], "idol_name": name["text"], "power": power["value"]},
            "header_signature": self.signature(image, self.layout["header_signature"]),
            "banner_signature": self.signature(image, self.layout["banner_signature"]),
            "parameters": {key: value["value"] for key, value in numbers.items()} if parameters_visible else None,
            "number_reads": numbers, "items": items, "cards": cards, "content_offset": offset,
            "card_grid": grid
        }

    def scan(self, detail_paths, *, context_path=None, client_area=False):
        if not detail_paths:
            raise ValueError("请选择同一份记忆的一张或多张详情截图。")
        pages, hashes = [], set()
        for path in map(Path, detail_paths):
            page = self.page(path,client_area=client_area)
            if page["sha256"] not in hashes:
                pages.append(page)
                hashes.add(page["sha256"])
        first = pages[0]
        for page in pages[1:]:
            same_text = all(re.sub(r"\s+", "", str(page["identity"][key])) ==
                            re.sub(r"\s+", "", str(first["identity"][key])) for key in ("title", "idol_name", "power"))
            rms = float(self.np.sqrt(((self.np.asarray(page["header_signature"], dtype=float) -
                                       self.np.asarray(first["header_signature"], dtype=float)) ** 2).mean()))
            if not same_text or rms > 15:
                raise ValueError("这些详情截图的固定页头不一致，可能属于不同记忆；请分组导入。")
        from gkshot.recognition.card_label_consensus import refine as refine_labels
        refine_labels(pages,self.catalog)
        context = None
        if context_path is not None:
            context_path = Path(context_path)
            image, frame = self.normalize(context_path)
            if self.stage_active(image):
                raise ValueError("列表定位图应来自记忆列表，不能是另一张详情图。")
            power = self.scanner.read_number(image.crop(self.layout["list_power"]))["value"]
            signature = self.np.asarray(self.signature(image, self.layout["banner_signature"]), dtype=float)
            rms = float(self.np.sqrt(((signature - self.np.asarray(first["banner_signature"], dtype=float)) ** 2).mean()))
            if power != first["identity"]["power"] or rms > 15:
                raise ValueError("列表中选中的记忆与详情页不一致，或未能确认；请核对定位截图。")
            context = {"source_path": str(context_path.resolve()), "sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
                       "role": "list-context", "selected_power": power, "frame": frame,
                       "note": "仅为本次采集的定位线索；列表排序变化后坐标不能复用，不能把所持数当作可用记忆数。"}
        draft = {"parameters": {k: None for k in self.layout["parameters"]}, "item_ids": [None] * 4,
                 "card_ids": [None] * 6, "customizations": [None] * 6, "p_idol_id": None}
        evidence, conflicts = {}, []
        def merge(field, slot, value, page):
            key = f"{field}.{slot}"
            evidence.setdefault(key, []).append({"sha256": page["sha256"], "value": value})
            current = draft[field][slot]
            if key in conflicts:
                return
            if current is not None and current != value:
                conflicts.append(key)
                draft[field][slot] = None
            else:
                draft[field][slot] = value
        for page in pages:
            if page["parameters"]:
                for key, value in page["parameters"].items(): merge("parameters", key, value, page)
            if page["items"]:
                for index, item in enumerate(page["items"]): merge("item_ids", index, item["id"], page)
            for card in page["cards"]: merge("card_ids", card["slot"], card["id"], page)
            grid = page.get('card_grid') or {}
            if grid.get('rule')=='pc-packed-card-grid-v1' and grid.get('bottom_confirmed') and grid.get('heading_confirmed') and grid.get('trailing_blank_confirmed'):
                for slot in grid['empty_slots']:
                    merge('card_ids',slot,0,page)
        p_ids = {self.catalog.skill_cards[card].get("pIdolId") for card in draft["card_ids"] if card in self.catalog.skill_cards}
        p_ids.discard("")
        if len(p_ids) == 1:
            draft["p_idol_id"] = next(iter(p_ids))
        missing = [f"parameters.{key}" for key, value in draft["parameters"].items() if value is None]
        missing += [f"{field}.{i}" for field in ("item_ids", "card_ids") for i, value in enumerate(draft[field]) if value is None]
        for slot in range(6):
            observations = [(p,c) for p in pages for c in p["cards"] if c["slot"] == slot]
            states = {c.get("customization_badge",{}).get("state") for _,c in observations}
            key = f"customizations.{slot}"
            if draft['card_ids'][slot]==0:
                draft['customizations'][slot]=''
                for page in pages:
                    if slot in (page.get('card_grid') or {}).get('empty_slots',[]):
                        evidence.setdefault(key,[]).append({'sha256':page['sha256'],'card_id':0,
                            'method':'confirmed-empty-card-slot','value':''})
                continue
            for page,card in observations:
                badge = card.get("customization_badge",{})
                evidence.setdefault(key,[]).append({"sha256":page["sha256"], "card_id":card["id"],
                    "method":"central-green-counter", "badge":badge,
                    "value":"" if badge.get("state")=="absent" else None})
            if "absent" in states and "present" in states:
                conflicts.append(key)
            elif "absent" in states and draft["card_ids"][slot] is not None:
                draft["customizations"][slot] = ""
        for page in pages:
            page.pop("header_signature")
            page.pop("banner_signature")
        return {"report_version": 1, "kind": "owned-memory", "status": "incomplete" if missing or conflicts else "needs-review",
                "identity": first["identity"], "draft": draft, "missing_fields": missing, "conflicts": conflicts,
                "evidence": evidence, "pages": pages, "list_context": context,
                "layout_id": self.layout["id"], "recognition_revision": self.catalog.manifest["revision"],
                "warnings": ["同一次导入只能选择同一份记忆的详情截图；页头相同不构成游戏唯一 ID。",
                             "记忆可少于六张卡；确认卡组末尾的空白槽填 0，遮挡、裁切或未识别仍保留未知。",
                             "中央绿色数字角标清晰缺席即无自定义；有角标需读取详情，不清晰或证据冲突才保留未知。",
                             "当前仅支持示例 PC 竖版详情布局。列表截图仅用于定位，不用于填充缺失属性。"]}
