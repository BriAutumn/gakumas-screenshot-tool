"""Offline recognition of an explicit screenshot layout. Always returns a review draft."""
import hashlib
import json
import re
from pathlib import Path

from gkshot.catalog import Catalog, PROJECT_ROOT


class ScreenshotScanner:
    def __init__(self, layout_path: Path | None = None, catalog: Catalog | None = None):
        # Optional dependencies stay out of the existing stdlib-only URL command.
        import numpy as np
        import onnxruntime as ort
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR

        self.np, self.Image = np, Image
        self.catalog = catalog or Catalog()
        self.layout = json.loads((layout_path or PROJECT_ROOT / "configs/layouts/contest-crop-v1.json").read_text(encoding="utf-8"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.card_model = ort.InferenceSession(str(self.catalog.verified_file("skill_card_model.onnx")), options,
                                              providers=["CPUExecutionProvider"])
        self.card_classes = json.loads(self.catalog.verified_file("skill_card_classes.json").read_text())
        self.ocr = RapidOCR(use_text_det=False, use_angle_cls=False)
        self.item_ids, templates = [], []
        for name in sorted(self.catalog.manifest["files"]):
            if name.startswith("pitem-icons/"):
                entity_id = int(Path(name).stem)
                if self.catalog.p_items.get(entity_id, {}).get("mode") != "stage":
                    continue
                with Image.open(self.catalog.verified_file(name)) as image:
                    templates.append(np.asarray(image.convert("RGB").resize((48, 48)), dtype=np.float32))
                self.item_ids.append(entity_id)
        self.item_templates = np.stack(templates)
        references = []
        for entity_id in (592, 593):
            with Image.open(self.catalog.verified_file(f"reference-cards/{entity_id}.png")) as image:
                references.append(np.asarray(image.convert("RGB").resize((64, 64)), dtype=np.float32)[23:43, 44:64])
        self.plus_template = references[1]
        self.plus_mask = np.max(abs(references[1] - references[0]), axis=2) > 70

    def read_number(self, crop, signed=False):
        from PIL import ImageOps
        np = self.np
        enlarged = crop.resize((crop.width * 4, crop.height * 4))
        reads = []
        for variant in (enlarged, ImageOps.expand(enlarged, border=12, fill="white")):
            result, _ = self.ocr(np.asarray(variant))
            if result and len(result) == 1:
                text, confidence = result[0][1], float(result[0][2])
                pattern = r"[+-]\d{1,5}" if signed else r"\d{1,5}"
                if re.fullmatch(pattern, text) and confidence >= 0.6:
                    reads.append({"text": text, "value": int(text), "score": round(confidence, 4)})
        values = {r["value"] for r in reads}
        return {"value": next(iter(values)) if len(values) == 1 else None, "reads": reads}

    def classify_card(self, crop):
        np = self.np
        pixels = np.asarray(crop.resize((64, 64), self.Image.Resampling.BILINEAR), dtype=np.float32)
        tensor = pixels.transpose(2, 0, 1)[None] / 255
        logits = self.card_model.run(None, {self.card_model.get_inputs()[0].name: tensor})[0][0]
        probabilities = np.exp(logits - logits.max())
        probabilities /= probabilities.sum()
        by_id = {}
        for label, probability in zip(self.card_classes, probabilities):
            entity_id = int(label.split("_")[0])
            by_id[entity_id] = by_id.get(entity_id, 0.0) + float(probability)
        candidates = [{"id": entity_id, "score": round(score, 5),
                       "name": self.catalog.label("skill_cards", entity_id)}
                      for entity_id, score in sorted(by_id.items(), key=lambda pair: -pair[1])[:5]]
        # The shared '+' glyph is isolated by differencing two upstream reference icons.
        # Only strong positive evidence may promote a base card to its matching upgraded candidate.
        glyph_pixels = np.asarray(crop.resize((64, 64)), dtype=np.float32)
        glyph_error = min(float(((glyph_pixels[23 + dy:43 + dy, 44 + dx:64 + dx][self.plus_mask]
                                  - self.plus_template[self.plus_mask]) ** 2).mean())
                          for dy in range(-3, 4) for dx in range(-3, 1))
        selected = candidates[0]["id"]
        card = self.catalog.skill_cards.get(selected, {})
        method = "onnx"
        if glyph_error < 3000 and card and not card["upgraded"]:
            for candidate in candidates[1:]:
                other = self.catalog.skill_cards.get(candidate["id"], {})
                if (other.get("name") == card["name"] + "+" and
                        other.get("pIdolId") == card["pIdolId"] and candidate["score"] > 0.05):
                    selected, method = candidate["id"], "onnx-plus-glyph"
                    break
        return {"id": selected, "method": method, "candidates": candidates,
                "plus_glyph_distance": round(glyph_error, 2), "needs_review": True}

    def classify_item(self, crop):
        np = self.np
        pixels = np.asarray(crop.resize((48, 48)), dtype=np.float32)
        interior = pixels[8:40, 8:40]
        # A confirmed empty slot is gray and almost uniform, not an arbitrary low score.
        if float(interior.std()) < 8 and float((interior.max(2) - interior.min(2)).mean()) < 6:
            return {"id": 0, "method": "empty-slot", "candidates": [], "needs_review": True}
        differences = (self.item_templates - pixels) ** 2
        errors = differences[:, 8:40, 8:40].mean((1, 2, 3)) + 0.2 * differences.mean((1, 2, 3))
        best = np.argsort(errors)[:5]
        candidates = [{"id": self.item_ids[i], "distance": round(float(errors[i]), 2),
                       "name": self.catalog.label("p_items", self.item_ids[i])} for i in best]
        return {"id": candidates[0]["id"], "method": "template", "candidates": candidates,
                "needs_review": True}

    def duplicate_match(self, crop, main_crops):
        np = self.np
        pixels = np.asarray(crop.resize((64, 64)), dtype=np.float32)
        # Gray duplicate mask darkens the top-right badge. Require it as well as art similarity.
        badge = pixels[2:17, 30:60]
        if float(badge.mean()) > 215 or float((np.ptp(badge, axis=2) < 12).mean()) < 0.85:
            return None
        vector = pixels[22:45, 14:46].ravel()
        if vector.std() < 5:
            return None
        scores = []
        for main in main_crops:
            other = np.asarray(main.resize((64, 64)), dtype=np.float32)[22:45, 14:46].ravel()
            scores.append(float(np.corrcoef(vector, other)[0, 1]) if other.std() >= 5 else -1)
        order = np.argsort(scores)
        if scores[order[-1]] > 0.88 and scores[order[-1]] - scores[order[-2]] > 0.2:
            return int(order[-1]), round(scores[order[-1]], 5)
        return None

    def scan(self, screenshot: Path, *, stage_id=None, support_bonus=None) -> dict:
        np, Image = self.np, self.Image
        raw = screenshot.read_bytes()
        with Image.open(screenshot) as source:
            if source.width * source.height > 30_000_000:
                raise ValueError("图片过大，请裁剪到编成面板。")
            width, height = source.size
            ref_w, ref_h = self.layout["reference_size"]
            if min(width, height) < 250 or abs((width / height) / (ref_w / ref_h) - 1) > self.layout["aspect_tolerance"]:
                raise ValueError("当前仅支持与示例相同裁剪范围的编成截图（532×387 或等比例缩放）。请先裁剪图片。")
            image = source.convert("RGB").resize((ref_w, ref_h), Image.Resampling.LANCZOS)
        numbers = {name: self.read_number(image.crop(box)) for name, box in self.layout["parameters"].items()}
        if sum(result["value"] is not None for result in numbers.values()) < 3:
            raise ValueError("未找到足够的属性数字，截图可能不符合当前布局；请按示例裁剪。")
        deltas = {name: self.read_number(image.crop(box), signed=True) for name, box in self.layout["deltas"].items()}
        item_crops = [image.crop(box) for box in self.layout["items"]]
        # Color evidence prevents a blank/largely unrelated image from becoming a draft.
        main_crops = [image.crop(box) for box in self.layout["cards"][0]]
        if sum(float(np.asarray(c).std()) > 20 for c in main_crops) < 3:
            raise ValueError("未找到卡片图像；请检查截图裁剪范围。")
        cards = [[self.classify_card(image.crop(box)) for box in row] for row in self.layout["cards"]]
        for index, box in enumerate(self.layout["cards"][1]):
            match = self.duplicate_match(image.crop(box), main_crops)
            if match:
                source, similarity = match
                cards[1][index].update(id=cards[0][source]["id"], method="same-image-duplicate",
                                       duplicate_of=source + 1, similarity=similarity)
        items = [self.classify_item(crop) for crop in item_crops]
        return {
            "report_version": 1, "status": "needs-review", "image_sha256": hashlib.sha256(raw).hexdigest(),
            "image_path": str(screenshot.resolve()), "image_size": [width, height],
            "layout_id": self.layout["id"], "recognition_revision": self.catalog.manifest["revision"],
            "numbers": numbers, "displayed_deltas": deltas, "cards": cards, "items": items,
            "warnings": [
                "结果是待核对草稿；模型分数不是经过校准的正确率。",
                "属性草稿读取黑色数字，不叠加旁边红蓝差值；导出前请核对数值含义。",
                "仅导入普通道具行；固定道具区域由所选场地规则处理，请核对场地。",
                "自定义效果和强化角标尚不能可靠识别，必须逐项核对；未知效果不会默认为无。",
                "仅验证过示例裁剪布局和其等比例缩放，其他界面需要新的布局配置。"
            ],
            "draft": {
                "schema_version": 1, "stage_id": stage_id, "support_bonus": support_bonus,
                "parameters": {name: result["value"] for name, result in numbers.items()},
                "item_ids": [item["id"] for item in items],
                "card_ids": [[card["id"] for card in row] for row in cards],
                "customizations": [[None] * 6 for _ in range(2)]
            }
        }
