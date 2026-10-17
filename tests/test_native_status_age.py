"""Derived 'stale_running' status behaviour for a previous, unfinished export.

The stale check is pure wall-clock arithmetic driven by the status file mtime,
so every case here is exercised with a mocked ``time.time`` and ``os.utime``;
no test ever sleeps.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gkshot.native_service import NativeService, STALE_RUNNING_SECONDS

STALE_MESSAGE = '上次导出未完成；本次启动尚无新状态，请提供诊断日志'


class StaleStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.service = NativeService(self.home, mapping=self.home / 'mapping.json', catalog=object())
        self.service.export_dir.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_status(self, payload, mtime):
        path = self.service.export_dir / 'status.json'
        path.write_text(json.dumps(payload), encoding='utf8')
        os.utime(path, (mtime, mtime))
        return path

    def status_at(self, now):
        with patch('gkshot.native_service.time.time', return_value=now):
            return self.service.status()

    def test_fresh_running_is_not_rewritten(self):
        now = 1_000_000.0
        self.write_status({'status': 'running', 'completed': False, 'records': 0,
                           'message': 'waiting for game data (maximum 60 seconds)'}, now - 5)
        info = self.status_at(now)
        self.assertEqual(info['status'], 'running')
        self.assertNotIn('source_status', info)
        self.assertFalse(info['completed'])
        self.assertEqual(info['message'], 'waiting for game data (maximum 60 seconds)')

    def test_stale_running_is_derived_after_threshold(self):
        now = 2_000_000.0
        self.write_status({'status': 'running', 'completed': False, 'records': 0,
                           'message': 'waiting for game data (maximum 60 seconds)'}, now - 300)
        info = self.status_at(now)
        self.assertEqual(info['status'], 'stale_running')
        self.assertEqual(info['source_status'], 'running')
        self.assertFalse(info['completed'])
        self.assertEqual(info['message'], STALE_MESSAGE)
        # The raw status is preserved for callers that want the original value.
        self.assertNotEqual(info['source_status'], info['status'])

    def test_future_mtime_is_never_stale(self):
        now = 3_000_000.0
        self.write_status({'status': 'running', 'completed': False, 'records': 0}, now + 500)
        info = self.status_at(now)
        self.assertEqual(info['status'], 'running')
        self.assertNotIn('source_status', info)
        self.assertLessEqual(info['exported_at'], now + 500)

    def test_completed_status_is_untouched_even_when_old(self):
        now = 4_000_000.0
        self.write_status({'status': 'completed', 'completed': True, 'records': 7,
                           'snapshot_sha256': 'a' * 64, 'message': ''}, now - 100_000)
        info = self.status_at(now)
        self.assertEqual(info['status'], 'completed')
        self.assertTrue(info['completed'])
        self.assertEqual(info['records'], 7)
        self.assertNotIn('source_status', info)
        self.assertEqual(info['message'], '')

    def test_threshold_boundary_is_exclusive(self):
        now = 5_000_000.0
        payload = {'status': 'running', 'completed': False, 'records': 0}
        # Exactly at the threshold is still considered fresh.
        self.write_status(payload, now - STALE_RUNNING_SECONDS)
        self.assertEqual(self.status_at(now)['status'], 'running')
        # One second past it is stale.
        self.write_status(payload, now - STALE_RUNNING_SECONDS - 1)
        self.assertEqual(self.status_at(now)['status'], 'stale_running')

    def test_status_read_does_not_mutate_disk(self):
        now = 6_000_000.0
        path = self.write_status({'status': 'running', 'completed': False, 'records': 0}, now - 400)
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime
        before_listing = sorted(p.name for p in self.service.export_dir.iterdir())

        self.status_at(now)

        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(path.stat().st_mtime, before_mtime)
        self.assertEqual(sorted(p.name for p in self.service.export_dir.iterdir()), before_listing)


if __name__ == '__main__':
    unittest.main()
