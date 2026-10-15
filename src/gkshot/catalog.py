"""Pinned catalogs for names and reviewed-export validation; no simulation code."""
import hashlib
import json
import re
from pathlib import Path

from gkshot.app_paths import RESOURCE_ROOT as PROJECT_ROOT
from gkshot.resource_paths import RECOGNITION_ROOT
ASSETS = RECOGNITION_ROOT


class Catalog:
    def __init__(self, root: Path = ASSETS):
        self.root = root
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for name in ("skill_cards", "p_items", "stages", "customizations"):
            rows = json.loads(self.verified_file(name + ".json").read_text(encoding="utf-8"))
            setattr(self, name, {row["id"]: row for row in rows})

    def verified_file(self, name: str) -> Path:
        record = self.manifest["files"].get(name)
        if record is None:
            raise ValueError(f"Untracked recognition asset: {name}")
        path = self.root / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Recognition asset hash mismatch: {name}")
        return path

    def label(self, kind: str, entity_id: int) -> str:
        if entity_id == 0:
            return "0 · 空槽"
        row = getattr(self, kind).get(entity_id)
        return f"{entity_id} · {row['name'] if row else '未知 ID'}"

    def validate_loadout(self, loadout):
        stage = self.stages.get(loadout.stage_id)
        if not stage or stage["type"] != "contest":
            raise ValueError("请选择数据目录中已有的普通竞技场场地；暂不支持联赛和自定义场地。")
        self.validate_entities(loadout.item_ids, loadout.card_ids, loadout.customizations)

    def validate_entities(self, item_ids, card_groups, customization_groups):
        for entity_id in item_ids:
            if entity_id and (entity_id not in self.p_items or self.p_items[entity_id]["mode"] != "stage"):
                raise ValueError(f"道具 {entity_id} 不存在或不适用于舞台。")
        for cards, tokens in zip(card_groups, customization_groups):
            for entity_id, token in zip(cards, tokens):
                if entity_id and entity_id not in self.skill_cards:
                    raise ValueError(f"未知卡片 ID：{entity_id}")
                if not token:
                    continue
                if not entity_id or not re.fullmatch(r"\d+x[1-9]\d*(?:e\d+x[1-9]\d*)*", token):
                    raise ValueError("卡片自定义效果格式不正确。")
                allowed = self.skill_cards[entity_id]["availableCustomizations"].split(",")
                seen = set()
                for part in token.split("e"):
                    effect, level = map(int, part.split("x"))
                    if effect in seen or str(effect) not in allowed or effect not in self.customizations:
                        raise ValueError(f"卡片 {entity_id} 不支持自定义效果 {effect}，或该效果重复。")
                    if level > self.customizations[effect]["max"]:
                        raise ValueError(f"自定义效果 {effect} 的等级超出上限。")
                    seen.add(effect)
