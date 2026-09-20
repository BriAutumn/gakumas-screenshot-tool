"""Owned memories, not simulator loadouts. SQLite + content-addressed source images."""
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gkshot.catalog import PROJECT_ROOT
from gkshot.models import _integer, _keys, _slots
from gkshot.paths import io_path

from gkshot.app_paths import USER_ROOT
DEFAULT_LIBRARY = USER_ROOT / "data/memory-library"


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


def assess_automatic(report, catalog):
    """Resolve complete machine evidence without inventing a human review."""
    draft = report["draft"]
    from gkshot.recognition.native_resolution import card_accepted, item_accepted
    def confirmed_empty(slot):
        for page in report['pages']:
            grid=page.get('card_grid') or {}
            count=grid.get('count')
            if (grid.get('rule')=='pc-packed-card-grid-v1' and type(count) is int and 1<=count<6
                    and grid.get('empty_slots')==list(range(count,6)) and slot in grid['empty_slots']
                    and grid.get('bottom_confirmed') and grid.get('heading_confirmed') and grid.get('trailing_blank_confirmed')
                    and sorted(c['slot'] for c in page['cards'])==list(range(count))):
                return True
        return False
    issues = set(report.get("conflicts",[]) + report.get("missing_fields",[]))
    for i,token in enumerate(draft["customizations"]):
        if token is None: issues.add(f"customizations.{i}")
        else:
            evidence=report.get("evidence",{}).get(f"customizations.{i}",[])
            supported=any(e.get("value")==token and (
                e.get("method")=="tooltip-rule-match" or
                (token=="" and e.get("method")=="catalog-no-options") or
                (token=="" and draft['card_ids'][i]==0 and e.get('method')=='confirmed-empty-card-slot' and confirmed_empty(i)) or
                (token=="" and e.get("method")=="central-green-counter" and e.get("badge",{}).get("state")=="absent")
                ) for e in evidence)
            if not supported:
                from gkshot.recognition.vision_evidence import METHOD, supported as vision_supported
                supported=any(e.get('method')==METHOD and vision_supported(report,e,i,catalog) for e in evidence)
            if not supported:issues.add(f"customizations.{i}")
    for key,value in draft["parameters"].items():
        reads = [p.get("number_reads",{}).get(key,{}) for p in report["pages"] if p.get("parameters")]
        if not any(len(r.get("reads",[])) >= 2 and all(x.get("value")==value and x.get("score",0)>=0.55
                   for x in r["reads"]) for r in reads):
            issues.add(f"parameters.{key}")
    for slot,card_id in enumerate(draft["card_ids"]):
        observations = [c for p in report["pages"] for c in p["cards"] if c["slot"]==slot and c["id"]==card_id]
        accepted = (card_id==0 and confirmed_empty(slot)) or any(card_accepted(o) for o in observations)
        if not accepted: issues.add(f"card_ids.{slot}")
    for slot,item_id in enumerate(draft["item_ids"]):
        observations = [p["items"][slot] for p in report["pages"] if p.get("items")]
        accepted = False
        for item in observations:
            candidates=item.get("candidates",[])
            if item["id"] != item_id: continue
            if item.get('method') in ('native-template-and-upgrade-mark','native-single-template'):
                accepted |= item_accepted(item)
                continue
            if item_id==0 and item.get("method")=="empty-slot": accepted=True
            elif item.get("method")=="template-and-upgrade-mark":
                e=item.get("variant_evidence",{})
                accepted |= (e.get("rule") in ("pitem-corner-v1", "pitem-corner-v2") and e.get("id")==item_id
                    and e.get("pixel_count",0)>=25 and e.get("alignment_distance",99999)<1200
                    and e.get("distance",99999)<2000
                    and e.get("distance",99999)<e.get("alternative_distance",0)*0.35
                    and e.get("family_distance",99999)<1200
                    and e.get("family_distance",99999)<e.get("other_family_distance",0)*0.7)
            elif len(candidates)>=2 and candidates[0]["id"]==item_id:
                best,second=candidates[0]["distance"],candidates[1]["distance"]
                accepted |= best < 1200 and best < second*0.9
        if not accepted: issues.add(f"item_ids.{slot}")
    # Independently authored image readings can resolve specific uncertain
    # fields. Missing pages and explicit record conflicts remain blockers.
    from gkshot.recognition.vision_fields import METHOD as FIELD_VISION, supported as field_supported
    for field in list(issues):
        if field in report.get('conflicts',[]) or field in report.get('missing_fields',[]):continue
        if any(e.get('method')==FIELD_VISION and field_supported(report,e,field,catalog)
               for e in report.get('evidence',{}).get(field,[])):
            issues.remove(field)
    validation_errors = []
    try:
        validate_memory(draft,catalog)
    except (ValueError,TypeError,KeyError) as error:
        issues.add("loadout-validation")
        validation_errors.append(str(error))
    report["automatic_check"]={"rule":"memory-complete-v1", "unresolved":sorted(issues),
                              "validation_errors":validation_errors}
    if not issues: return "auto-resolved"
    return "incomplete" if report.get("conflicts") or report.get("missing_fields") else "needs-review"


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

    def archive(self, source):
        path = io_path(source["source_path"])
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != source["sha256"]:
            raise ValueError("截图在识别后发生变化，已取消入库：" + path.name)
        # Keep original bytes, infer no identity from filenames. Pillow can decode by file signature.
        suffix = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if data.startswith(b"\xff\xd8") else ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ".image"
        relative = Path("sources") / f"{digest}{suffix}"
        target = io_path(self.root / relative)
        target.parent.mkdir(exist_ok=True)
        try:
            with target.open("xb") as stream: stream.write(data)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("记忆库中的原图损坏：" + target.name)
        return relative.as_posix()

    def import_report(self, report, *, capture_id=None):
        if report.get("kind") != "owned-memory" or report.get("report_version") != 1:
            raise ValueError("需要独立记忆的识别报告，不能导入竞技场编成报告。")
        if report["status"] not in ("incomplete", "needs-review"):
            raise ValueError("识别报告不能直接声称已核对。")
        report = copy.deepcopy(report)
        from gkshot.catalog import Catalog
        report["status"] = assess_automatic(report,Catalog())
        capture_key = fingerprint({"pages": sorted(p["sha256"] for p in report["pages"]), "capture_id": capture_id})
        for source in report["pages"] + ([report["list_context"]] if report.get("list_context") else []) + report.get('identity_sources',[]):
            source["archive_path"] = self.archive(source)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM memories WHERE capture_key=?", (capture_key,)).fetchone()
            if existing:
                # Adding a location image must not create another owned copy or discard human corrections.
                old_report = json.loads(existing["report_json"])
                if not old_report.get("list_context") and report.get("list_context"):
                    old_report["list_context"] = report["list_context"]
                    db.execute("UPDATE memories SET report_json=?,updated_at=? WHERE memory_id=?",
                               (json.dumps(old_report, ensure_ascii=False), now, existing["memory_id"]))
                return {"memory_id": existing["memory_id"], "created": False, "status": existing["status"]}
            memory_id = str(uuid4())
            content_key = fingerprint(report["draft"])
            similar = [row[0] for row in db.execute("SELECT memory_id FROM memories WHERE content_key=?", (content_key,))]
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,NULL)",
                       (memory_id, capture_key, content_key, now, now, report["status"], json.dumps(report, ensure_ascii=False)))
        return {"memory_id": memory_id, "created": True, "status": report["status"], "similar_memory_ids": similar}

    def get(self, memory_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        if row is None:
            raise ValueError("找不到这份记忆。")
        result = dict(row)
        result["report"] = json.loads(result.pop("report_json"))
        result["review"] = json.loads(result.pop("review_json") or "null")
        return result

    def list(self, *, reviewed_only=False, ready_only=False):
        with self.connect() as db:
            condition = " WHERE status='reviewed'" if reviewed_only else " WHERE status IN ('reviewed','auto-resolved')" if ready_only else ""
            query = "SELECT memory_id FROM memories" + condition + " ORDER BY created_at"
            ids = [row[0] for row in db.execute(query)]
        return [self.get(memory_id) for memory_id in ids]

    def save_review(self, memory_id, draft, *, confirmed, catalog):
        if not confirmed:
            raise ValueError("请先核对这份记忆的身份、数值、道具、卡片和自定义效果。")
        draft = validate_memory(draft, catalog)
        self.get(memory_id)
        now = datetime.now(timezone.utc).isoformat()
        review = {"draft": draft, "confirmed": True, "reviewed_at": now}
        with self.connect() as db:
            db.execute("UPDATE memories SET status='reviewed',review_json=?,content_key=?,updated_at=? WHERE memory_id=?",
                       (json.dumps(review, ensure_ascii=False), fingerprint(draft), now, memory_id))

    def add_card_detail(self, memory_id, detail, *, catalog, slot=None):
        """Attach a tooltip to an explicitly selected owned memory; never merge owned copies.

        The recognition caller supplies pixel-derived evidence. Successful matches update
        only the customization field; existing human reviews remain authoritative.
        """
        from gkshot.recognition.card_detail import canonical, match_rule
        from PIL import Image
        if detail.get("kind") != "card-detail" or detail.get("report_version") != 1:
            raise ValueError("需要卡片浮动详情识别报告。")
        record = self.get(memory_id)
        report = record["report"]
        if any(canonical(str(detail["identity"][key])) != canonical(str(report["identity"][key]))
               for key in ("title", "idol_name", "power")):
            raise ValueError("卡片详情的记忆页头与选中记录不一致，未绑定。")
        if detail["recognition_revision"] != catalog.manifest["revision"]:
            raise ValueError("详情报告的数据版本与本地目录不一致，请重新识别。")
        # An OCR name/power collision alone cannot establish ownership.
        source = report["pages"][0]
        with Image.open(self.source_path(source)) as image:
            frame = source["frame"]
            normalized = image.convert("RGB").crop((frame["left"], frame["top"], frame["right"], image.height)).resize(tuple(frame["normalized_size"]), Image.Resampling.LANCZOS)
        layout = json.loads((PROJECT_ROOT / "configs/layouts/memory-detail-pc-v1.json").read_text(encoding="utf-8"))
        reference = list(normalized.crop(layout["header_signature"]).resize((32, 16)).get_flattened_data())
        observed = [pixel for row in detail["header_signature"] for pixel in row]
        if len(observed) != len(reference):
            raise ValueError("卡片详情页头证据不完整。")
        rms = (sum((a-b)**2 for p, q in zip(reference, observed) for a, b in zip(p, q)) / (len(reference)*3))**0.5
        if rms > 15:
            raise ValueError("卡片详情页头图像与选中记忆不一致，未绑定。")
        draft = (record["review"] or {}).get("draft", report["draft"])
        matches = [i for i, value in enumerate(draft["card_ids"]) if value == detail["card_id"]]
        if slot is None:
            if len(matches) != 1:
                raise ValueError("无法唯一确定详情属于哪个卡槽；同名重复卡需由采集器传入槽位。")
            slot = matches[0]
        if type(slot) is not int or slot not in matches:
            raise ValueError("卡片详情与目标卡槽不一致。")
        if detail["status"] == "matched":
            path = PROJECT_ROOT / "configs/recognition/card-detail-rules.json"
            if hashlib.sha256(path.read_bytes()).hexdigest() != detail["rules_sha256"]:
                raise ValueError("详情识别规则已经变化，请重新识别。")
            rules = json.loads(path.read_text(encoding="utf-8"))
            attempts = detail.get("attempts", [])
            checks = [match_rule(a["reads"], detail["card_id"], catalog, rules,detail.get('corner_evidence')) for a in attempts]
            if len(checks) != 2 or not all(c["status"] == "matched" and c["token"] == detail["token"] for c in checks):
                raise ValueError("强化结果缺少一致的完整读取证据。")
            rule=next(r for r in rules['rules'] if r['card_id']==detail['card_id'])
            from gkshot.recognition.card_detail import verify_popup_boundary_evidence
            if not all(verify_popup_boundary_evidence(detail,a,rule) for a in attempts):
                raise ValueError('详情框外绿色像素的边界证据与原图不一致。')
            from gkshot.recognition.customization_rules import is_complete_rule
            if rule.get('corner_cost_required'):
                from gkshot.recognition.card_corner import valid_cost
                corner=detail.get('corner_evidence',{});owner=detail.get('ownership_evidence') or {}
                if (not valid_cost(corner,detail['card_id'],catalog.skill_cards[detail['card_id']]['name']) or corner['slot']!=slot
                        or corner['owner_sha256']!=owner.get('sha256')
                        or corner['owner_path']!=owner.get('source_path')):
                    raise ValueError('消耗角标缺少同一原图、同卡槽的归属证据。')
            if is_complete_rule(rule) and not all(
                    a.get('green_text_coverage',{}).get('complete') is True for a in attempts):
                raise ValueError("自定义绿色文字存在未读取区域，不能自动填写。")
            if detail.get('resolution_consensus'):
                widths=[a.get('pixel_width',0) for a in attempts]
                history=detail.get('resolution_attempts',[])
                if (not min(widths)>0 or max(widths)<min(widths)*1.15
                        or not all(a in history for a in attempts)):
                    raise ValueError("不同分辨率的完整读取证据不完整。")
                history=history+detail.get('native_retry',{}).get('attempts',[])
                for attempt in history:
                    check=match_rule(attempt['reads'],detail['card_id'],catalog,rules,detail.get('corner_evidence'))
                    if (check['status']=='conflict' or (check['status']=='matched'
                            and attempt.get('green_text_coverage',{}).get('complete') is True
                            and check['token']!=detail['token'])):
                        raise ValueError("不同分辨率的完整强化读数冲突，不能自动填写。")
            catalog.validate_entities([], [[detail["card_id"]]], [[detail["token"]]])
        evidence = report.setdefault("card_details", [])
        if any(d["sha256"] == detail["sha256"] and d["slot"] == slot and d["rules_sha256"] == detail["rules_sha256"] for d in evidence):
            return {"memory_id": memory_id, "created": False, "operation": "card-detail", "detail_status": "already-attached", "slot": slot, "token": detail["token"], "applied": False}
        detail = copy.deepcopy(detail)
        detail["slot"] = slot
        detail["archive_path"] = self.archive(detail)
        if detail.get("ownership_evidence"):
            detail["ownership_evidence"]["archive_path"] = self.archive(detail["ownership_evidence"])
        evidence.append(detail)
        applied = False
        if not record["review"]:
            field = f"customizations.{slot}"
            if detail["status"] == "matched" and field not in report["conflicts"]:
                previous = report["draft"]["customizations"][slot]
                if previous is not None and previous != detail["token"]:
                    report["draft"]["customizations"][slot] = None
                    report["conflicts"].append(field)
                else:
                    report["draft"]["customizations"][slot] = detail["token"]
                    applied = True
                report["evidence"].setdefault(field, []).append({"sha256": detail["sha256"], "value": detail["token"], "method": "tooltip-rule-match"})
            # This is a derived field, conditional on the current card ID, not whole-memory approval.
            for index, card_id in enumerate(report["draft"]["card_ids"]):
                card = catalog.skill_cards.get(card_id)
                if card and not card["availableCustomizations"] and report["draft"]["customizations"][index] is None and f"customizations.{index}" not in report["conflicts"]:
                    report["draft"]["customizations"][index] = ""
                    report["evidence"].setdefault(f"customizations.{index}", []).append({"value": "", "method": "catalog-no-options", "card_id": card_id, "revision": catalog.manifest["revision"]})
            report["status"] = assess_automatic(report,catalog)
        status = record["status"] if record["review"] else report["status"]
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            changed = db.execute("UPDATE memories SET report_json=?,status=?,content_key=?,updated_at=? WHERE memory_id=? AND updated_at=?",
                                 (json.dumps(report, ensure_ascii=False), status, fingerprint((record["review"] or {}).get("draft", report["draft"])), now, memory_id, record["updated_at"]))
            if changed.rowcount != 1:
                raise ValueError("此记忆在识别期间被修改，请重新读取详情；已保存的修改未被覆盖。")
        detail_status = "conflict" if f"customizations.{slot}" in report["conflicts"] else detail["status"]
        return {"memory_id": memory_id, "created": False, "operation": "card-detail", "detail_status": detail_status, "slot": slot, "token": detail["token"], "applied": applied, "human_review_preserved": bool(record["review"])}

    def source_path(self, source):
        path = (self.root / source["archive_path"]).resolve()
        if not path.is_relative_to(self.root / "sources"):
            raise ValueError("原图路径不在当前记忆库内。")
        path = io_path(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("归档原图校验失败。")
        return path
