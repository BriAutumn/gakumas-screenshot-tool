"""Explicit candidate comparison; no combination search or library writes."""
import hashlib
import json
import math
import os
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from gkshot.adapters.local_engine import LocalSimulator, SimulationCancelled, SimulationError
from gkshot.models import Loadout
from gkshot.catalog import Catalog, PROJECT_ROOT
from gkshot.memory_library import validate_memory


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding="utf-8", newline='\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def summary(scores):
    n = len(scores)
    mean = statistics.fmean(scores)
    sd = statistics.stdev(scores) if n > 1 else None
    se = sd / math.sqrt(n) if sd is not None else None
    return {"trials": n, "mean": mean, "sample_stddev": sd, "standard_error": se,
            "min": min(scores), "max": max(scores), "median": statistics.median(scores)}


def read_ready_memory(library, memory_id):
    """SQLite URI mode=ro prevents initialization, migrations, or accidental writes."""
    database = Path(library).resolve() / "library.sqlite3"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT status,report_json,review_json FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
    except sqlite3.Error as error:
        raise ValueError(f'无法只读打开记忆库 {database}：{error}') from error
    if row is None or row["status"] not in ("auto-resolved", "reviewed"):
        raise ValueError(f"记忆 {memory_id} 不存在或尚未确认可用")
    if row["status"] == "reviewed":
        review = json.loads(row["review_json"] or "null")
        if not review or review.get("confirmed") is not True:
            raise ValueError("人工审核记录缺少确认")
        return review["draft"]
    return json.loads(row["report_json"])["draft"]


def compose_memories(main, sub, *, stage_id, support_bonus, catalog=None, idols=None):
    catalog = catalog or Catalog()
    main, sub = validate_memory(main, catalog), validate_memory(sub, catalog)
    if idols is None:
        runtime = PROJECT_ROOT / 'runtime/node'
        manifest = json.loads((runtime / 'manifest.json').read_text(encoding='utf-8'))
        relative = 'packages/gakumas-data/json/p_idols.json'
        raw = (runtime / 'upstream' / relative).read_bytes()
        if (hashlib.sha256(raw).hexdigest() != manifest['files'][relative]
            or catalog.manifest['revision'] != manifest['revision']):
            raise ValueError('记忆目录和模拟数据的版本不匹配')
        idols = {p['id']: p for p in json.loads(raw)}
    first, second = idols[main['p_idol_id']], idols[sub['p_idol_id']]
    if first['idolId'] != second['idolId'] or first['plan'] != second['plan']:
        raise ValueError('主副记忆必须属于相同角色和计划；同角色不同版本可以组合')
    # Mirrors upstream LoadoutContext.setMemory and getMemoryParamContribution.
    loadout = Loadout.from_dict({"schema_version": 1, "stage_id": stage_id,
        "support_bonus": support_bonus,
        "parameters": {key: main["parameters"][key] + sub["parameters"][key] // 5
                       for key in ("vocal", "dance", "visual", "stamina")},
        "item_ids": main["item_ids"], "card_ids": [main["card_ids"], sub["card_ids"]],
        "customizations": [main["customizations"], sub["customizations"]]})
    catalog.validate_loadout(loadout)
    return loadout


def list_ready_memories(library):
    database = Path(library).resolve() / 'library.sqlite3'
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute("SELECT memory_id FROM memories WHERE status IN ('auto-resolved','reviewed') ORDER BY created_at").fetchall()
    except sqlite3.Error as error:
        raise ValueError(f'无法只读打开记忆库 {database}：{error}') from error
    return [(row[0], read_ready_memory(library, row[0])) for row in rows]


def compare(candidates, *, trials=100, seed=610397104, timeout=300, cache=None,
            cancel=None, progress=None, on_result=None, simulator=None):
    candidates = list(candidates)
    if not candidates:
        raise ValueError("至少需要一个候选编成")
    if len({name for name, _ in candidates}) != len(candidates):
        raise ValueError("候选名称不可重复")
    if len({loadout.stage_id for _, loadout in candidates}) != 1:
        raise ValueError("比较的候选必须使用相同场地；请按场分别运行")
    if type(trials) is not int or not 1 <= trials <= 100000:
        raise ValueError("trials must be 1..100000")
    if type(seed) is not int or not 0 <= seed <= 0xffffffff:
        raise ValueError("seed must be uint32")
    simulator = simulator or LocalSimulator()
    identity = simulator.identity()
    report = {"schema_version": 1, "status": "complete", "stage_id": candidates[0][1].stage_id,
              "trials": trials, "seed": seed, "engine_revision": identity["revision"],
              "results": [], "ranking": [],
              "note": "按样本均值排序，标准差表示单次波动；相同种子不保证不同编成获得相同抽牌，排名不是显著性结论。"}
    for name, loadout in candidates:
        if cancel and cancel.is_set():
            report["status"] = "cancelled"
            break
        inputs = {"loadout": loadout.to_dict(), "trials": trials, "seed": seed, "identity": identity}
        key = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path = Path(cache) / (key + ".json") if cache else None
        hit = False
        try:
            result = None
            if path and path.is_file():
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                    raw = stored["result"]
                    checksum = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
                    if (stored["key"] == key and stored["checksum"] == checksum
                        and len(raw["scores"]) == trials and raw["seed"] == seed
                        and raw["engine_revision"] == identity["revision"]
                        and all(type(x) in (int, float) and math.isfinite(x) for x in raw["scores"])):
                        result, hit = raw, True
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if result is None:
                result = simulator.simulate(loadout, trials=trials, seed=seed, timeout=timeout, cancel=cancel,
                    progress=(lambda done, total: progress(name, done, total)) if progress else None)
                if path:
                    raw = dict(result)
                    raw.pop("request_id", None)
                    atomic_json(path, {"key": key, "result": raw,
                        "checksum": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()})
            elif progress:
                progress(name, trials, trials)
            entry = {"name": name, "status": "ok", "cached": hit, "loadout": loadout.to_dict(),
                     "summary": summary(result["scores"]), "simulation": result}
        except SimulationCancelled:
            report["status"] = "cancelled"
            break
        except (SimulationError, ValueError, OSError) as error:
            entry = {"name": name, "status": "error", "error": str(error), "loadout": loadout.to_dict()}
            report["status"] = "completed-with-errors"
        report["results"].append(entry)
        if on_result:
            on_result(entry)
    report["ranking"] = [r["name"] for r in sorted(
        (r for r in report["results"] if r["status"] == "ok"),
        key=lambda r: r["summary"]["mean"], reverse=True)]
    return report
