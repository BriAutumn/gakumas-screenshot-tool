import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.native_photos import MAX_IMAGE_BYTES, default_cache_root, native_photo_preview

def cache_key(path):
    # Synthetic exported keys; production uses the game's URL formatter.
    return hashlib.md5(path.encode('utf8')).hexdigest().upper()

def add_keys(rows):
    for row in rows:
        if row.get('ImagePath') and 'PhotoCacheKey' not in row:
            row['PhotoCacheKey'] = cache_key(row['ImagePath'])


PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 16


def _decode(result):
    return base64.b64decode(result['image'].split(',', 1)[1])


class NativePhotoPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def snapshot(self, rows):
        add_keys(rows)
        raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        (self.library / 'snapshot.json').write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def cache_put(self, image_path, data):
        digest = cache_key(image_path)
        path = self.cache / digest[:2] / digest
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return digest

    def test_exact_id_and_ordinal_return_original_photo(self):
        rows = [{'UserMemoryId': 'owned-1', 'ImagePath': 'photo/a'},
                {'UserMemoryId': 'owned-2', 'ImagePath': 'photo/b'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/b', JPEG)
        result = native_photo_preview(self.library, 'owned-2', 2, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'available')
        self.assertEqual(result['photo_mime'], 'image/jpeg')
        self.assertEqual(result['photo_sha256'], hashlib.sha256(JPEG).hexdigest())
        self.assertTrue(result['image'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(_decode(result), JPEG)

    def test_identical_draft_two_ids_select_the_matching_image(self):
        rows = [{'UserMemoryId': 'copy-a', 'ImagePath': 'same/draft/a'},
                {'UserMemoryId': 'copy-b', 'ImagePath': 'same/draft/b'}]
        sha = self.snapshot(rows)
        self.cache_put('same/draft/a', PNG)
        self.cache_put('same/draft/b', JPEG)
        first = native_photo_preview(self.library, 'copy-a', 1, sha, cache_root=self.cache)
        second = native_photo_preview(self.library, 'copy-b', 2, sha, cache_root=self.cache)
        self.assertEqual(first['photo_mime'], 'image/png')
        self.assertEqual(_decode(first), PNG)
        self.assertEqual(second['photo_mime'], 'image/jpeg')
        self.assertEqual(_decode(second), JPEG)

    def test_snapshot_hash_mismatch_never_reads_the_image(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/x'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/x', PNG)  # present on disk, must still be ignored
        before = (self.cache / cache_key('photo/x')[:2] / cache_key('photo/x')).read_bytes()
        result = native_photo_preview(self.library, 'owned', 1, 'f' * 64, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'snapshot-mismatch')
        self.assertNotIn('image', result)
        self.assertIn('重新', result['photo_message'])
        self.assertEqual(result['photo_message'].find(str(self.cache)), -1)
        self.assertEqual((self.cache / cache_key('photo/x')[:2] / cache_key('photo/x')).read_bytes(), before)
        # The same library still works with the proven hash.
        self.assertEqual(native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)['photo_status'],
                         'available')

    def test_duplicate_id_and_ordinal_mismatch_are_rejected(self):
        rows = [{'UserMemoryId': 'dup', 'ImagePath': 'photo/p'},
                {'UserMemoryId': 'dup', 'ImagePath': 'photo/p'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/p', PNG)
        result = native_photo_preview(self.library, 'dup', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'memory-mismatch')
        self.assertNotIn('image', result)

        rows = [{'UserMemoryId': 'a', 'ImagePath': 'photo/p'},
                {'UserMemoryId': 'b', 'ImagePath': 'photo/q'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/q', PNG)
        result = native_photo_preview(self.library, 'b', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'memory-mismatch')
        self.assertNotIn('image', result)

    def test_missing_snapshot_and_missing_metadata_are_reported(self):
        result = native_photo_preview(self.library, 'owned', 1, 'a' * 64, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'metadata-missing')
        self.assertNotIn('image', result)
        self.assertIn('重新导出', result['photo_message'])

        rows = [{'UserMemoryId': 'old', 'AssetId': 'asset-only', 'ImagePath': ''}]
        sha = self.snapshot(rows)
        result = native_photo_preview(self.library, 'old', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'asset-photo-unavailable')

    def test_absent_cache_file_is_reported_without_leaking_paths(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'secret/photo.png'}]
        sha = self.snapshot(rows)
        result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'cache-missing')
        self.assertNotIn('image', result)
        self.assertNotIn(str(self.cache), result['photo_message'])
        self.assertNotIn('secret/photo.png', result['photo_message'])
        self.assertIn('回忆一览', result['photo_message'])

    def test_invalid_and_oversized_images_are_rejected(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/z'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/z', b'GIF89a not a jpeg or png')
        self.assertEqual(native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)['photo_status'],
                         'image-invalid')

        self.cache_put('photo/z', b'\xff\xd8\xff' + b'0' * MAX_IMAGE_BYTES)
        result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'image-too-large')
        self.assertNotIn('image', result)

    def test_image_at_size_limit_is_accepted(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/limit'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/limit', b'\xff\xd8\xff' + b'0' * (MAX_IMAGE_BYTES - 3))
        result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'available')
        self.assertEqual(len(_decode(result)), MAX_IMAGE_BYTES)

    def test_exported_cache_keys_are_used_verbatim(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': ' photo/x '}]
        sha = self.snapshot(rows)
        self.cache_put('photo/x', PNG)  # trimmed name must not be used
        self.assertEqual(native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)['photo_status'],
                         'cache-missing')
        self.cache_put(' photo/x ', JPEG)
        result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'available')
        self.assertEqual(_decode(result), JPEG)

    def test_missing_localappdata_is_explicit(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/x'}]
        sha = self.snapshot(rows)
        with patch.dict(os.environ, {}, clear=True):
            result = native_photo_preview(self.library, 'owned', 1, sha)
        self.assertEqual(result['photo_status'], 'cache-unconfigured')
        self.assertNotIn('image', result)
        self.assertIn('缓存', result['photo_message'])

    def test_untrusted_cache_key_cannot_read_arbitrary_paths(self):
        for key in ['../outside.png', 'c:/private.png', ' A' * 16,
                    cache_key('photo/lower').lower(), 'A' * 31]:
            rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/x', 'PhotoCacheKey': key}]
            sha = self.snapshot(rows)
            result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
            self.assertEqual(result['photo_status'], 'metadata-invalid')
            self.assertNotIn('image', result)

    def test_old_image_path_without_exported_key_requires_reexport(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/old', 'PhotoCacheKey': ''}]
        sha = self.snapshot(rows)
        self.cache_put('photo/old', PNG)
        self.assertEqual(native_photo_preview(self.library, 'owned', 1, sha,
                         cache_root=self.cache)['photo_status'], 'metadata-missing')

    def test_exact_preset_asset_and_hash_are_required(self):
        preset = self.root / 'presets'
        preset.mkdir()
        aid = 'img_general_memory_test-001'
        image = preset / (aid + '.png')
        image.write_bytes(PNG)
        (preset / 'manifest.json').write_text(json.dumps({'covers': [
            {'asset_id': aid, 'image_sha256': hashlib.sha256(PNG).hexdigest()}]}))
        sha = self.snapshot([{'UserMemoryId': 'owned', 'AssetId': aid}])
        result = native_photo_preview(self.library, 'owned', 1, sha, preset_root=preset)
        self.assertEqual(result['photo_kind'], 'preset')
        self.assertEqual(_decode(result), PNG)
        image.write_bytes(PNG + b'changed')
        self.assertEqual(native_photo_preview(self.library, 'owned', 1, sha,
                         preset_root=preset)['photo_status'], 'image-invalid')

    def test_default_cache_root_is_sibling_locallow(self):
        local = str(self.root / 'AppData' / 'Local')
        with patch.dict(os.environ, {'LOCALAPPDATA': local}):
            root = default_cache_root()
        self.assertEqual(root, Path(local).parent / 'LocalLow' /
                         'BANDAI NAMCO Entertainment Inc_' / 'gakumas' / 'photo_storage_cache' / 'cache_files')

    def test_default_cache_root_end_to_end(self):
        local = self.root / 'AppData' / 'Local'
        default = (local.parent / 'LocalLow' / 'BANDAI NAMCO Entertainment Inc_' /
                   'gakumas' / 'photo_storage_cache' / 'cache_files')
        default.mkdir(parents=True)
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/real'}]
        sha = self.snapshot(rows)
        key = cache_key('photo/real')
        (default / key[:2]).mkdir()
        (default / key[:2] / key).write_bytes(PNG)
        with patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
            result = native_photo_preview(self.library, 'owned', 1, sha)
        self.assertEqual(result['photo_status'], 'available')
        self.assertEqual(_decode(result), PNG)

    def test_preview_does_not_mutate_snapshot_or_cache(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/ro'}]
        sha = self.snapshot(rows)
        self.cache_put('photo/ro', PNG)
        before = {str(path.relative_to(self.cache)): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()}
        native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        after = {str(path.relative_to(self.cache)): path.read_bytes() for path in self.cache.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(hashlib.sha256((self.library / 'snapshot.json').read_bytes()).hexdigest(), sha)

    def test_symlink_escape_is_refused(self):
        rows = [{'UserMemoryId': 'owned', 'ImagePath': 'photo/link'}]
        sha = self.snapshot(rows)
        outside = self.root / 'outside.png'
        outside.write_bytes(PNG)
        key = cache_key('photo/link')
        link = self.cache / key[:2] / key
        link.parent.mkdir()
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available with current privileges')
        result = native_photo_preview(self.library, 'owned', 1, sha, cache_root=self.cache)
        self.assertEqual(result['photo_status'], 'unsafe-cache-target')
        self.assertNotIn('image', result)


class NativeMemoryPreviewIntegrationTests(unittest.TestCase):
    """The structured native preview must stay usable and gain photo fields when proven."""

    @classmethod
    def setUpClass(cls):
        from gkshot.catalog import Catalog
        cls.catalog = Catalog()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.draft = {'p_idol_id': 123, 'parameters': {'vocal': 100, 'dance': 200, 'visual': 300, 'stamina': 40},
                      'card_ids': [0] * 6, 'customizations': [''] * 6, 'item_ids': [0] * 4}

    def build(self, name, rows, *, write_snapshot):
        add_keys(rows)
        from gkshot.memory_library import MemoryLibrary
        from gkshot.native_memories import CONVERSION_POLICY
        catalog = self.catalog
        library = self.root / 'data/native-memories' / name
        library.mkdir(parents=True)
        snapshot_sha = 'c' * 64
        if write_snapshot:
            raw = json.dumps(rows, ensure_ascii=False).encode('utf-8')
            (library / 'snapshot.json').write_bytes(raw)
            snapshot_sha = hashlib.sha256(raw).hexdigest()
        manifest = {'source': 'native-export', 'schema_version': 1, 'conversion_policy': CONVERSION_POLICY,
                    'catalog_revision': catalog.manifest['revision'],
                    'snapshot_sha256': snapshot_sha, 'mapping_sha256': 'b' * 64,
                    'exported_at': '2026-10-08T00:00:00+00:00', 'ready': len(rows)}
        (library / 'native-import.json').write_text(json.dumps(manifest), encoding='utf-8')
        lib = MemoryLibrary(library)
        with lib.connect() as db:
            for ordinal, row in enumerate(rows, start=1):
                memory_id = row['UserMemoryId']
                report = {'kind': 'native-memory', 'draft': self.draft, 'pages': [],
                          'identity': {'status': 'native', 'p_idol_id': 123},
                          'native_source': {'memory_id': memory_id, 'ordinal': ordinal,
                                            'snapshot_sha256': snapshot_sha, 'mapping_sha256': 'b' * 64,
                                            'catalog_revision': catalog.manifest['revision']}}
                db.execute('INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (memory_id, 'native:' + memory_id, 'c', 'today', 'today',
                            'native-imported', json.dumps(report), None))
        return library

    def test_native_preview_returns_original_photo_when_proven(self):
        from gkshot.simulation_search import memory_preview
        rows = [{'UserMemoryId': 'uuid-1', 'ImagePath': 'photo/original'}]
        library = self.build('with-photo', rows, write_snapshot=True)
        local = self.root / 'AppData' / 'Local'
        default = (local.parent / 'LocalLow' / 'BANDAI NAMCO Entertainment Inc_' /
                   'gakumas' / 'photo_storage_cache' / 'cache_files')
        default.mkdir(parents=True)
        key = cache_key('photo/original')
        (default / key[:2]).mkdir()
        (default / key[:2] / key).write_bytes(JPEG)
        with patch.dict(os.environ, {'LOCALAPPDATA': str(local)}):
            preview = memory_preview(library, 'uuid-1')
        self.assertEqual(preview['source'], 'native-export')
        self.assertEqual(preview['draft'], self.draft)
        self.assertEqual(preview['ordinal'], 1)
        self.assertEqual(preview['photo_status'], 'available')
        self.assertEqual(_decode(preview), JPEG)

    def test_old_snapshot_still_returns_structured_draft_without_image(self):
        from gkshot.simulation_search import memory_preview
        rows = [{'UserMemoryId': 'uuid-old', 'ImagePath': 'photo/old'}]
        library = self.build('old-import', rows, write_snapshot=False)
        preview = memory_preview(library, 'uuid-old')
        self.assertEqual(preview['source'], 'native-export')
        self.assertEqual(preview['draft'], self.draft)
        self.assertEqual(preview['photo_status'], 'metadata-missing')
        self.assertNotIn('image', preview)


if __name__ == '__main__':
    unittest.main()
