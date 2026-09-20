"""Explicit capture groups allow batch imports without guessing which pages belong together."""
from pathlib import Path
from gkshot.models import read_json


def import_batch(manifest_path, scanner, library):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("memories"), list) or not manifest["memories"]:
        raise ValueError("批量清单需要 schema_version=1 和非空 memories 列表。")
    results, seen = [], set()
    # Validate grouping before importing anything. capture_id identifies an owned copy, not its stats.
    for group in manifest["memories"]:
        if (not isinstance(group, dict) or not isinstance(group.get("capture_id"), str)
                or not group["capture_id"].strip() or group["capture_id"] in seen
                or not isinstance(group.get("details"), list) or not group["details"]
                or not all(isinstance(path, str) and path for path in group["details"])):
            raise ValueError("每组需要唯一 capture_id 和该记忆的非空详情图列表；请勿混合不同记忆。")
        seen.add(group["capture_id"])
    for group in manifest["memories"]:
        try:
            paths = [manifest_path.parent / path for path in group["details"]]
            context = manifest_path.parent / group["list_context"] if group.get("list_context") else None
            report = scanner.scan(paths, context_path=context)
            results.append({"capture_id": group["capture_id"], **library.import_report(report, capture_id=group["capture_id"])})
        except (OSError, ValueError, TypeError) as error:
            results.append({"capture_id": group["capture_id"], "error": str(error)})
    return results
