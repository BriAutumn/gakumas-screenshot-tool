"""Immutable snapshot library import for native export snapshots.

Reads a raw native-export snapshot plus a mapping, converts via
``gkshot.native_memories.convert_snapshot``, and materialises a fresh
immutable MemoryLibrary under ``library_path``. The import is
transactional: all writes land in a staging sibling directory that is
later renamed into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from gkshot.memory_library import MemoryLibrary, fingerprint
from gkshot.native_memories import convert_snapshot, CONVERSION_POLICY


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def import_native_snapshot(snapshot_path, mapping_path, library_path, catalog) -> dict:
    snapshot_path = Path(snapshot_path).resolve()
    mapping_path = Path(mapping_path).resolve()
    library_path = Path(library_path).resolve()

    for input_path in (snapshot_path, mapping_path):
        if input_path == library_path or library_path in input_path.parents:
            raise ValueError(
                "input path %s resides inside output library %s"
                % (input_path, library_path)
            )

    snapshot_bytes = snapshot_path.read_bytes()
    mapping_bytes = mapping_path.read_bytes()
    snapshot_sha256 = _sha256_bytes(snapshot_bytes)
    mapping_sha256 = _sha256_bytes(mapping_bytes)

    rows = json.loads(snapshot_bytes.decode("utf-8-sig"))
    mapping = json.loads(mapping_bytes.decode("utf-8-sig"))

    mapping_revision = (mapping.get("provenance") or {}).get("catalog_revision")
    catalog_revision = catalog.manifest["revision"]
    if mapping_revision != catalog_revision:
        raise ValueError(
            "mapping catalog_revision %r does not match catalog revision %r"
            % (mapping_revision, catalog_revision)
        )

    # Existing destination handling: only idempotent native imports allowed.
    manifest_path = library_path / "native-import.json"
    db_path = library_path / "library.sqlite3"
    if library_path.exists():
        if db_path.is_file() and manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if (
                existing.get("source") == "native-export"
                and existing.get("schema_version") == 1
                and existing.get('conversion_policy') == CONVERSION_POLICY
                and existing.get("catalog_revision") == catalog_revision
                and existing.get("snapshot_sha256") == snapshot_sha256
                and existing.get("mapping_sha256") == mapping_sha256
            ):
                return existing
        raise FileExistsError(
            "library path %s already exists and is not the same native import"
            % library_path
        )

    # Convert before creating any destination files.
    conversion = convert_snapshot(rows, mapping, catalog)
    memories = conversion["memories"]
    rejected = conversion["rejected"]
    skipped = conversion.get('skipped', [])

    ordinal_by_id: dict = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        mid = row.get("UserMemoryId")
        if mid is not None and mid not in ordinal_by_id:
            ordinal_by_id[mid] = index

    now = datetime.now(timezone.utc).isoformat()

    parent = library_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=".native-import-", dir=str(parent)))
    staging_lib = staging_parent / "library"

    try:
        lib = MemoryLibrary(str(staging_lib))
        with lib.connect() as conn:
            for entry in memories:
                mid = entry["memory_id"]
                draft = entry["draft"]
                report = {
                    "schema_version": 1,
                    "kind": "native-memory",
                    "draft": draft,
                    "identity": {
                        "status": "native",
                        "p_idol_id": draft["p_idol_id"],
                    },
                    "pages": [],
                    "native_source": {
                        "memory_id": mid,
                        "ordinal": ordinal_by_id.get(mid),
                        "snapshot_sha256": snapshot_sha256,
                        "mapping_sha256": mapping_sha256,
                        "catalog_revision": catalog_revision,
                    },
                }
                conn.execute(
                    "INSERT INTO memories "
                    "(memory_id, capture_key, content_key, created_at, updated_at, "
                    " status, report_json, review_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        mid,
                        "native:" + mid,
                        fingerprint(draft),
                        now,
                        now,
                        "native-imported",
                        _dumps(report),
                        None,
                    ),
                )

        (staging_lib / "snapshot.json").write_bytes(snapshot_bytes)
        (staging_lib / "mapping.json").write_bytes(mapping_bytes)

        manifest = {
            "source": "native-export",
            "schema_version": 1,
            "exported_at": _iso_utc(snapshot_path.stat().st_mtime),
            "snapshot_sha256": snapshot_sha256,
            "mapping_sha256": mapping_sha256,
            "catalog_revision": catalog_revision,
            "total": len(rows),
            "ready": len(memories),
            "rejected": rejected,
            "skipped": skipped,
            "conversion_policy": CONVERSION_POLICY,
        }
        (staging_lib / "native-import.json").write_text(
            _dumps(manifest), encoding="utf-8"
        )

        os.rename(str(staging_lib), str(library_path))
    except Exception:
        shutil.rmtree(str(staging_parent), ignore_errors=True)
        raise
    finally:
        shutil.rmtree(str(staging_parent), ignore_errors=True)

    return manifest
