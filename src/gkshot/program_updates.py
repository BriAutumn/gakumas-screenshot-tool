"""Side-by-side program update check, staging, activation and rollback.

The main bootstrap launcher reads ``user_home/program-active.json`` and starts a
verified ``GKMemoryTool.exe`` from the active version directory.  Versions live
side by side under ``user_home/program-updates/version-<manifesthash12>`` so a
running executable is never overwritten or deleted; the baseline (the bundled
program) is selected whenever the pointer is absent or ``active`` is ``null``.

This module only *prepares* updates:

* :func:`check_release` queries the official GitHub Releases HTTPS API and picks
  the newest published, non-draft, non-prerelease release whose tag carries the
  configured ``memory-tool-`` prefix (old screenshot-tool releases are ignored).
* :func:`download_release` downloads the exact archive and ``.sha256`` assets
  over HTTPS (GitHub hosts only) into an atomic temp file.
* :func:`stage_archive` validates a downloaded ZIP completely -- path
  confinement, duplicate/case-collision and symlink entries, size limits and the
  embedded manifest -- before extracting anything, then atomically renames a
  fresh ``version-<manifesthash12>`` directory into place.
* :func:`activate_staged` re-verifies the staged bytes and atomically rewrites
  the active pointer, preserving the previous active entry (or ``None`` for the
  baseline).
* :func:`rollback` re-verifies the previous entry and activates it, or clears
  the active pointer back to the baseline.  No history directory is deleted.

No shell is launched and no elevation is attempted; restarting the process is
the caller's job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen as _stdlib_urlopen, build_opener, HTTPRedirectHandler

__all__ = [
    "ProgramUpdateError",
    "check_release",
    "download_release",
    "stage_archive",
    "activate_staged",
    "rollback",
    "program_active_path",
]

PRODUCT = "gk-memory-tool"
SCHEMA_VERSION = 1
PROGRAM_UPDATES_DIRNAME = "program-updates"
PROGRAM_ACTIVE_NAME = "program-active.json"
DEFAULT_REPOSITORY = "BriAutumn/gakumas-screenshot-tool"
DEFAULT_TAG_PREFIX = "memory-tool-"

#: 1 GiB total uncompressed payload, 100 MiB per member.
MAX_TOTAL_BYTES = 1 << 30
MAX_MEMBER_BYTES = 100 << 20
#: Bounds for bounded network reads.
MAX_API_BYTES = 8 << 20
MAX_CHECKSUM_BYTES = 64 << 10
MAX_ARCHIVE_BYTES = 1 << 30

_TIMEOUT = 30.0
_USER_AGENT = "gk-memory-tool-updater"

_API_HOSTS = frozenset({"api.github.com"})
_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*\Z")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ProgramUpdateError(RuntimeError):
    """The release feed, archive, staged directory or pointer is unusable."""


def program_active_path(user_home) -> Path:
    """Absolute path of the active-version pointer for ``user_home``."""
    return Path(user_home).resolve() / PROGRAM_ACTIVE_NAME


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------
def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _parse_version(text) -> tuple:
    if not isinstance(text, str) or not _VERSION_RE.fullmatch(text):
        raise ProgramUpdateError(f"版本号不是数字点分格式：{text!r}")
    return tuple(int(part) for part in text.split("."))


def _validate_relative_path(name) -> str:
    """Reject absolute, drive-qualified, backslash or ``..`` confining names."""
    if not isinstance(name, str) or not name:
        raise ProgramUpdateError("清单中的路径必须是非空字符串。")
    if "\x00" in name:
        raise ProgramUpdateError("清单中的路径包含空字节。")
    if "\\" in name:
        raise ProgramUpdateError(f"清单中的路径包含反斜杠：{name!r}")
    if ":" in name:
        raise ProgramUpdateError(f"清单中的路径包含盘符或流标记：{name!r}")
    if name.startswith("/") or Path(name).is_absolute():
        raise ProgramUpdateError(f"清单中的路径不能是绝对路径：{name!r}")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise ProgramUpdateError(f"清单中的路径包含非法片段：{name!r}")
        stem = part.split('.')[0].upper()
        if (part.endswith(('.', ' ')) or any(ord(c) < 32 or c in '<>"|?*' for c in part)
            or stem in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$'}
            or re.fullmatch(r'(?:COM|LPT)[1-9]', stem)):
            raise ProgramUpdateError(f'清单中的路径不适用于 Windows：{name!r}')
    return name


def _parse_manifest(raw: bytes) -> dict:
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProgramUpdateError(f"release.json 无法解析：{error}") from error
    if not isinstance(record, dict):
        raise ProgramUpdateError("release.json 必须是 JSON 对象。")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ProgramUpdateError(
            f"release.json 的 schema_version 不受支持：{record.get('schema_version')!r}"
        )
    if record.get("product") != PRODUCT:
        raise ProgramUpdateError(
            f"release.json 的产品标识不兼容：{record.get('product')!r}"
        )
    version = record.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ProgramUpdateError(f"release.json 的 version 不合法：{version!r}")
    entrypoint = record.get("entrypoint")
    _validate_relative_path(entrypoint)
    files = record.get("files")
    if not isinstance(files, dict) or not files:
        raise ProgramUpdateError("release.json 必须包含非空的 files 映射。")
    cleaned = {}
    for name, digest in files.items():
        _validate_relative_path(name)
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ProgramUpdateError(f"文件哈希不合法：{name!r}")
        cleaned[name] = digest
    if entrypoint not in cleaned:
        raise ProgramUpdateError("release.json 的入口文件未包含在 files 中。")
    return {
        "schema_version": SCHEMA_VERSION,
        "product": PRODUCT,
        "version": version,
        "entrypoint": entrypoint,
        "files": cleaned,
    }


def _verify_staged(directory) -> dict:
    """Re-hash every staged byte; extra or missing files are hard errors."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ProgramUpdateError(f"暂存目录不存在：{directory}")
    manifest_path = directory / "release.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ProgramUpdateError(f"暂存目录缺少 release.json：{directory}")
    raw = manifest_path.read_bytes()
    record = _parse_manifest(raw)
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    present = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or getattr(path, 'is_junction', lambda:False)():
            raise ProgramUpdateError(f"暂存目录包含符号链接：{path}")
        if path.is_dir():
            continue
        present[path.relative_to(directory).as_posix()] = path
    payload = {name: path for name, path in present.items() if name != "release.json"}
    if set(payload) != set(record["files"]):
        raise ProgramUpdateError("暂存目录的文件清单与 release.json 不一致。")
    for name, expected in record["files"].items():
        if _file_sha256(payload[name]) != expected:
            raise ProgramUpdateError(f"暂存文件校验失败：{name}")
    return {
        "directory": str(directory),
        "manifest": record,
        "manifest_sha256": manifest_sha256,
    }


def _stage_result(directory, record, manifest_sha256) -> dict:
    return {
        "directory": str(Path(directory).resolve()),
        "version": record["version"],
        "entrypoint": record["entrypoint"],
        "manifest_sha256": manifest_sha256,
    }


def _atomic_write_json(path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".program-active-", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp_path), str(path))
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(str(temp_path))
            except OSError:
                pass


def _read_pointer(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProgramUpdateError(f"活动指针无法解析：{path}") from error
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ProgramUpdateError(f"活动指针格式不正确：{path}")
    if "active" not in data:
        raise ProgramUpdateError(f"活动指针缺少 active 字段：{path}")
    active = data.get("active")
    if active is not None:
        required = ("directory", "version", "entrypoint", "manifest_sha256")
        if not isinstance(active, dict) or not all(key in active for key in required):
            raise ProgramUpdateError("活动指针的 active 字段格式不正确。")
    previous = data.get("previous")
    if previous is not None and not isinstance(previous, dict):
        raise ProgramUpdateError("活动指针的 previous 字段格式不正确。")
    return data


# --------------------------------------------------------------------------
# HTTPS helpers (injectable for tests)
# --------------------------------------------------------------------------
def _default_opener():
    class PinnedRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _validate_https(newurl, _API_HOSTS | _DOWNLOAD_HOSTS)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener = build_opener(PinnedRedirect())
    def open_url(url, timeout=_TIMEOUT):
        request = Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/vnd.github+json, application/octet-stream",
            },
        )
        return opener.open(request, timeout=timeout)

    return open_url


def _open(opener, url):
    try:
        return opener(url, timeout=_TIMEOUT)
    except TypeError:
        return opener(url)


def _close(response) -> None:
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _validate_https(url, allowed_hosts) -> None:
    if not isinstance(url, str) or not url:
        raise ProgramUpdateError("下载地址缺失。")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ProgramUpdateError(f"只允许 HTTPS 下载地址：{url!r}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ProgramUpdateError(f"下载地址主机不受信任：{url!r}")


def _open_checked(opener, url, allowed_hosts):
    _validate_https(url, allowed_hosts)
    response = _open(opener, url)
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        try:
            final_url = geturl()
        except Exception:
            final_url = None
        if final_url:
            _validate_https(final_url, allowed_hosts)
    return response


def _read_bounded(response, limit) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = response.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ProgramUpdateError("响应内容超过允许的大小。")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch(opener, url, limit, allowed_hosts) -> bytes:
    response = _open_checked(opener, url, allowed_hosts)
    try:
        return _read_bounded(response, limit)
    finally:
        _close(response)


def _parse_checksum(text, expected_name) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        digest = parts[0].lower()
        if not _SHA256_RE.fullmatch(digest):
            continue
        if len(parts) != 2:
            raise ProgramUpdateError('校验文件必须包含哈希及压缩包文件名')
        if len(parts) > 1:
            name = parts[1].strip().lstrip("*")
            name = name.replace("\\", "/").rsplit("/", 1)[-1]
            if name != expected_name:
                raise ProgramUpdateError("校验文件中的文件名与下载文件不一致。")
        return digest
    raise ProgramUpdateError("校验文件格式不正确。")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def check_release(
    current_version: str,
    repository: str = DEFAULT_REPOSITORY,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    *,
    urlopen=None,
) -> dict:
    """Find the newest applicable release from the official GitHub API.

    Only the releases array of the official API is consulted, and only tags
    carrying ``tag_prefix`` are considered, so old screenshot-tool releases are
    never selected.  Drafts and prereleases are skipped.  Each candidate must
    publish the exact ``GKMemoryTool-<version>-windows-x64.zip`` archive and its
    ``.sha256`` companion.  Returns ``{"available": False, "message": ...}`` in
    Chinese when nothing newer is published.
    """
    current = _parse_version(current_version)
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise ProgramUpdateError(f"仓库标识不合法：{repository!r}")
    if not isinstance(tag_prefix, str) or not tag_prefix:
        raise ProgramUpdateError("tag_prefix 必须是非空字符串。")

    url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
    opener = urlopen or _default_opener()
    try:
        body = _fetch(opener, url, MAX_API_BYTES, _API_HOSTS)
    except ProgramUpdateError:
        raise
    except OSError as error:
        raise ProgramUpdateError(f"无法查询发布信息：{error}") from error
    try:
        releases = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProgramUpdateError(f"发布信息无法解析：{error}") from error
    if not isinstance(releases, list):
        raise ProgramUpdateError("发布信息格式不正确。")

    best = None
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith(tag_prefix):
            continue
        raw_version = tag[len(tag_prefix):]
        if not _VERSION_RE.fullmatch(raw_version):
            continue
        version = _parse_version(raw_version)
        if version <= current:
            continue
        assets = {}
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and isinstance(asset.get("name"), str):
                assets[asset["name"]] = asset
        archive_name = f"GKMemoryTool-{raw_version}-windows-x64.zip"
        archive = assets.get(archive_name)
        checksum = assets.get(archive_name + ".sha256")
        if not archive or not checksum:
            continue
        archive_url = archive.get("browser_download_url")
        checksum_url = checksum.get("browser_download_url")
        if not archive_url or not checksum_url:
            continue
        if best is None or version > best[0]:
            best = (version, raw_version, release, archive, checksum)

    if best is None:
        return {
            "available": False,
            "current_version": current_version,
            "message": f"未找到高于 {current_version} 的可用正式版本（{tag_prefix}）。",
        }

    _, raw_version, release, archive, checksum = best
    archive_name = f"GKMemoryTool-{raw_version}-windows-x64.zip"
    return {
        "available": True,
        "current_version": current_version,
        "version": raw_version,
        "tag": release.get("tag_name"),
        "release_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "assets": {
            "archive": {
                "name": archive_name,
                "url": archive.get("browser_download_url"),
                "size": archive.get("size"),
            },
            "checksum": {
                "name": archive_name + ".sha256",
                "url": checksum.get("browser_download_url"),
            },
        },
    }


def download_release(release: dict, updates_dir: Path, *, urlopen=None) -> dict:
    """Download the release archive plus its strict checksum into ``updates_dir``.

    Only the official GitHub HTTPS hosts are accepted (initial URL and any
    redirect target).  The body is streamed into an atomic temp file with a
    hard size bound, verified against the ``.sha256`` companion, and only then
    renamed to its final name.
    """
    if not isinstance(release, dict) or not release.get("available"):
        raise ProgramUpdateError("没有可下载的更新版本。")
    version = release.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ProgramUpdateError(f"版本号格式不正确：{version!r}")
    assets = release.get("assets")
    if not isinstance(assets, dict):
        raise ProgramUpdateError("发布信息缺少资源列表。")
    archive = assets.get("archive")
    checksum = assets.get("checksum")
    if not isinstance(archive, dict) or not isinstance(checksum, dict):
        raise ProgramUpdateError("发布信息缺少压缩包或校验文件。")
    archive_name = f"GKMemoryTool-{version}-windows-x64.zip"
    if archive.get("name") != archive_name:
        raise ProgramUpdateError("压缩包文件名与版本不一致。")
    if checksum.get("name") != archive_name + ".sha256":
        raise ProgramUpdateError("校验文件名与版本不一致。")

    opener = urlopen or _default_opener()
    checksum_body = _fetch(
        opener, checksum.get("url"), MAX_CHECKSUM_BYTES, _DOWNLOAD_HOSTS
    )
    expected = _parse_checksum(checksum_body.decode("utf-8", "replace"), archive_name)

    updates_dir = Path(updates_dir)
    updates_dir.mkdir(parents=True, exist_ok=True)
    final = updates_dir / archive_name
    if final.exists():
        if final.is_file() and _file_sha256(final) == expected:
            return {
                "zip_path": str(final.resolve()),
                "name": archive_name,
                "version": version,
                "sha256": expected,
                "size": final.stat().st_size,
                "reused": True,
            }
        raise FileExistsError(f"目标文件已存在且校验不一致：{final}")

    handle, temp_name = tempfile.mkstemp(
        dir=str(updates_dir), prefix=".download-", suffix=".part"
    )
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(handle, "wb") as sink:
            response = _open_checked(opener, archive.get("url"), _DOWNLOAD_HOSTS)
            try:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ProgramUpdateError("下载内容超过允许的大小。")
                    sink.write(chunk)
                    digest.update(chunk)
            finally:
                _close(response)
            sink.flush()
            os.fsync(sink.fileno())
        if digest.hexdigest() != expected:
            raise ProgramUpdateError("下载的压缩包 SHA-256 校验失败。")
        os.replace(str(temp_path), str(final))
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(str(temp_path))
            except OSError:
                pass
    return {
        "zip_path": str(final.resolve()),
        "name": archive_name,
        "version": version,
        "sha256": expected,
        "size": final.stat().st_size,
        "reused": False,
    }


def stage_archive(zip_path: Path, updates_dir: Path, *, expected_sha256=None) -> dict:
    """Validate and stage a downloaded archive under ``updates_dir``.

    Nothing is extracted until every entry and the embedded manifest have been
    validated.  The finished directory is renamed atomically to
    ``version-<manifesthash12>``; re-staging the identical archive is
    idempotent after re-verifying the existing bytes, while a conflicting
    directory is rejected.
    """
    zip_path = Path(zip_path).resolve()
    updates_dir = Path(updates_dir).resolve()
    if not zip_path.is_file():
        raise ProgramUpdateError(f"更新压缩包不存在：{zip_path}")
    if expected_sha256 is not None:
        expected = str(expected_sha256).strip().lower()
        if not _SHA256_RE.fullmatch(expected):
            raise ProgramUpdateError("expected_sha256 必须是 64 位十六进制字符串。")
        if _file_sha256(zip_path) != expected:
            raise ProgramUpdateError("更新压缩包 SHA-256 校验失败。")

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise ProgramUpdateError(f"更新压缩包无法解析：{error}") from error

    with archive:
        file_entries = {}
        lower_names = {}
        total = 0
        for info in archive.infolist():
            name = info.filename
            if info.is_dir():
                stripped = name[:-1] if name.endswith("/") else name
                if stripped:
                    _validate_relative_path(stripped)
                continue
            _validate_relative_path(name)
            if info.flag_bits & 0x1:
                raise ProgramUpdateError(f"压缩包包含加密条目：{name!r}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ProgramUpdateError(f"压缩包包含符号链接条目：{name!r}")
            if name in file_entries:
                raise ProgramUpdateError(f"压缩包包含重复条目：{name!r}")
            lowered = name.lower()
            if lowered in lower_names:
                raise ProgramUpdateError(
                    f"压缩包包含大小写冲突条目：{name!r} 与 {lower_names[lowered]!r}"
                )
            lower_names[lowered] = name
            if info.file_size > MAX_MEMBER_BYTES:
                raise ProgramUpdateError(f"单个文件超过大小上限：{name!r}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ProgramUpdateError("压缩包解压后总大小超过上限。")
            file_entries[name] = info

        if "release.json" not in file_entries:
            raise ProgramUpdateError("压缩包根目录缺少 release.json。")
        manifest_raw = archive.read(file_entries["release.json"])
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        record = _parse_manifest(manifest_raw)
        listed = record["files"]

        payload = set(file_entries) - {"release.json"}
        missing = sorted(set(listed) - payload)
        if missing:
            raise ProgramUpdateError("压缩包缺少清单列出的文件：" + "、".join(missing))
        extra = sorted(payload - set(listed))
        if extra:
            raise ProgramUpdateError("压缩包包含清单未列出的文件：" + "、".join(extra))

        target = updates_dir / ("version-" + manifest_sha256[:12])
        if target.exists():
            if not target.is_dir():
                raise FileExistsError(f"目标路径已存在且不是目录：{target}")
            try:
                existing = _verify_staged(target)
            except ProgramUpdateError as error:
                raise FileExistsError(
                    f"目标目录已存在但校验失败：{target}"
                ) from error
            if existing["manifest_sha256"] != manifest_sha256:
                raise FileExistsError(f"目标目录内容与压缩包不一致：{target}")
            return _stage_result(
                existing["directory"], existing["manifest"], existing["manifest_sha256"]
            )

        updates_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(updates_dir)))
        try:
            for name, info in file_entries.items():
                destination = temp_dir.joinpath(*name.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                expected = manifest_sha256 if name == "release.json" else listed[name]
                digest = hashlib.sha256()
                with archive.open(info) as source, open(destination, "wb") as sink:
                    while True:
                        chunk = source.read(1 << 20)
                        if not chunk:
                            break
                        sink.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ProgramUpdateError(f"解压内容校验失败：{name!r}")
            os.rename(str(temp_dir), str(target))
            temp_dir = None
        finally:
            if temp_dir is not None:
                shutil.rmtree(str(temp_dir), ignore_errors=True)

    return _stage_result(target.resolve(), record, manifest_sha256)


def activate_staged(staged_dir: Path, user_home: Path) -> dict:
    """Re-verify ``staged_dir`` and atomically point the launcher at it.

    The directory must live beneath ``user_home/program-updates``.  The previous
    active entry is preserved (``None`` means the bundled baseline), so a later
    rollback can return to it.
    """
    user_home = Path(user_home).resolve()
    updates_root = (user_home / PROGRAM_UPDATES_DIRNAME).resolve()
    staged = Path(staged_dir).resolve()
    if staged == updates_root or not staged.is_relative_to(updates_root):
        raise ProgramUpdateError(f"只能激活 {updates_root} 之下的目录：{staged}")

    verified = _verify_staged(staged)
    record = verified["manifest"]
    active = {
        "directory": str(staged),
        "version": record["version"],
        "entrypoint": record["entrypoint"],
        "manifest_sha256": verified["manifest_sha256"],
    }

    pointer_path = user_home / PROGRAM_ACTIVE_NAME
    existing = _read_pointer(pointer_path)
    previous = None
    if existing is not None:
        old_active = existing.get("active")
        if isinstance(old_active, dict) and old_active.get("directory") == active["directory"]:
            previous = existing.get("previous")
            if previous is not None and not isinstance(previous, dict):
                previous = None
        elif isinstance(old_active, dict):
            previous = old_active

    pointer = {
        "schema_version": SCHEMA_VERSION,
        "active": active,
        "previous": previous,
    }
    _atomic_write_json(pointer_path, pointer)
    return pointer


def rollback(user_home: Path) -> dict:
    """Return to the preserved previous version, or clear back to the baseline.

    The previous directory is re-verified before use.  When no previous entry is
    recorded, the active pointer is cleared (``active: null``) so the bootstrap
    launcher falls back to the bundled baseline.  Every history directory is
    left untouched.
    """
    user_home = Path(user_home).resolve()
    pointer_path = user_home / PROGRAM_ACTIVE_NAME
    pointer = _read_pointer(pointer_path)
    if pointer is None:
        raise ProgramUpdateError("没有可回滚的活动版本。")

    previous = pointer.get("previous")
    if isinstance(previous, dict):
        updates_root = (user_home / PROGRAM_UPDATES_DIRNAME).resolve()
        previous_dir = Path(previous.get("directory") or "").resolve()
        if previous_dir == updates_root or not previous_dir.is_relative_to(updates_root):
            raise ProgramUpdateError("上一个版本目录不在更新目录之内。")
        verified = _verify_staged(previous_dir)
        record = verified["manifest"]
        active = {
            "directory": str(previous_dir),
            "version": record["version"],
            "entrypoint": record["entrypoint"],
            "manifest_sha256": verified["manifest_sha256"],
        }
        new_pointer = {
            "schema_version": SCHEMA_VERSION,
            "active": active,
            "previous": None,
        }
    else:
        new_pointer = {
            "schema_version": SCHEMA_VERSION,
            "active": None,
            "previous": None,
        }

    _atomic_write_json(pointer_path, new_pointer)
    return new_pointer
