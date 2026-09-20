"""Layout/state checks only. Collection does not need card IDs or effect rules."""
import hashlib
import json
import unicodedata
from pathlib import Path

from gkshot.catalog import PROJECT_ROOT

DEFAULT_PROFILE = PROJECT_ROOT / "configs/capture/pc-memory-v1.json"


def load_profile(path=DEFAULT_PROFILE):
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_profile(profile)


def validate_profile(profile):
    if profile.get("schema_version") != 1 or profile.get("window_title", "").casefold() != "gakumas":
        raise ValueError("采集配置仅支持 gakumas PC 客户端。")
    for key in ("open_detail", "list_scroll", "stage_tab", "detail_scroll", "dismiss_tooltip", "back"):
        if len(profile[key]) != 2 or not all(0 < n < 1 for n in profile[key]):
            raise ValueError("无效的采集标定点：" + key)
    for key in ("list_region", "list_scrollbar", "stage_region", "list_button_region", "header_region", "detail_region"):
        x1, y1, x2, y2 = profile[key]
        if not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
            raise ValueError("无效的采集区域：" + key)
    if not 0.2 <= profile["settle_seconds"] <= 5 or not 2 <= profile["stable_timeout"] <= 60:
        raise ValueError("采集等待时间超出允许范围。")
    if type(profile["max_scroll_steps"]) is not int or not 1 <= profile["max_scroll_steps"] <= 200:
        raise ValueError("滚动上限无效。")
    if type(profile["tooltip_retries"]) is not int or not 1 <= profile["tooltip_retries"] <= 4:
        raise ValueError("详情重试次数无效。")
    if not 0 < profile["list_match_tolerance"] <= 12:
        raise ValueError("列表匹配阈值无效。")
    return profile


def crop(image, region):
    return image.crop(tuple(round(value * (image.width if i % 2 == 0 else image.height)) for i, value in enumerate(region)))


def signature(image, region=None, size=(24, 16)):
    image = crop(image, region) if region else image
    return list(image.convert("RGB").resize(size).tobytes())


def distance(a, b):
    if len(a) != len(b) or not a:
        return float("inf")
    return sum(abs(x-y) for x, y in zip(a, b)) / len(a)


def rows_match(a, b, tolerance):
    return len(a) == len(b) and all(x["column"] == y["column"] and distance(x["signature"], y["signature"]) < tolerance for x, y in zip(a, b))


def pages_match(a, b, tolerance):
    return len(a) == len(b) and all(rows_match(x, y, tolerance) for x, y in zip(a, b))


def relocate_page(saved, current, tolerance):
    """Relocate a complete, unique contiguous row sequence; never match a subset."""
    import copy
    if not saved or any(not row for row in saved):
        return None
    starts = [i for i in range(len(current)-len(saved)+1)
              if pages_match(saved,current[i:i+len(saved)],tolerance)]
    if len(starts) != 1:
        return None
    # Preserve only the saved rows: newly visible neighbours belong to the next
    # pagination step, and must not change existing page/cell identifiers.
    return copy.deepcopy(current[starts[0]:starts[0]+len(saved)])


class AmbiguousOverlap(ValueError):
    """Matching pictures alone cannot identify the owned row."""


def overlap(previous, current, tolerance, *, anchor=None, current_anchor=None, row_match=rows_match, counts=None):
    candidates = [count for count in (range(1, min(len(previous), len(current))+1) if counts is None else counts)
                  if 0<count<=min(len(previous),len(current))
                  if all(row_match(a, b, tolerance) for a, b in zip(previous[-count:], current[:count]))]
    if anchor is not None and current_anchor is not None:
        # The same orange selection survives scrolling. Unlike card art, it is
        # a positional anchor for this navigation operation, not a content ID.
        candidates = [count for count in candidates if anchor[1]==current_anchor[1]
                      and anchor[0]-(len(previous)-count)==current_anchor[0]
                      and 0<=current_anchor[0]<count]
    if len(candidates) > 1:
        raise AmbiguousOverlap("列表中相似画面导致翻页对应关系不唯一。")
    return candidates[0] if candidates else 0


def complete_internal_rows(tops, pitch):
    """Recover a lost contour row only between two regular-grid anchors.

    Selected orange brackets can join neighbouring thumbnail contours. Keep
    observed positions and interpolate only near integer multiples of the
    observed thumbnail height plus gap; never extend beyond either anchor.
    """
    if len(tops) < 2 or pitch <= 0:
        return list(tops)
    result = [tops[0]]
    for before, after in zip(tops, tops[1:]):
        gap = after - before
        steps = round(gap / pitch)
        if 2 <= steps <= 4 and abs(gap - steps * pitch) <= 8:
            result.extend(round(before + gap * i / steps) for i in range(1, steps))
        result.append(after)
    return result


class CaptureVision:
    def __init__(self, profile):
        import cv2
        import numpy as np
        self.profile, self.cv2, self.np = profile, cv2, np
        self._ocr = None
        self._button_cache = {}
        self._loading_cache = {}
        self._return_cache = {}

    def normalized(self, image):
        return image.convert("RGB").resize((1080, round(image.height * 1080/image.width)))

    def orange(self, image, region):
        a = self.np.asarray(crop(image, region)).astype(int)
        return float(((a[:, :, 0] > 225) & (a[:, :, 1] > 60) & (a[:, :, 1] < 200) & (a[:, :, 2] < 115)).mean())

    def is_list(self, image):
        region = self.profile["list_button_region"]
        pixels = self.np.asarray(crop(image,region).convert("RGB")).astype(int)
        r,g,b = pixels[:,:,0],pixels[:,:,1],pixels[:,:,2]
        # Production-only memories disable/dim the same button; color alone is
        # insufficient. Require the thumbnail grid, plus text for the dim state.
        warm = float(((r>140)&(g>60)&(g<205)&(b<155)&(r>g*1.15)&(g>b*1.1)).mean())
        if warm < 0.15:
            return False
        if sum(len(row) for row in self.list_rows(image)) < 3:
            return False
        if self.orange(image,region) > 0.12:
            return True
        button = crop(image,region)
        key = hashlib.sha256(button.resize((160,64)).tobytes()).hexdigest()
        if key not in self._button_cache:
            if len(self._button_cache)>64:self._button_cache.clear()
            self._button_cache[key] = any("詳細を見る" in row["text"].replace(" ","") and row["score"]>=0.7
                                           for row in self.read(button))
        return self._button_cache[key]

    def selection_matches(self, image, tile):
        """Orange corner brackets must surround the intended thumbnail."""
        image=self.normalized(image)
        x1,y1,x2,y2=[round(v*(image.width if i%2==0 else image.height)) for i,v in enumerate(tile["box"])]
        corners=[((x1-2,y1-12,x1+42,y1-2),(x1-12,y1-2,x1-2,y1+42)),
                 ((x2-42,y1-12,x2+2,y1-2),(x2+2,y1-2,x2+12,y1+42)),
                 ((x1-2,y2+2,x1+42,y2+12),(x1-12,y2-42,x1-2,y2+2)),
                 ((x2-42,y2+2,x2+2,y2+12),(x2+2,y2-42,x2+12,y2+2))]
        hits=0
        for strips in corners:
            supported=[]
            for box in strips:
                a=self.np.asarray(image.crop(box)).astype(int)
                supported.append(float(((a[:,:,0]>235)&(a[:,:,1]>95)&(a[:,:,1]<210)&(a[:,:,2]<140)).mean())>.12)
            # Both arms of an L must extend OUTSIDE the artwork. Orange art
            # inside a thumbnail or an adjacent selected row is not a marker.
            hits += all(supported)
        return hits>=3

    def can_return_to_list(self, image):
        """Require actual detail tab labels before pressing the global Back."""
        if not self.is_detail_page(image) or self.is_loading(image):return False
        panel=crop(image,[.10,.455,.90,.515])
        key=hashlib.sha256(panel.resize((300,40)).tobytes()).hexdigest()
        if key in self._return_cache:return self._return_cache[key]
        texts=''.join(''.join(unicodedata.normalize('NFKC',r['text']).split())
                      for r in self.read(panel) if r['score']>=.65)
        if len(self._return_cache)>32:self._return_cache.clear()
        self._return_cache[key]='プロデュース' in texts and 'ステージ' in texts
        return self._return_cache[key]

    def selected_list_cell(self, image, rows):
        selected=[(r,tile['column']) for r,row in enumerate(rows) for tile in row
                  if self.selection_matches(image,tile)]
        return selected[0] if len(selected)==1 else None

    def page_overlap(self, image, previous, rows, tolerance, *, anchor=None, current_anchor=None, full_page=False):
        """Correct small contour jitter before comparing the same owned rows.

        Re-sample the actual pixels with the saved rectangle size. Every cell
        in a row must pass the original threshold at the SAME small offset;
        independent best matches could join unrelated memories. Selection and
        unique contiguous overlap checks still apply to all candidates.
        """
        normalized = None
        np = self.np
        offsets = sorted(((dx,dy) for dx in range(-6,7) for dy in range(-6,7)),
                         key=lambda p:abs(p[0])+abs(p[1]))

        def aligned(a,b,limit):
            nonlocal normalized
            if rows_match(a,b,limit):
                return True
            if len(a)!=len(b) or not a or any(x['column']!=y['column'] for x,y in zip(a,b)):
                return False
            # This is a geometry correction, not a wider image-match threshold.
            if any(distance(x['signature'],y['signature'])>=24 for x,y in zip(a,b)):
                return False
            if normalized is None:normalized=self.normalized(image)
            samples=[]
            for saved,current in zip(a,b):
                if len(saved['signature'])!=24*16*3:
                    return False
                sx,sy,ex,ey=[round(v*(normalized.width if i%2==0 else normalized.height)) for i,v in enumerate(saved['box'])]
                x,y,_,_=[round(v*(normalized.width if i%2==0 else normalized.height)) for i,v in enumerate(current['box'])]
                samples.append((x,y,ex-sx,ey-sy,np.asarray(saved['signature'],dtype=np.int16)))
            for dx,dy in offsets:
                for x,y,w,h,reference in samples:
                    interior=normalized.crop((x+dx+22,y+dy+22,x+dx+w-22,y+dy+h-22))
                    actual=np.asarray(interior.resize((24,16)),dtype=np.int16).ravel()
                    if float(np.abs(actual-reference).mean())>=limit:
                        break
                else:
                    return True
            return False

        return overlap(previous,rows,tolerance,anchor=anchor,current_anchor=current_anchor,row_match=aligned,
                       counts=[len(previous)] if full_page else None)

    def is_detail(self, image):
        return not self.is_list(image) and self.orange(image, self.profile["stage_region"]) > 0.001

    def is_detail_page(self, image):
        """Recognize the detail tabs before choosing Stage (Produce may be active)."""
        return not self.is_list(image) and (
            self.orange(image,self.profile["stage_region"])>0.001 or
            self.orange(image,[0.12,0.455,0.49,0.505])>0.001)

    def is_loading(self, image):
        panel = crop(image,[0.78,0.86,0.97,0.97]).convert("RGB")
        pixels = self.np.asarray(panel)
        if float((pixels.min(2)>245).mean()) < 0.5:
            return False
        p = pixels.astype(int)
        if float(((p[:,:,0]>225)&(p[:,:,1]>60)&(p[:,:,1]<190)&(p[:,:,2]<150)).mean()) < 0.005:
            return False
        key = hashlib.sha256(panel.resize((100,100)).tobytes()).hexdigest()
        if key not in self._loading_cache:
            if len(self._loading_cache)>32:self._loading_cache.clear()
            self._loading_cache[key] = any(
                "LOADING" in "".join(unicodedata.normalize("NFKC",r["text"]).upper().split())
                and r["score"]>=0.65 for r in self.read(panel))
        return self._loading_cache[key]

    def at_detail_edge(self, image, bottom=False):
        # The lower pane's narrow scrollbar is independent of the list scrollbar.
        # Unknown/custom layouts fall back to movement-based endpoint checks.
        region = self.profile.get("detail_scrollbar", [0.981, 0.515, 0.996, 0.906])
        pixels = self.np.asarray(crop(image, region)).astype(int)
        dark = ((pixels.mean(2) > 90) & (pixels.mean(2) < 190)
                & ((pixels.max(2)-pixels.min(2)) < 30)).mean(1)
        rows = self.np.flatnonzero(dark > 0.2)
        if not 3 < len(rows) < len(dark)*0.95:
            return False
        margin = max(6, round(len(dark)*0.02))
        return bool(rows[-1] >= len(dark)-margin if bottom else rows[0] <= margin)

    def at_list_end(self, image):
        np = self.np
        pixels = np.asarray(crop(image, self.profile["list_scrollbar"])).astype(int)
        dark = ((pixels.mean(2) < 180) & ((pixels.max(2)-pixels.min(2)) < 20)).mean(1)
        rows = np.flatnonzero(dark > 0.2)
        return bool(len(rows) > 2 and len(rows) < len(dark)*0.8 and rows[-1] >= len(dark)-max(6,len(dark)*0.035))

    def at_list_start(self, image):
        np = self.np
        pixels = np.asarray(crop(image, self.profile["list_scrollbar"])).astype(int)
        dark = ((pixels.mean(2) < 180) & ((pixels.max(2)-pixels.min(2)) < 20)).mean(1)
        rows = np.flatnonzero(dark > 0.2)
        return bool(len(rows)>2 and len(rows)<len(dark)*0.8 and rows[0]<=max(6,len(dark)*0.05))

    def read(self, image, *, single_line=False):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            root = PROJECT_ROOT / "assets/ocr"
            name = "japan_PP-OCRv4_rec_mobile.onnx"
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            model = root / name
            if hashlib.sha256(model.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
                raise ValueError("本地日文模型校验失败。")
            self._ocr = RapidOCR(use_angle_cls=False, rec_model_path=str(model))
        previous = self._ocr.use_text_det
        try:
            if single_line:
                from PIL import ImageOps
                image = ImageOps.expand(image,border=10,fill='white')
                self._ocr.use_text_det = False
            results, _ = self._ocr(self.np.asarray(image))
        finally:
            self._ocr.use_text_det = previous
        return [{"text": text, "score": float(score)} for _, text, score in (results or [])]

    def production_only(self, image, tile):
        reads = self.read(crop(image, tile["box"]).resize((640, 360)))
        # Small ュ is frequently read as full-size ユ on the dim thumbnail.
        # Keep the complete label and confidence requirement; do not broadly
        # classify arbitrary unreadable or disabled memories as production-only.
        return any("プロデユース専用" in "".join(unicodedata.normalize("NFKC",row["text"]).split()).replace("ュ","ユ")
                   and row["score"] > 0.68 for row in reads)

    def details_enabled(self, image):
        return self.orange(image,self.profile["list_button_region"]) > 0.12

    def matches_list_page(self, image, rows, tolerance):
        """Re-sample every anchored tile, without rediscovering selected borders.

        Fixed positions are only used for the same page. Actual scrolling,
        reordering or changed content must still fail the pixel comparison.
        """
        if not rows or any(not row for row in rows):
            return False
        image = self.normalized(image)
        for row in rows:
            for tile in row:
                box = tile["box"]
                if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
                    return False
                x1,y1,x2,y2 = [round(v*(image.width if i%2==0 else image.height)) for i,v in enumerate(box)]
                if x2-x1 <= 44 or y2-y1 <= 44:
                    return False
                current = signature(image.crop((x1+22,y1+22,x2-22,y2-22)))
                if distance(current, tile["signature"]) >= tolerance:
                    return False
        return True

    def list_rows(self, image):
        """Find full thumbnail rows inside a calibrated viewport, then sample interiors.

        Infer missing border contours from the three-column grid, never from a
        content fingerprint. Identical-looking owned memories retain separate slots.
        """
        np, cv2 = self.np, self.cv2
        image = self.normalized(image)
        a = np.asarray(image)
        x1, y1, x2, y2 = [round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(self.profile["list_region"])]
        edge = cv2.Canny(cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), 40, 120)
        edge[:y1] = 0; edge[y2:] = 0; edge[:, :x1] = 0; edge[:, x2:] = 0
        edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(edge, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            # A thumbnail clipped by the sticky list header can still produce a
            # closed, wide contour. Only full 16:9 cards may anchor row matching;
            # a missing strip shifts the sample and destroys pagination overlap.
            if (290 < w < 355 and 165 < h < 205 and abs(h-w*9/16) <= 3
                    and y > y1 + 3 and y+h < y2-3):
                candidates.append((x, y, w, h))
        if not candidates:
            return []
        # Standard game grid has a 10px gap; derive origin/width from observed rectangles.
        width = float(np.median([c[2] for c in candidates]))
        # Clipped outer frames can expose an unrelated 16:9 rectangle *inside*
        # the artwork. It must agree with the actual thumbnail width as well.
        candidates = [c for c in candidates if abs(c[2]-width)<=6]
        if not candidates:return []
        width = float(np.median([c[2] for c in candidates]))
        height = float(np.median([c[3] for c in candidates]))
        origins = [x-round((x-50)/(width+10))*(width+10) for x, _, _, _ in candidates]
        left = float(np.median(origins))
        tops = []
        for _, y, _, _ in sorted(candidates, key=lambda c:c[1]):
            if not tops or abs(y-tops[-1]) > 20:
                tops.append(y)
        tops = complete_internal_rows(tops, height + 10)
        # Orange selection brackets can join all contours in an edge row.
        # Extend the regular grid only where two actual rectangular borders
        # corroborate it; never infer a clipped or blank row from pitch alone.
        def framed_row(y):
            hits = 0
            for column in range(3):
                x=round(left+column*(width+10)); right=round(x+width); bottom=round(y+height)
                sides=[edge[y+18:bottom-18,max(0,x-5):x+6].max(axis=1),
                       edge[y+18:bottom-18,right-5:right+6].max(axis=1),
                       edge[y-5:y+6,x+18:right-18].max(axis=0),
                       edge[bottom-5:bottom+6,x+18:right-18].max(axis=0)]
                supported=[float((side>0).mean())>.55 for side in sides]
                hits += supported[2] and supported[3] and (supported[0] or supported[1])
            return hits>=2
        for direction in (-1,1):
            for _ in range(3):
                y=round((tops[0] if direction<0 else tops[-1])+direction*(height+10))
                if y<=y1+5 or y+height>=y2-5 or not framed_row(y):
                    break
                if direction<0:tops.insert(0,y)
                else:tops.append(y)
        rows = []
        for y in tops:
            row = []
            for column in range(3):
                x = round(left+column*(width+10))
                box = (x, y, round(x+width), round(y+height))
                interior = image.crop((x+22, y+22, round(x+width-22), round(y+height-22)))
                pixels = np.asarray(interior)
                if float(pixels.std()) < 18 and float((pixels.max(2)-pixels.min(2) > 25).mean()) < 0.04:
                    continue  # blank cells in a final partial row
                row.append({"column": column, "box": [box[0]/image.width, box[1]/image.height, box[2]/image.width, box[3]/image.height],
                            "point": [(box[0]+box[2])/2/image.width, (box[1]+box[3])/2/image.height],
                            "signature": signature(interior)})
            if row:
                rows.append(row)
        return rows

    def cards(self, image):
        from gkshot.recognition.card_frames import complete_card_frames, complete_card_grid
        np, cv2 = self.np, self.cv2
        image = self.normalized(image)
        pixels = np.asarray(image).astype(int)
        mask = ((pixels.max(2)-pixels.min(2) > 40) & (pixels.max(2) > 150)).astype(np.uint8)*255
        mask[:round(image.height*0.51)] = 0
        mask[image.height-170:] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        found = []
        for x, y, w, h, area in cv2.connectedComponentsWithStats(mask)[2][1:]:
            if 190 < w < 235 and 190 < h < 235 and area > 10000 and y+h < image.height-173:
                found.append((int(x), int(y), int(w), int(h)))
        found=complete_card_frames(image,found,top=round(image.height*.51),bottom=image.height-170)
        grid = complete_card_grid(image,found,at_bottom=self.at_detail_edge(image,bottom=True),
                                  read_heading=lambda im:self.read(im,single_line=True))
        if grid is None:return []
        return [{"slot": slot+1, "box": [x/image.width,y/image.height,(x+w)/image.width,(y+h)/image.height],
                 "point": [(x+w/2)/image.width,(y+h/2)/image.height], "card_count":grid['count']}
                for slot,(x,y,w,h) in enumerate(grid['boxes'])]

    def customization_badge(self, image, card):
        """Use the same badge rule as offline recognition when selecting tooltips."""
        from gkshot.recognition.customization_badge import detect_badge
        if "box" not in card:
            return {"state":"unknown", "rule":"pc-green-counter-v2", "reason":"missing-card-box"}
        return detect_badge(crop(image, card["box"]), self.np)

    def tooltip(self, image):
        np, cv2 = self.np, self.cv2
        normalized = self.normalized(image)
        pixels = np.asarray(normalized)
        mask = (pixels.min(2) > 252).astype(np.uint8)*255
        boxes = []
        for x, y, w, h, area in cv2.connectedComponentsWithStats(mask)[2][1:]:
            if (350 < w < 750 and 100 < h < normalized.height*0.85 and area > w*h*0.5
                    and x > 3 and y > 3 and x+w < normalized.width-4 and y+h < normalized.height-4):
                panel = normalized.crop((x,y,x+w,y+h))
                reads = self.read(panel)
                if len(reads) >= 2 and any(row["score"] >= 0.75 for row in reads):
                    boxes.append({"box": [x/normalized.width,y/normalized.height,(x+w)/normalized.width,(y+h)/normalized.height],
                                  "reads": reads, "check": "bounded-panel-with-text"})
        return boxes[0] if len(boxes) == 1 else None
