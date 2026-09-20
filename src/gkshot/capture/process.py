"""Replay saved raw captures under the CURRENT recognizers, without a game window."""
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from gkshot.capture.store import CaptureSession, atomic_json
from gkshot.memory_library import MemoryLibrary


def processing_summary(results):
    successful = sum(r.get("status") == "processed" for r in results)
    issues = sum(r.get("status") == "processed-with-issues" for r in results)
    failed = sum(r.get("status") == "failed" for r in results)
    if not results:
        return "没有可处理的已采完记忆；请先完成一份试采，中途暂停的记忆不会入库。"
    return f"离线处理：成功入库 {successful + issues} 份（其中 {issues} 份有待解决问题），失败 {failed} 份。"


def latest_result_library(captures_root):
    """Find a result with imported records, skipping newer empty/failed runs."""
    candidates = sorted(Path(captures_root).glob("*/processing/*/results.json"),
                        key=lambda p: p.parent.name, reverse=True)
    for path in candidates:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            library = path.parent / "memory-library"
            if any(r.get("memory_id") for r in result.get("results", [])) and (library / "library.sqlite3").is_file():
                return library
        except (ValueError, OSError):
            continue
    return None


def process_session(root, *, library_root=None, notify=None, memory_scanner=None, detail_scanner=None, cancelled=None):
    from gkshot.recognition.memory import MemoryScanner
    from gkshot.recognition.card_detail import CardDetailScanner
    session = CaptureSession(root)
    notify = notify or (lambda text: None)
    cancelled = cancelled or (lambda: False)
    scanner = memory_scanner or MemoryScanner()
    tooltip = detail_scanner or CardDetailScanner(scanner)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    directory = session.root / "processing" / run_id
    directory.mkdir(parents=True)
    library = MemoryLibrary(library_root or directory / "memory-library")
    results = []
    for entry in session.data["entries"]:
        if cancelled():
            notify("离线处理已停止；本次已有结果保留。")
            break
        if entry["status"] not in ("captured", "needs-recapture"):
            continue
        notify(f"离线处理第 {entry['ordinal']} 份；不会操作游戏……")
        result = {"capture_id": entry["capture_id"], "ordinal": entry["ordinal"], "card_details": [], "errors": []}
        images = [image for image in entry["images"] if image["metadata"].get("attempt") == entry["attempt"]]
        try:
            pages = [session.source(image) for image in images if image["role"] in ("overview-top", "overview-cards")]
            report = scanner.scan(pages,client_area=True)
            from gkshot.recognition.bound_card_identity import refine
            identity_details = refine(report,session,images,scanner,tooltip) if hasattr(scanner,'normalize') else {}
            atomic_json(directory / f"{entry['ordinal']:05d}-memory.json", report)
            # Every run gets its own draft: changed rules never silently mutate a prior reviewed result.
            result.update(library.import_report(report, capture_id=f"{session.data['session_id']}:{entry['capture_id']}:processing:{run_id}"))
            for index, image in enumerate(images):
                if cancelled():
                    result["errors"].append({"error": "processing-cancelled"})
                    break
                if image["role"] != "card-tooltip":
                    continue
                slot = image["slot"]
                try:
                    if report["draft"]["customizations"][slot-1] == "":
                        result["card_details"].append({"slot":slot,"detail_status":"skipped-no-customization"})
                        continue
                    expected = report["draft"]["card_ids"][slot-1]
                    owner_path = None
                    if image["metadata"].get("binding_check") == "same-header-and-overview-before-and-after":
                        before = [i for i in images[:index] if i["role"]=="card-before" and i["slot"]==slot
                                  and i["metadata"].get("try")==image["metadata"].get("try")]
                        if not before:
                            raise ValueError("缺少同一次展开前的归属截图。")
                        owner_path = session.source(before[-1])
                    detail = identity_details.get(index)
                    if detail is None:
                        detail = tooltip.scan(session.source(image), expected_card_id=expected,
                                              client_area=True,owner_path=owner_path)
                    atomic_json(directory / f"{entry['ordinal']:05d}-card-{slot}-{index}.json", detail)
                    if detail.get("card_id") is None:
                        result["card_details"].append({"slot":slot,"detail_status":detail["status"]})
                        result["errors"].append({"slot":slot,"error":"详情卡名未可靠识别；原图已保留，可更新识别规则后重新处理。"})
                        continue
                    attached = library.add_card_detail(result["memory_id"], detail, catalog=scanner.catalog, slot=slot-1)
                    result["card_details"].append(attached)
                    if attached["detail_status"] not in ("matched", "already-attached"):
                        result["errors"].append({"slot": slot, "error": detail.get("reason") or attached["detail_status"]})
                except (OSError, ValueError, TypeError) as error:
                    result["errors"].append({"slot": slot, "error": str(error)})
            saved=library.get(result["memory_id"])
            result["memory_status"]=saved["status"]
            result["unresolved"]=saved["report"].get("automatic_check",{}).get("unresolved",[])
            result["status"] = "processed" if not result["errors"] and not result["unresolved"] else "processed-with-issues"
        except (OSError, ValueError, TypeError) as error:
            result["status"] = "failed"
            result["errors"].append({"error": str(error)})
        results.append(result)
        atomic_json(directory / "results.json", {"run_id": run_id, "source_session": session.data["session_id"],
            "recognition_revision": scanner.catalog.manifest["revision"], "results": results})
    if not results:
        atomic_json(directory / "results.json", {"run_id": run_id, "source_session": session.data["session_id"], "results": []})
    from gkshot.capture.diagnostics import write_diagnostics
    write_diagnostics(directory,library_root=library.root)
    return {"run_id": run_id, "directory": str(directory), "library_directory": str(library.root), "results": results}
