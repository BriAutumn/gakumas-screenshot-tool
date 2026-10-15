"""Pin ONE active released resource set per process.

Consumers read :data:`RESOURCE_ROOT`, :data:`RECOGNITION_ROOT` and
:data:`RUNTIME_ROOT` at import time.  A process that imported this module while
the bundled baseline was active keeps using the baseline even after an update is
activated, and a process started after activation keeps using its own pinned
release for its whole life.  This is what lets an old running simulation finish
with exactly the files it validated.

The active pointer lives at ``runtime/releases/active.json``.  A pointer that
exists but is broken (malformed JSON, empty/invalid revision, path escaping
``runtime/releases``, missing release directory, mismatching manifest, tampered
or incomplete release inventory) is a hard error: we never silently fall back to
the bundled baseline, because that would mix data versions without telling
anyone.  When no pointer exists -- or its ``active.revision`` is ``null`` -- the
read-only bundled resources are used, even though the pointer itself lives under
the writable per-user home.

The updater must NOT use the constants below for its own status; it calls
:func:`resolve_paths` directly so that it always observes the current on-disk
pointer instead of the value frozen at import time.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from gkshot.app_paths import USER_ROOT, RESOURCE_ROOT as BUNDLED_ROOT
PROJECT_ROOT = USER_ROOT
RELEASES_RELATIVE = Path("runtime") / "releases"
ACTIVE_NAME = "active.json"
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED_STEMS = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}


class ResourceError(RuntimeError):
    """The active release pointer or release directory is unusable."""


def releases_root(project_root=None) -> Path:
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    # AppData virtualization can redirect only this subtree, not its parent.
    # Canonicalize the full boundary just as we canonicalize the release leaf.
    return (root / RELEASES_RELATIVE).resolve()


def pointer_path(project_root=None) -> Path:
    return releases_root(project_root) / ACTIVE_NAME


def baseline_paths(project_root=None):
    """Return the bundled project resources (revision ``null``)."""
    root = Path(project_root) if project_root is not None else BUNDLED_ROOT
    root = root.resolve()
    return root, root / "assets" / "recognition", root / "runtime" / "node"


def read_pointer(project_root=None):
    """Read the pointer fresh from disk; ``None`` when it does not exist yet."""
    path = pointer_path(project_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ResourceError(f"活动版本指针无法解析：{path}：{error}") from error
    if not isinstance(data, dict):
        raise ResourceError(f"活动版本指针格式不正确：{path}")
    schema = data.get("schema_version")
    if schema != 1:
        raise ResourceError(f"活动版本指针 schema_version 不受支持：{schema!r}")
    if "active" not in data:
        raise ResourceError(f"活动版本指针缺少 active 字段：{path}")
    _validate_history(data)
    return data


def _validate_history(pointer):
    history = pointer.get("history")
    if not isinstance(history, list) or not history:
        raise ResourceError("活动版本指针 history 字段格式不正确。")
    for entry in history:
        if not isinstance(entry, dict) or 'revision' not in entry:
            raise ResourceError("活动版本指针 history 含非对象条目。")
        revision = entry.get("revision")
        if revision is None:
            continue
        if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
            raise ResourceError(f"活动版本指针 history 含非法 revision：{revision!r}")


def _active_entry(pointer):
    active = pointer.get("active")
    if not isinstance(active, dict) or 'revision' not in active:
        raise ResourceError("活动版本指针的 active 字段格式不正确。")
    return active


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path) -> bool:
    """A symlink or (on Python 3.12+) an NTFS junction/reparse point."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _validate_relative_path(name) -> str:
    """Reject absolute, drive-qualified, backslash or Windows-unsafe names."""
    if not isinstance(name, str) or not name:
        raise ResourceError("活动版本清单中的路径必须是非空字符串。")
    if "\x00" in name or "\\" in name or ":" in name:
        raise ResourceError(f"活动版本清单中的路径包含非法字符：{name!r}")
    if name.startswith("/") or Path(name).is_absolute():
        raise ResourceError(f"活动版本清单中的路径不能是绝对路径：{name!r}")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise ResourceError(f"活动版本清单中的路径包含非法片段：{name!r}")
        stem = part.split('.')[0].upper()
        if (part.endswith(('.', ' ')) or any(ord(c) < 32 or c in '<>"|?*' for c in part)
                or stem in _RESERVED_STEMS
                or re.fullmatch(r'(?:COM|LPT)[1-9]', stem)):
            raise ResourceError(f"活动版本清单中的路径不适用于 Windows：{name!r}")
    return name


def _load_release_record(release, revision, expected_manifest_sha):
    manifest = release / "release.json"
    if _is_link(manifest) or not manifest.is_file():
        raise ResourceError(f"活动版本缺少 release.json：{release}")
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha:
        raise ResourceError('活动版本清单校验失败。')
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ResourceError(f"活动版本 release.json 无法解析：{error}") from error
    if not isinstance(record, dict):
        raise ResourceError('活动版本 release.json 必须是 JSON 对象。')
    if record.get("schema_version") != 1:
        raise ResourceError(f"活动版本 release.json schema_version 不受支持：{record.get('schema_version')!r}")
    if record.get("revision") != revision:
        raise ResourceError('活动版本 release.json 的 revision 与指针不一致。')
    if (record.get("validation") or {}).get("status") != "passed":
        raise ResourceError('活动版本未记录通过验证，拒绝使用。')
    return record


def _verify_inventory(release, record) -> dict:
    """Every listed file must exist with the exact SHA; extras are refused."""
    files = record.get("files")
    if not isinstance(files, dict) or not files:
        raise ResourceError('活动版本 release.json 缺少非空的 files 清单。')
    lowered = {}
    for name, digest in files.items():
        _validate_relative_path(name)
        if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
            raise ResourceError(f"活动版本清单中的哈希不合法：{name!r}")
        key = name.lower()
        if key in lowered:
            raise ResourceError(f"活动版本清单存在大小写冲突：{name!r} 与 {lowered[key]!r}")
        lowered[key] = name
    present = {}
    for path in release.rglob("*"):
        if _is_link(path):
            raise ResourceError(f"活动版本包含符号链接或联结：{path}")
        if path.is_dir():
            continue
        present[path.relative_to(release).as_posix()] = path
    present.pop("release.json", None)
    missing = sorted(set(files) - set(present))
    if missing:
        raise ResourceError('活动版本缺少清单列出的文件：' + '、'.join(missing))
    extra = sorted(set(present) - set(files))
    if extra:
        raise ResourceError('活动版本包含清单未列出的文件：' + '、'.join(extra))
    for name, digest in files.items():
        resolved = (release / name).resolve()
        if not resolved.is_relative_to(release) or _sha256_file(resolved) != digest:
            raise ResourceError(f"活动版本文件校验失败：{name!r}")
    return files


def _verify_manifest_revision(release, files, label: str, relative: str, revision: str):
    directory = release / Path(relative).parent
    if not directory.is_dir():
        raise ResourceError(f"活动版本缺少 {label}：{release}")
    if relative not in files:
        raise ResourceError(f"活动版本清单缺少 {relative}。")
    path = release / relative
    if _is_link(path) or not path.is_file():
        raise ResourceError(f"活动版本缺少 {label} 清单。")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ResourceError(f"活动版本 {label} 清单无法解析：{error}") from error
    if not isinstance(manifest, dict) or manifest.get("revision") != revision:
        raise ResourceError(f"活动版本 {label} 数据版本不匹配。")


def _verify_native_mapping(release, files, revision: str):
    relative = "assets/native/mapping.json"
    if relative not in files:
        raise ResourceError('活动版本清单缺少原生映射 assets/native/mapping.json。')
    path = release / relative
    if not path.is_file():
        raise ResourceError('活动版本缺少原生映射文件。')
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ResourceError(f"活动版本原生映射无法解析：{error}") from error
    if not isinstance(mapping, dict):
        raise ResourceError('活动版本原生映射必须是 JSON 对象。')
    provenance = mapping.get("provenance")
    if not isinstance(provenance, dict):
        raise ResourceError('活动版本原生映射缺少 provenance。')
    reference = provenance.get("catalog_revision") or provenance.get("engine_revision")
    if reference != revision:
        raise ResourceError('活动版本原生映射的 provenance 与引擎版本不一致。')


def resolve_paths(project_root=None):
    """Resolve ``(resource_root, recognition_root, runtime_root)``.

    ``active.revision`` of ``null`` (or an absent pointer) selects the bundled
    baseline.  Anything else is validated strictly under ``runtime/releases``;
    an empty or malformed revision, a broken pointer or an incomplete/tampered
    release is an error, never a fallback.

    Without an explicit ``project_root`` the bundled baseline is the read-only
    shipped resources, not per-user copies under the writable home.  An explicit
    ``project_root`` keeps the single-root layout used by tests.
    """
    explicit = project_root is not None
    root = Path(project_root).resolve() if explicit else Path(USER_ROOT).resolve()
    pointer = read_pointer(root)
    active = _active_entry(pointer) if pointer else None
    if active is None:
        return baseline_paths(root if explicit else None)
    revision = active.get("revision")
    if revision is None:
        return baseline_paths(root if explicit else None)
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise ResourceError(f"活动版本指针的 revision 不是 40 位小写十六进制：{revision!r}")
    releases = releases_root(root)
    relative = active.get("release")
    if relative in (None, ""):
        relative = str(RELEASES_RELATIVE / revision)
    if not isinstance(relative, str):
        raise ResourceError("活动版本指针的 release 字段格式不正确。")
    release_path = root / Path(relative)
    release = release_path.resolve()
    if release == releases or not release.is_relative_to(releases):
        raise ResourceError(f"活动版本目录不在 runtime/releases 之内：{release}")
    if release.parent != releases:
        raise ResourceError(f"活动版本目录必须是 runtime/releases/<revision>：{release}")
    if release.name != revision:
        raise ResourceError("活动版本目录名与指针中的 revision 不一致。")
    if _is_link(release_path) or not release.is_dir():
        raise ResourceError(f"活动版本目录不存在或是链接：{release}")
    expected = active.get("release_manifest_sha256")
    if not isinstance(expected, str) or not _SHA256_PATTERN.fullmatch(expected):
        raise ResourceError('活动版本指针缺少有效的 release_manifest_sha256。')
    record = _load_release_record(release, revision, expected)
    files = _verify_inventory(release, record)
    _verify_manifest_revision(release, files, "assets/recognition", "assets/recognition/manifest.json", revision)
    _verify_manifest_revision(release, files, "runtime/node", "runtime/node/manifest.json", revision)
    _verify_native_mapping(release, files, revision)
    recognition = release / "assets" / "recognition"
    runtime = release / "runtime" / "node"
    return release, recognition, runtime


# Frozen for the lifetime of this process.  A broken pointer raises here.
RESOURCE_ROOT, RECOGNITION_ROOT, RUNTIME_ROOT = resolve_paths()
