import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from gkshot.native_service import NativeService
from test_native_memories import _make_row, MAPPING, FakeCatalog


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        mapping = dict(MAPPING, provenance={'catalog_revision':'test'})
        # JSON keys in the synthetic fixtures are integers; use a stub converter
        # only in the mapping serialization test, real conversion below supplies original mapping.
        self.mapping = self.home/'mapping.json'
        self.mapping.write_text(json.dumps(mapping),encoding='utf8')
        self.catalog = FakeCatalog(); self.catalog.manifest={'revision':'test'}
        self.service = NativeService(self.home,self.mapping,self.catalog)
        self.service.export_dir.mkdir()

    def tearDown(self): self.temp.cleanup()

    def export(self, rows, status_overrides=None):
        raw=json.dumps(rows).encode()
        (self.service.export_dir/'memories.json').write_bytes(raw)
        info={'completed':True,'status':'completed','records':len(rows),'snapshot_sha256':hashlib.sha256(raw).hexdigest()}
        info.update(status_overrides or {})
        (self.service.export_dir/'status.json').write_text(json.dumps(info),encoding='utf8')

    def test_incomplete_status_cannot_reuse_old_file(self):
        self.export([_make_row()],{'completed':False,'status':'incomplete'})
        with self.assertRaises(ValueError): self.service.import_latest()
        self.assertFalse((self.home/'data/native-memories').exists())

    def test_hash_and_count_must_match(self):
        for bad in [{'snapshot_sha256':'0'*64},{'records':99}]:
            self.export([_make_row()],bad)
            with self.assertRaises(ValueError): self.service.import_latest()

    def test_stage_filter_and_idempotent_import(self):
        from gkshot.native_memories import convert_snapshot
        rows=[_make_row(),_make_row(UserMemoryId='produce',ExamBattleProduceCards=[],ExamBattleProduceItemIds=[])]
        self.export(rows)
        def convert(data, mapping, catalog): return convert_snapshot(data,MAPPING,catalog)
        with patch('gkshot.native_library.convert_snapshot',side_effect=convert):
            first=self.service.import_latest(); second=self.service.import_latest()
        self.assertEqual(first['library'],second['library'])
        self.assertEqual(first['ready'],1); self.assertEqual(first['skipped_count'],1)
        self.assertEqual(first['rejected_count'],0)
