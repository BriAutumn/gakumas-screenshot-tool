"""Read-only look-up of the original player memory photo.

The native exporter records the game's own ``ImagePath`` on each
``UserMemoryId``. The game stores those photos as JPEG/PNG blobs under
``%USERPROFILE%\\AppData\\LocalLow\\BANDAI NAMCO Entertainment Inc_\\
gakumas\\photo_storage_cache\\cache_files`` and names each blob by the
``PhotoCacheKey`` exported from the game's own photo URL formatter.

This module never invents a picture. An image is returned only when the
snapshot proof is exact *and* the exported cache key exists under the
fixed cache root. ``ImagePath`` is never opened as a local path or fetched.
Nothing is written, imported or mutated.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from gkshot.app_paths import RESOURCE_ROOT

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024

VENDOR_DIR = 'BANDAI NAMCO Entertainment Inc_'
GAME_DIR = 'gakumas'
CACHE_DIR = 'photo_storage_cache'
CACHE_FILES = 'cache_files'

_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_JPEG_MAGIC = b'\xff\xd8\xff'

_LOAD_HINT = '请在游戏中打开回忆一览，等照片加载完成后重试。'
_REEXPORT_HINT = '请在游戏中查看该回忆后重新导出，并再次读取最新回忆。'

_MISSING_CACHE = '本机没有找到这张回忆的原始照片缓存。' + _LOAD_HINT
_UNSAFE = '照片缓存文件不在受信任的目录内，已拒绝读取。'


def default_cache_root():
    """Return the fixed game cache directory, or None if LOCALAPPDATA is unusable."""
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return None
    base = Path(local)
    if not base.is_absolute():
        return None
    return base.parent / 'LocalLow' / VENDOR_DIR / GAME_DIR / CACHE_DIR / CACHE_FILES


def _result(status, message=None):
    data = {'photo_status': status}
    if message:
        data['photo_message'] = message
    return data


def _metadata_missing():
    return _result('metadata-missing',
                   '此导出没有包含原始照片信息（可能是旧版本导出，或该回忆尚无照片）。' + _REEXPORT_HINT)


def _preset_preview(asset_id, root):
    missing = _result('asset-photo-unavailable', '这张回忆使用游戏内置封面资源，目前尚未接入；可通过下方属性与技能定位。')
    if not isinstance(asset_id, str) or re.fullmatch(r'img_general_memory_[a-z0-9_-]+', asset_id) is None:
        return missing
    try:
        root = Path(root).resolve()
        manifest = root / 'manifest.json'
        if manifest.stat().st_size > 128 * 1024:
            return missing
        entries = json.loads(manifest.read_text(encoding='utf8'))['covers']
        matches = [e for e in entries if e.get('asset_id') == asset_id]
        if len(matches) != 1:
            return missing
        path = (root / (asset_id + '.png')).resolve()
        if not path.is_relative_to(root):
            return _result('unsafe-cache-target', _UNSAFE)
        with path.open('rb') as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        if (len(data) > MAX_IMAGE_BYTES or not data.startswith(_PNG_MAGIC)
                or hashlib.sha256(data).hexdigest() != matches[0]['image_sha256']):
            return _result('image-invalid', '内置回忆封面资源校验失败，请更新工具。')
        return {'photo_status': 'available', 'photo_kind': 'preset', 'photo_mime': 'image/png',
                'photo_sha256': hashlib.sha256(data).hexdigest(),
                'image': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')}
    except (OSError, ValueError, KeyError, TypeError):
        return missing


def native_photo_preview(library, memory_id, ordinal, snapshot_sha256, *, cache_root=None, preset_root=None):
    """Return a status dict for the original photo of one native memory row.

    ``library`` holds the immutable import with ``snapshot.json``;
    ``memory_id``/``ordinal``/``snapshot_sha256`` come from the verified
    native provenance. On success the dict carries ``photo_status``
    ``available`` plus ``image`` (data URL), ``photo_mime`` and
    ``photo_sha256``. Every other outcome carries ``photo_status`` and a
    Chinese ``photo_message`` and never carries ``image``.
    """
    if (not isinstance(memory_id, str) or not memory_id
            or type(ordinal) is not int or ordinal < 1
            or not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64):
        return _result('memory-mismatch', '回忆标识或序号与导出记录不一致，不能读取原始照片。')

    snapshot_path = Path(library).resolve() / 'snapshot.json'
    if not snapshot_path.is_file():
        return _metadata_missing()
    try:
        if snapshot_path.stat().st_size > MAX_SNAPSHOT_BYTES:
            return _result('snapshot-invalid', '导出快照过大，无法校验原始照片。' + _REEXPORT_HINT)
        with snapshot_path.open('rb') as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            return _result('snapshot-invalid', '导出快照过大，无法校验原始照片。' + _REEXPORT_HINT)
    except OSError:
        return _metadata_missing()
    if hashlib.sha256(raw).hexdigest().lower() != snapshot_sha256.lower():
        # A row from an altered or mismatched snapshot must never be read.
        return _result('snapshot-mismatch', '导出快照与已验证来源不一致，不能读取原始照片；请重新导入导出数据。')
    try:
        rows = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError):
        return _result('snapshot-invalid', '导出快照格式不正确，无法读取原始照片。' + _REEXPORT_HINT)
    if not isinstance(rows, list):
        return _result('snapshot-invalid', '导出快照格式不正确，无法读取原始照片。' + _REEXPORT_HINT)

    matches = [(index, row) for index, row in enumerate(rows)
               if isinstance(row, dict) and row.get('UserMemoryId') == memory_id]
    if len(matches) != 1 or matches[0][0] + 1 != ordinal:
        return _result('memory-mismatch', '导出快照中没有与这条回忆唯一对应的记录，不能读取原始照片。')

    row = matches[0][1]
    key = row.get('PhotoCacheKey')
    if not isinstance(key, str) or not key:
        if row.get('AssetId') and not row.get('ImagePath'):
            return _preset_preview(row['AssetId'], preset_root or RESOURCE_ROOT / 'assets/memory-covers')
        return _metadata_missing()
    if re.fullmatch(r'[0-9A-F]{32}', key) is None:
        return _result('metadata-invalid', '导出的照片缓存标识无效，请更新插件后重新导出。')

    root = default_cache_root() if cache_root is None else Path(cache_root)
    if root is None:
        return _result('cache-unconfigured', '无法确定本机游戏照片缓存目录。' + _LOAD_HINT)
    try:
        root_resolved = root.resolve()
    except OSError:
        return _result('cache-missing', '本机没有找到原始照片缓存。' + _LOAD_HINT)
    if not root_resolved.is_dir():
        return _result('cache-missing', '本机没有找到原始照片缓存。' + _LOAD_HINT)

    candidate = root_resolved / key[:2] / key
    try:
        # Resolve before reading so a symlink/reparse point cannot smuggle in
        # a target outside the fixed cache root.
        target = candidate.resolve()
    except OSError:
        return _result('unsafe-cache-target', _UNSAFE)
    if not target.is_relative_to(root_resolved):
        return _result('unsafe-cache-target', _UNSAFE)
    if not target.is_file():
        return _result('cache-missing', _MISSING_CACHE)
    try:
        with open(target, 'rb') as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return _result('cache-missing', _MISSING_CACHE)
    if len(data) > MAX_IMAGE_BYTES:
        return _result('image-too-large', '原始照片文件过大，无法安全显示；请重新导出后再试。')
    if data.startswith(_PNG_MAGIC):
        mime = 'image/png'
    elif data.startswith(_JPEG_MAGIC):
        mime = 'image/jpeg'
    else:
        return _result('image-invalid', '本机照片缓存不是有效的 JPEG/PNG 图片，已拒绝显示。')

    return {'photo_status': 'available', 'photo_kind': 'player', 'photo_mime': mime,
            'photo_sha256': hashlib.sha256(data).hexdigest(),
            'image': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}
