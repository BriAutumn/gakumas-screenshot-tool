import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.catalog import Catalog
from gkshot.memory_library import MemoryLibrary
from gkshot.native_memories import CONVERSION_POLICY
from gkshot.simulation import list_ready_memories, read_ready_memory
from gkshot.simulation_search import memory_records, memory_preview
from gkshot.simulation_web import SimulationServer


class NativeLibrarySearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = self.root / 'data/native-memories/export-test'
        self.catalog = Catalog()
        self.manifest = {'source': 'native-export', 'schema_version': 1,
                         'conversion_policy': CONVERSION_POLICY,
                         'catalog_revision': self.catalog.manifest['revision'],
                         'snapshot_sha256': 'a' * 64, 'mapping_sha256': 'b' * 64,
                         'exported_at': '2026-10-08T00:00:00+00:00', 'ready': 2}
        self.draft = {'p_idol_id': 123, 'parameters': {'vocal': 100, 'dance': 200, 'visual': 300, 'stamina': 40},
                      'card_ids': [0] * 6, 'customizations': [''] * 6, 'item_ids': [0] * 4}
        library = MemoryLibrary(self.library)
        (self.library / 'native-import.json').write_text(json.dumps(self.manifest), encoding='utf-8')
        with library.connect() as db:
            for memory_id, ordinal in [('uuid-z', 1), ('uuid-a', 2)]:
                report = {'kind': 'native-memory', 'draft': self.draft, 'pages': [],
                          'identity': {'status': 'native', 'p_idol_id': 123},
                          'native_source': {k: self.manifest[k] for k in ('catalog_revision', 'snapshot_sha256', 'mapping_sha256')}}
                report['native_source'].update(memory_id=memory_id, ordinal=ordinal)
                db.execute('INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (memory_id, 'native:' + memory_id, 'same-content', 'today', 'today',
                            'native-imported', json.dumps(report), None))

    def test_native_source_order_and_duplicate_content_owned_ids(self):
        records = memory_records(self.library)
        self.assertEqual([r['id'] for r in records], ['uuid-z', 'uuid-a'])
        self.assertEqual([r['ordinal'] for r in records], [1, 2])
        self.assertTrue(all(r['source'] == 'native-export' and r['capture_id'] is None for r in records))
        self.assertEqual(read_ready_memory(self.library, 'uuid-z'), self.draft)
        self.assertEqual(len(list_ready_memories(self.library)), 2)

    def test_native_preview_is_structured_without_fabricated_screenshot(self):
        preview = memory_preview(self.library, 'uuid-z')
        self.assertEqual(preview['source'], 'native-export')
        self.assertEqual(preview['draft'], self.draft)
        self.assertNotIn('image', preview)

    def test_stale_catalog_and_different_snapshot_are_rejected(self):
        for field, value in [('catalog_revision', 'old-version'), ('snapshot_sha256', 'c' * 64)]:
            bad = {**self.manifest, field: value}
            (self.library / 'native-import.json').write_text(json.dumps(bad), encoding='utf-8')
            with self.assertRaises(ValueError):
                memory_records(self.library)
            with self.assertRaises(ValueError):
                read_ready_memory(self.library, 'uuid-a')

    def test_server_discovers_and_labels_native_library(self):
        with patch('gkshot.simulation_web.USER_ROOT', self.root):
            server = SimulationServer(('127.0.0.1', 0))
        try:
            self.assertEqual(server.libraries['data/native-memories/export-test'], self.library)
            self.assertEqual(server.library_label(self.library), '原生导出 · 2026-10-08 · 2 份')
        finally:
            server.server_close()

    def test_server_ignores_libraries_from_before_stage_filter(self):
        old = dict(self.manifest)
        old.pop('conversion_policy')
        (self.library / 'native-import.json').write_text(json.dumps(old), encoding='utf8')
        with patch('gkshot.simulation_web.USER_ROOT', self.root):
            server = SimulationServer(('127.0.0.1', 0))
        try:
            self.assertNotIn('data/native-memories/export-test', server.libraries)
            self.assertTrue((self.library / 'library.sqlite3').is_file())
        finally: server.server_close()


if __name__ == '__main__':
    unittest.main()
