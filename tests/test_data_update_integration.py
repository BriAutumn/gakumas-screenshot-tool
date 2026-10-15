"""Integration tests for the upstream simulation-data release plumbing.

Covers the three pieces the launcher/data updater depend on:

* :mod:`gkshot.resource_paths` -- frozen baseline vs. explicit single-root
  fixtures, strict validation of an activated release (inventory hashes,
  native mapping provenance, path safety) and process-lifetime pinning;
* :mod:`gkshot.native_service` -- default mapping/catalog come from the
  *pinned* resource set, explicit arguments still win;
* :mod:`gkshot.simulation_web` -- the maintenance marker refuses every
  endpoint that would start or resume engine work.

The parallel ``gkshot.data_updates`` module is intentionally not imported so
this file stays valid while that module is developed separately.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from gkshot import native_service
from gkshot import resource_paths
from gkshot import catalog as catalog_module
from gkshot.native_service import NativeService
from gkshot.resource_paths import ResourceError, resolve_paths

REVISION_A = 'a' * 40
REVISION_B = 'b' * 40
REVISION_OTHER = 'c' * 40


# --------------------------------------------------------------------------- #
# release fixture helpers
# --------------------------------------------------------------------------- #
def standard_files(revision, mapping_revision=None):
    """The minimum file set a valid release must contain."""
    return {
        'assets/recognition/manifest.json': json.dumps({'revision': revision, 'files': {}}),
        'assets/recognition/skill_cards.json': json.dumps([]),
        'runtime/node/manifest.json': json.dumps({'revision': revision}),
        'runtime/node/index.mjs': 'export const revision = %s;\n' % json.dumps(revision),
        'assets/native/mapping.json': json.dumps({'provenance': {
            'engine_revision': revision,
            'catalog_revision': mapping_revision or revision}}),
    }


def write_release(root, revision, files=None, mutate_record=None):
    """Materialise ``runtime/releases/<revision>``; returns (dir, release.json bytes)."""
    files = standard_files(revision) if files is None else files
    release = root / 'runtime' / 'releases' / revision
    for name, data in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode('utf-8'))
    record = {'schema_version': 1, 'revision': revision, 'validation': {'status': 'passed'},
              'files': {name: hashlib.sha256(data.encode('utf-8')).hexdigest()
                        for name, data in files.items()}}
    if mutate_record is not None:
        mutate_record(record)
    raw = json.dumps(record).encode('utf-8')
    (release / 'release.json').write_bytes(raw)
    return release, raw


def point_at(root, revision, manifest_raw):
    base = root / 'runtime' / 'releases'
    base.mkdir(parents=True, exist_ok=True)
    (base / 'active.json').write_text(json.dumps({
        'schema_version': 1,
        'active': {'revision': revision, 'release': f'runtime/releases/{revision}',
                   'release_manifest_sha256': hashlib.sha256(manifest_raw).hexdigest()},
        'history': [{'revision': None}, {'revision': revision}]}), encoding='utf-8')


def build_active_release(root, revision, **kwargs):
    release, raw = write_release(root, revision, **kwargs)
    point_at(root, revision, raw)
    return release


def bundled_tree(root):
    (root / 'assets' / 'recognition').mkdir(parents=True, exist_ok=True)
    (root / 'runtime' / 'node').mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# resource_paths
# --------------------------------------------------------------------------- #
class BaselineResolutionTests(unittest.TestCase):
    """Absent/null pointer resolves the read-only bundled root, never the home."""

    def test_default_mode_without_pointer_uses_bundled_root(self):
        with tempfile.TemporaryDirectory() as folder:
            home = bundled_tree(Path(folder).resolve() / 'home')
            bundled = bundled_tree(Path(folder).resolve() / 'bundled')
            with patch.object(resource_paths, 'USER_ROOT', home), \
                 patch.object(resource_paths, 'PROJECT_ROOT', home), \
                 patch.object(resource_paths, 'BUNDLED_ROOT', bundled):
                self.assertFalse((home / 'runtime' / 'releases' / 'active.json').exists())
                self.assertIsNone(resource_paths.read_pointer())
                self.assertEqual(resolve_paths(), (bundled, bundled / 'assets/recognition',
                                                   bundled / 'runtime/node'))
                # Writing a null-active pointer keeps the bundled baseline.
                base = home / 'runtime' / 'releases'
                base.mkdir(parents=True)
                (base / 'active.json').write_text(json.dumps(
                    {'schema_version': 1, 'active': {'revision': None},
                     'history': [{'revision': None}]}), encoding='utf-8')
                self.assertEqual(resolve_paths()[0], bundled)

    def test_explicit_project_root_keeps_single_root_fixture_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            project = bundled_tree(Path(folder).resolve())
            self.assertEqual(resolve_paths(project_root=project),
                             (project, project / 'assets/recognition', project / 'runtime/node'))
            # An explicit root that *does* carry a pointer resolves that release.
            release = build_active_release(project, REVISION_A)
            self.assertEqual(resolve_paths(project_root=project),
                             (release, release / 'assets/recognition', release / 'runtime/node'))


class ProcessPinningTests(unittest.TestCase):
    """Module constants are frozen at import; resolve_paths() sees the new pointer."""

    def test_constants_stay_pinned_while_resolve_paths_follows_new_pointer(self):
        pinned = resource_paths.RESOURCE_ROOT
        pinned_recognition = resource_paths.RECOGNITION_ROOT
        pinned_runtime = resource_paths.RUNTIME_ROOT
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            release_a = build_active_release(home, REVISION_A)
            with patch.object(resource_paths, 'USER_ROOT', home):
                self.assertEqual(resolve_paths(), (release_a, release_a / 'assets/recognition',
                                                   release_a / 'runtime/node'))
                # A later activation (a future process) is observed fresh...
                release_b = build_active_release(home, REVISION_B)
                self.assertEqual(resolve_paths()[0], release_b)
                # ...while this process keeps the versions it imported.
                self.assertEqual(resource_paths.RESOURCE_ROOT, pinned)
                self.assertEqual(resource_paths.RECOGNITION_ROOT, pinned_recognition)
                self.assertEqual(resource_paths.RUNTIME_ROOT, pinned_runtime)

    def test_native_service_snapshot_is_not_repinned(self):
        baseline = native_service.RESOURCE_ROOT
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(resource_paths, 'RESOURCE_ROOT', Path(folder)):
                self.assertEqual(native_service.RESOURCE_ROOT, baseline)
                service = NativeService(user_home=Path(folder))
        self.assertEqual(service.mapping, baseline / 'assets/native/mapping.json')


class ReleaseValidationTests(unittest.TestCase):
    """A malformed/tampered activation is a hard error, never a silent fallback."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self):
        with patch.object(resource_paths, 'USER_ROOT', self.home):
            return resolve_paths()

    def test_redirected_releases_subtree_uses_same_canonical_boundary(self):
        physical = self.home / 'physical'
        release = build_active_release(physical, REVISION_A)
        pointer = json.loads((physical / 'runtime/releases/active.json').read_text('utf-8'))
        logical = self.home / 'logical'
        logical.mkdir()
        boundary = logical / 'runtime/releases'
        original_resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            if path.is_relative_to(boundary):
                path = physical / 'runtime/releases' / path.relative_to(boundary)
            return original_resolve(path, *args, **kwargs)

        # Windows redirects file reads too; the logical parent itself stays put.
        with patch.object(Path, 'resolve', new=redirected), \
                patch.object(resource_paths, 'read_pointer', return_value=pointer):
            self.assertEqual(resolve_paths(project_root=logical)[0], release)

    def test_missing_release_directory_is_refused(self):
        base = self.home / 'runtime' / 'releases'
        base.mkdir(parents=True)
        (base / 'active.json').write_text(json.dumps({
            'schema_version': 1,
            'active': {'revision': REVISION_A, 'release': f'runtime/releases/{REVISION_A}',
                       'release_manifest_sha256': '0' * 64},
            'history': [{'revision': REVISION_A}]}), encoding='utf-8')
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_native_mapping_revision_mismatch_is_refused(self):
        files = standard_files(REVISION_A, mapping_revision=REVISION_OTHER)
        build_active_release(self.home, REVISION_A, files=files)
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_native_mapping_without_provenance_is_refused(self):
        files = standard_files(REVISION_A)
        files['assets/native/mapping.json'] = json.dumps({'entries': []})
        build_active_release(self.home, REVISION_A, files=files)
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_file_hash_tamper_is_refused(self):
        release = build_active_release(self.home, REVISION_A)
        target = release / 'assets' / 'recognition' / 'skill_cards.json'
        target.write_bytes(target.read_bytes() + b' ')
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_unlisted_extra_file_is_refused(self):
        release = build_active_release(self.home, REVISION_A)
        (release / 'assets' / 'recognition' / 'extra.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_path_escape_and_case_collision_are_refused(self):
        def escape(record):
            record['files']['../evil.json'] = '0' * 64

        build_active_release(self.home, REVISION_A, mutate_record=escape)
        with self.assertRaises(ResourceError):
            self.resolve()
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()

        def collide(record):
            record['files']['ASSETS/NATIVE/MAPPING.JSON'] = record['files']['assets/native/mapping.json']

        build_active_release(self.home, REVISION_A, mutate_record=collide)
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_symlinked_release_file_is_refused(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('symlinks unavailable')
        release = build_active_release(self.home, REVISION_A)
        outside = self.home / 'outside.json'
        outside.write_text('[]', encoding='utf-8')
        target = release / 'assets' / 'recognition' / 'skill_cards.json'
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation not permitted')
        with self.assertRaises(ResourceError):
            self.resolve()

    def test_valid_release_resolves_and_pointer_tamper_is_refused(self):
        release = build_active_release(self.home, REVISION_A)
        self.assertEqual(self.resolve()[0], release)
        pointer = self.home / 'runtime' / 'releases' / 'active.json'
        data = json.loads(pointer.read_text(encoding='utf-8'))
        data['active']['release_manifest_sha256'] = '0' * 64
        pointer.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ResourceError):
            self.resolve()


# --------------------------------------------------------------------------- #
# native service defaults
# --------------------------------------------------------------------------- #
class PinnedMappingTests(unittest.TestCase):
    def test_default_mapping_and_catalog_use_pinned_root(self):
        with tempfile.TemporaryDirectory() as folder:
            pinned = Path(folder).resolve()
            mapping = pinned / 'assets' / 'native' / 'mapping.json'
            mapping.parent.mkdir(parents=True)
            mapping.write_text(json.dumps({'provenance': {'engine_revision': REVISION_A}}), encoding='utf-8')
            with patch.object(native_service, 'RESOURCE_ROOT', pinned):
                service = NativeService(user_home=pinned)
        self.assertEqual(service.mapping, mapping)
        self.assertEqual(service.home, pinned)
        # Same pinned recognition catalog the web server and engine use.
        self.assertEqual(service.catalog.root, catalog_module.ASSETS)
        self.assertEqual(catalog_module.ASSETS, resource_paths.RECOGNITION_ROOT)
        self.assertIsInstance(service.catalog.manifest, dict)

    def test_explicit_mapping_catalog_and_user_home_win(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            custom = home / 'custom-mapping.json'
            custom.write_text('{}', encoding='utf-8')
            marker = object()
            service = NativeService(user_home=home / 'data', mapping=custom, catalog=marker)
        self.assertEqual(service.mapping, custom)
        self.assertEqual(service.home, (home / 'data').resolve())
        self.assertIs(service.catalog, marker)
        self.assertEqual(service.export_dir, (home / 'data').resolve() / 'native-export')


# --------------------------------------------------------------------------- #
# server maintenance gate
# --------------------------------------------------------------------------- #
class MaintenanceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gkshot.simulation_web import SimulationServer
        cls.server = SimulationServer(('127.0.0.1', 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def post(self, path, body):
        data = json.dumps(body).encode()
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request('POST', path, body=data, headers={'X-GK-Token': self.server.token})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_start_and_resume_endpoints_reject_while_maintenance(self):
        self.assertFalse(self.server.maintenance.is_set())
        self.assertTrue(self.server.begin_maintenance())
        try:
            self.assertTrue(self.server.busy())
            for path in ('/api/run', '/api/search/start', '/api/search/resume',
                         '/api/season-runs/start', '/api/season-runs/resume'):
                code, raw = self.post(path, {})
                self.assertEqual(code, 409, (path, raw))
                self.assertIn('模拟数据', json.loads(raw)['error'])
            self.assertEqual(self.server.jobs, {})
        finally:
            self.server.end_maintenance()
        self.assertFalse(self.server.maintenance.is_set())
        # The gate is released: the same request now reaches normal validation.
        self.assertEqual(self.post('/api/run', {})[0], 400)

    def test_begin_maintenance_refused_while_engine_work_runs(self):
        job_id, search_id = 'f' * 32, 'a' * 32
        self.server.jobs[job_id] = {'state': 'running', 'cancel': threading.Event(), 'progress': None}
        try:
            self.assertFalse(self.server.begin_maintenance())
            self.assertFalse(self.server.maintenance.is_set())
        finally:
            self.server.jobs.pop(job_id, None)
        self.server.searches[search_id] = {'manifest': {'config': {}},
                                           'snapshot': {'state': 'running'},
                                           'cancel': threading.Event()}
        try:
            self.assertFalse(self.server.begin_maintenance())
        finally:
            self.server.searches.pop(search_id, None)
        self.assertTrue(self.server.begin_maintenance())
        # Already-running work may finish, but no new owner can claim the marker.
        self.assertFalse(self.server.begin_maintenance())
        self.server.end_maintenance()


if __name__ == '__main__':
    unittest.main()
