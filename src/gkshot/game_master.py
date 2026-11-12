"""Read a complete, immutable game master snapshot exported by the plugin."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


TABLES = (
    "ProduceCard", "ProduceItem", "IdolCard", "ProduceCardCustomize",
    "ProduceCardGrowEffect", "ProduceExamEffect", "ProduceDescriptionProduceCardGrowEffect",
    "ProduceExamTrigger", "Character",
)
KEYS = {
    "ProduceCard": ("id", "upgradeCount"),
    "ProduceCardCustomize": ("id", "customizeCount"),
    "ProduceDescriptionProduceCardGrowEffect": ("type",),
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_TABLE_BYTES = 128 << 20


class GameMasterError(RuntimeError):
    pass


def snapshot_id(files):
    names = (name + ".json" for name in TABLES)
    manifest = "".join(f"{name} {files[name]}\n" for name in names)
    return hashlib.sha256(manifest.encode("ascii")).hexdigest()


class GameMasterSnapshot:
    def __init__(self, user_home, revision=None):
        root = Path(user_home) / "native-export" / "game-master"
        path = root / "active.json" if revision is None else root / "snapshots" / revision / "snapshot.json"
        try:
            manifest = json.loads(path.read_bytes())
        except FileNotFoundError as error:
            raise GameMasterError("未找到游戏主数据。请更新游戏插件，重启游戏并进入主界面，等导出完成后再同步。") from error
        except (OSError, ValueError) as error:
            raise GameMasterError(f"游戏主数据清单无法读取：{error}") from error
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise GameMasterError("游戏主数据清单版本不受支持，请更新插件并重新导出。")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != {name + ".json" for name in TABLES} or any(
            not isinstance(digest, str) or not SHA256.fullmatch(digest) for digest in files.values()
        ):
            raise GameMasterError("游戏主数据清单不完整，请重新导出。")
        self.revision = snapshot_id(files)
        if manifest.get("snapshot") != self.revision or (revision is not None and revision != self.revision):
            raise GameMasterError("游戏主数据清单校验失败，请重新导出。")
        self.files = files
        self.directory = root / "snapshots" / self.revision

    def read(self, table):
        name = table + ".json"
        path = self.directory / name
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_TABLE_BYTES + 1)
            if len(raw) > MAX_TABLE_BYTES or hashlib.sha256(raw).hexdigest() != self.files[name]:
                raise GameMasterError(f"游戏主数据 {name} 校验失败，请重新导出。")
            rows = json.loads(raw)
        except (OSError, ValueError) as error:
            raise GameMasterError(f"游戏主数据 {name} 无法读取：{error}") from error
        if not isinstance(rows, list) or not rows:
            raise GameMasterError(f"游戏主数据 {name} 为空，请重新导出。")
        fields = KEYS.get(table, ("id",))
        keys = set()
        for row in rows:
            if not isinstance(row, dict):
                raise GameMasterError(f"游戏主数据 {name} 含非对象记录。")
            key = tuple(row.get(field) for field in fields)
            if any(not isinstance(value, str) or not value for value in key[:1]) or (
                len(key) > 1 and (type(key[1]) is not int or key[1] < 0)
            ):
                raise GameMasterError(f"游戏主数据 {name} 含非法主键：{key!r}。")
            if key in keys:
                raise GameMasterError(f"游戏主数据 {name} 含重复主键：{key!r}。")
            keys.add(key)
        return rows
