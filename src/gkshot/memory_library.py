"""Owned memories, not simulator loadouts. SQLite + content-addressed source images."""
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gkshot.app_paths import USER_ROOT
PROJECT_ROOT = USER_ROOT
from gkshot.models import _integer, _keys, _slots
from gkshot.paths import io_path

DEFAULT_LIBRARY = PROJECT_ROOT / "data/memory-library"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_memory(draft, catalog):
    _keys(draft, {"parameters", "item_ids", "card_ids", "customizations", "p_idol_id"}, "memory")
    _keys(draft["parameters"], {"vocal", "dance", "visual", "stamina"}, "parameters")
    for key, value in draft["parameters"].items(): _integer(value, key)
    for value in _slots(draft["item_ids"], 4, "item_ids"): _integer(value, "item ID")
    for value in _slots(draft["card_ids"], 6, "card_ids"): _integer(value, "card ID")
    for token in _slots(draft["customizations"], 6, "customizations"):
        if not isinstance(token, str):
            raise ValueError("请核对已有卡片的自定义效果；未知不能视为没有，空槽应留空效果。")
    p_idol_id = _integer(draft["p_idol_id"], "所属偶像版本", 1)
    known = {card["pIdolId"] for card in catalog.skill_cards.values() if card["pIdolId"]}
    if p_idol_id not in known:
        raise ValueError("所属偶像版本不在当前目录内。")
    own_cards = {catalog.skill_cards[value]["pIdolId"] for value in draft["card_ids"]
                 if value in catalog.skill_cards and catalog.skill_cards[value]["pIdolId"]}
    if own_cards and own_cards != {p_idol_id}:
        raise ValueError("记忆所属偶像版本与角色固有卡不一致，请核对。")
    catalog.validate_entities(draft["item_ids"], [draft["card_ids"]], [draft["customizations"]])
    return copy.deepcopy(draft)


class MemoryLibrary:
    def __init__(self, root: Path = DEFAULT_LIBRARY):
        self.root = Path(root).resolve()
        io_path(self.root).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("此记忆库版本比当前程序新，拒绝修改。")
            db.execute("""CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY, capture_key TEXT NOT NULL UNIQUE,
                content_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL, report_json TEXT NOT NULL, review_json TEXT
            )""")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(io_path(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

