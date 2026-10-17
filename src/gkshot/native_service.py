"""Fixed export discovery and immutable Stage library snapshots."""
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

from gkshot.app_paths import USER_ROOT
from gkshot.catalog import Catalog
from gkshot.native_library import import_native_snapshot
from gkshot.native_memories import CONVERSION_POLICY
# The native mapping ships with the *pinned* resource set, so a process started
# after a data update reads the mapping belonging to its own engine/catalog.
from gkshot.resource_paths import RESOURCE_ROOT

# A status of "running" older than this many seconds is from a previous game
# session that never published a terminal state (for example the game was closed
# before the exporter finished). It is surfaced as "stale_running" so the UI can
# ask for a diagnostic log instead of implying a live export. The threshold is
# comfortably above the exporter's own 180s readiness/watchdog budget so a
# legitimate automatic run is never mislabelled as stale. The comparison is
# plain wall-clock arithmetic (no timezone handling) and a future mtime — clock
# skew or a touched file — is deliberately never classified as stale.
STALE_RUNNING_SECONDS = 240


class NativeService:
    def __init__(self, user_home=USER_ROOT, mapping=None, catalog=None):
        self.home = Path(user_home).resolve()
        self.export_dir = self.home / 'native-export'
        self.mapping = Path(mapping or RESOURCE_ROOT / 'assets/native/mapping.json')
        self.catalog = catalog or Catalog()
        self.lock = threading.Lock()

    def status(self):
        try:
            status_path = self.export_dir / 'status.json'
            info = json.loads(status_path.read_text(encoding='utf-8-sig'))
            if not isinstance(info, dict):
                raise ValueError('导出状态格式不正确')
            info = dict(info)
            stat = status_path.stat()
            info['exported_at'] = stat.st_mtime
            info['export_dir'] = str(self.export_dir)
            # Never classify a future mtime as stale: (time.time() - mtime) is
            # negative then, so it cannot exceed the threshold.
            if info.get('status') == 'running' and (time.time() - stat.st_mtime) > STALE_RUNNING_SECONDS:
                info['source_status'] = 'running'
                info['status'] = 'stale_running'
                info['completed'] = False
                info['message'] = '上次导出未完成；本次启动尚无新状态，请提供诊断日志'
            return info
        except FileNotFoundError:
            return {'status': 'not_found', 'completed': False, 'records': 0,
                    'message': '请先安装插件，再启动游戏并进入主页面', 'export_dir': str(self.export_dir)}

    def import_latest(self):
        with self.lock:
            before = (self.export_dir / 'status.json').read_bytes() if (self.export_dir / 'status.json').is_file() else b''
            status = self.status()
            if status.get('status') != 'completed' or status.get('completed') is not True:
                raise ValueError('没有完整导出：' + status.get('message', status.get('status', 'unknown')))
            raw = (self.export_dir / 'memories.json').read_bytes()
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError('导出文件过大')
            digest = hashlib.sha256(raw).hexdigest()
            if status.get('snapshot_sha256') != digest:
                raise ValueError('导出文件与状态不匹配，请更新插件并重新启动游戏；旧导出可在启动器中手动导入')
            rows = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(rows, list) or type(status.get('records')) is not int or len(rows) != status['records']:
                raise ValueError('导出记录数量与状态不匹配')
            if before != (self.export_dir / 'status.json').read_bytes():
                raise ValueError('游戏正在更新导出，请稍后重试')
            return self._import_bytes(raw)

    def import_file(self, path):
        """Explicit launcher file chooser for old/prototype exports."""
        with self.lock:
            source = Path(path)
            if source.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('导出文件过大')
            return self._import_bytes(source.read_bytes())

    def _import_bytes(self, raw):
        mapping_hash = hashlib.sha256(self.mapping.read_bytes()).hexdigest()
        snapshot_hash = hashlib.sha256(raw).hexdigest()
        key = hashlib.sha256((snapshot_hash + mapping_hash + CONVERSION_POLICY).encode()).hexdigest()[:24]
        destination = self.home / 'data/native-memories' / key
        staging = self.home / 'data/import-staging'
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as temp:
            source = Path(temp) / 'memories.json'
            source.write_bytes(raw)
            manifest = import_native_snapshot(source, self.mapping, destination, self.catalog)
        return {**manifest, 'library': destination.relative_to(self.home).as_posix(),
                'skipped_count': len(manifest.get('skipped', [])),
                'rejected_count': len(manifest['rejected'])}
