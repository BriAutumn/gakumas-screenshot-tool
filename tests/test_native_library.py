import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.native_library import import_native_snapshot


class FakeCatalog:
    def __init__(self, revision="rev-1"):
        self.manifest = {"revision": revision}


def fake_convert(rows, mapping, catalog):
    memories = []
    rejected = []
    for row in rows:
        mid = row.get("UserMemoryId") if isinstance(row, dict) else None
        if mid is None:
            rejected.append({"UserMemoryId": None, "error": "missing UserMemoryId"})
            continue
        memories.append(
            {
                "memory_id": mid,
                "draft": {"text": row.get("text", ""), "p_idol_id": 123},
            }
        )
    return {"schema_version": 1, "memories": memories, "rejected": rejected}


class NativeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name
        self.snap = os.path.join(self.tmp, "snap.json")
        self.mapp = os.path.join(self.tmp, "map.json")
        self.lib = os.path.join(self.tmp, "library")
        self.rows = [
            {"UserMemoryId": "m1", "text": "one"},
            {"UserMemoryId": "m2", "text": "two"},
        ]
        self._write_inputs()
        self.catalog = FakeCatalog("rev-1")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_inputs(self):
        with open(self.snap, "w", encoding="utf-8-sig") as fh:
            json.dump(self.rows, fh)
        with open(self.mapp, "w", encoding="utf-8-sig") as fh:
            json.dump({"provenance": {"catalog_revision": "rev-1"}}, fh)

    def _import(self):
        with patch("gkshot.native_library.convert_snapshot", side_effect=fake_convert):
            return import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)

    def _connect(self):
        conn = sqlite3.connect(os.path.join(self.lib, "library.sqlite3"))
        conn.row_factory = sqlite3.Row
        return conn

    def _assert_no_staging(self):
        self.assertFalse(
            [n for n in os.listdir(self.tmp) if n.startswith(".native-import-")]
        )

    def test_creates_library_and_is_not_partial(self):
        manifest = self._import()
        self.assertEqual(manifest["source"], "native-export")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["total"], 2)
        self.assertEqual(manifest["ready"], 2)
        self.assertEqual(manifest["rejected"], [])
        for name in ("native-import.json", "snapshot.json", "mapping.json"):
            self.assertTrue(os.path.exists(os.path.join(self.lib, name)))

    def test_rows_are_native_imported_with_no_review(self):
        self._import()
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT memory_id, capture_key, status, review_json, report_json "
                "FROM memories ORDER BY memory_id"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["memory_id"], "m1")
        self.assertEqual(rows[0]["capture_key"], "native:m1")
        self.assertEqual(rows[0]["status"], "native-imported")
        self.assertIsNone(rows[0]["review_json"])
        report = json.loads(rows[0]["report_json"])
        self.assertEqual(report["kind"], "native-memory")
        self.assertEqual(report["identity"], {"status": "native", "p_idol_id": 123})
        self.assertEqual(report["pages"], [])
        self.assertEqual(report["native_source"]["ordinal"], 1)
        self.assertEqual(report["native_source"]["catalog_revision"], "rev-1")

    def test_rejected_rows_recorded_not_inserted(self):
        self.rows = list(self.rows) + [{"text": "no id"}]
        self._write_inputs()
        manifest = self._import()
        self.assertEqual(manifest["total"], 3)
        self.assertEqual(manifest["ready"], 2)
        self.assertEqual(len(manifest["rejected"]), 1)
        with contextlib.closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(count, 2)

    def test_version_mismatch_before_output(self):
        with open(self.mapp, "w", encoding="utf-8-sig") as fh:
            json.dump({"provenance": {"catalog_revision": "rev-other"}}, fh)
        with patch("gkshot.native_library.convert_snapshot", side_effect=fake_convert) as m:
            with self.assertRaises(ValueError):
                import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)
            m.assert_not_called()
        self.assertFalse(os.path.exists(self.lib))

    def test_duplicate_before_output(self):
        def duplicate_convert(rows, mapping, catalog):
            raise ValueError("duplicate memory_id")

        with patch(
            "gkshot.native_library.convert_snapshot", side_effect=duplicate_convert
        ):
            with self.assertRaises(ValueError):
                import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)
        self.assertFalse(os.path.exists(self.lib))

    def test_repeat_is_idempotent(self):
        first = self._import()
        second = self._import()
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(first["mapping_sha256"], second["mapping_sha256"])
        with contextlib.closing(self._connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(count, 2)

    def test_refuse_different_snapshot(self):
        self._import()
        self.rows = [{"UserMemoryId": "m3", "text": "three"}]
        self._write_inputs()
        with patch("gkshot.native_library.convert_snapshot", side_effect=fake_convert):
            with self.assertRaises(FileExistsError):
                import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)

    def test_refuse_existing_non_native_library(self):
        os.makedirs(self.lib)
        with open(os.path.join(self.lib, "library.sqlite3"), "w", encoding="utf-8") as fh:
            fh.write("not a native library")
        with patch("gkshot.native_library.convert_snapshot", side_effect=fake_convert):
            with self.assertRaises(FileExistsError):
                import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)

    def test_no_partial_library_on_write_failure(self):
        with patch("gkshot.native_library.convert_snapshot", side_effect=fake_convert):
            with patch("gkshot.native_library.MemoryLibrary") as ml:
                ml.return_value.connect.side_effect = RuntimeError("boom")
                with self.assertRaises(RuntimeError):
                    import_native_snapshot(self.snap, self.mapp, self.lib, self.catalog)
        self.assertFalse(os.path.exists(self.lib))
        self._assert_no_staging()

    def test_ordinals_follow_native_row_order_with_gaps(self):
        self.rows = [
            {"UserMemoryId": "uuid-b", "text": "b"},
            {"text": "rejected"},
            {"UserMemoryId": "uuid-a", "text": "a"},
        ]
        self._write_inputs()
        manifest = self._import()
        self.assertEqual(manifest["total"], 3)
        self.assertEqual(manifest["ready"], 2)
        self.assertEqual(len(manifest["rejected"]), 1)
        with contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT memory_id, report_json FROM memories ORDER BY memory_id"
            ).fetchall()
        self.assertEqual([r["memory_id"] for r in rows], ["uuid-a", "uuid-b"])
        reports = {r["memory_id"]: json.loads(r["report_json"]) for r in rows}
        self.assertEqual(reports["uuid-b"]["native_source"]["ordinal"], 1)
        self.assertEqual(reports["uuid-a"]["native_source"]["ordinal"], 3)
        self.assertEqual(reports["uuid-a"]["identity"]["p_idol_id"], 123)


if __name__ == "__main__":
    unittest.main()
