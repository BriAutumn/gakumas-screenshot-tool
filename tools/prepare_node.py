#!/usr/bin/env python3
"""Prepare the bundled Node.js runtime without committing ``node.exe`` to Git.

``node.exe`` is intentionally excluded from Git (the win-x64 binary is roughly
92 MB).  This script materialises the exact, pinned Node.js runtime files that
ship next to the read-only engine sources:

* ``node-v24.16.0-win-x64/node.exe`` -> ``runtime/node-bin/node.exe``
* ``node-v24.16.0-win-x64/LICENSE``  -> ``runtime/node-bin/LICENSE``

Only those two exact members of the official archive are ever extracted.  The
whole archive is hashed first, then each member, and a mismatching existing
output file is refused instead of overwritten.  Re-running with an already
correct runtime is a no-op and needs no network.  Nothing outside
``runtime/node-bin`` is written, and no user data is touched.

Usage::

    python tools/prepare_node.py
    python tools/prepare_node.py --archive C:\\downloads\\node-v24.16.0-win-x64.zip

The archive is always taken from the fixed official HTTPS URL, or from a local
file passed with ``--archive``; an arbitrary remote URL is never accepted.  The
script only uses the standard library and does not require Windows to run
preparation (the prepared ``node.exe`` is validated on Windows when used).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

NODE_VERSION = "24.16.0"
NODE_ARCH = "win-x64"
ARCHIVE_ROOT = f"node-v{NODE_VERSION}-{NODE_ARCH}"
ARCHIVE_NAME = ARCHIVE_ROOT + ".zip"
ARCHIVE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{ARCHIVE_NAME}"
ARCHIVE_HOST = "nodejs.org"
ARCHIVE_SHA256 = "edaca9bd58ec8e92037dac4e877d52f6b8f430b81c18b57e264b4e2fb111cd56"

#: archive member name -> (SHA-256, destination file name inside node-bin)
MEMBERS = {
    f"{ARCHIVE_ROOT}/node.exe": (
        "b3094d0b49f9ad602262a9921551737bb97637c05dd357a06ae98188d7290aa3",
        "node.exe",
    ),
    f"{ARCHIVE_ROOT}/LICENSE": (
        "8efdacdc1cfa3460aeb7fe98e3c54337b971d5da70e6eee292b73b981acb220c",
        "LICENSE",
    ),
}

#: Hard bounds so a hostile or truncated archive or member can never fill disk.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
_CHUNK = 1 << 20

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = REPO_ROOT / "runtime" / "node-bin"


class PrepareError(RuntimeError):
    """The archive, a member, an existing output file or a path is unusable."""


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------
def _is_link(path: Path) -> bool:
    """Return ``True`` for a symlink or a Windows junction/reparse point."""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            return bool(checker(path))
        except OSError:
            return True
    path_checker = getattr(path, "is_junction", None)
    if callable(path_checker):
        try:
            return bool(path_checker())
        except OSError:
            return True
    return False


def _assert_within_repo(path: Path, repo_root: Path) -> Path:
    """Reject a target that is not a plain path inside the canonical repo root.

    Every existing component between ``repo_root`` and ``path`` must be a
    directory reached without a symlink or junction, and the resolved target
    must stay inside the resolved repo root.  This keeps the promised outputs
    inside the repository even when ``runtime`` or ``node-bin`` is redirected.
    """
    repo_real = Path(os.path.realpath(str(repo_root)))
    absolute = Path(os.path.abspath(str(path)))
    try:
        relative = absolute.relative_to(repo_root)
    except ValueError as error:
        raise PrepareError(f"拒绝写入仓库之外的路径：{absolute}") from error
    current = repo_root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and _is_link(current):
            raise PrepareError(f"拒绝使用包含符号链接/联接的路径：{current}")
    resolved = Path(os.path.realpath(str(absolute)))
    if resolved != repo_real and repo_real not in resolved.parents:
        raise PrepareError(f"拒绝写入仓库之外的路径：{resolved}")
    return absolute


def _ensure_directory(directory: Path, repo_root: Path) -> None:
    """Create ``directory`` after proving no component escapes the repo root."""
    _assert_within_repo(directory, repo_root)
    current = repo_root
    for part in directory.relative_to(repo_root).parts:
        current = current / part
        if os.path.lexists(str(current)):
            if _is_link(current):
                raise PrepareError(f"拒绝使用包含符号链接/联接的路径：{current}")
            if not current.is_dir():
                raise PrepareError(f"目标路径不是目录：{current}")
        else:
            current.mkdir()
    _assert_within_repo(directory, repo_root)


# ---------------------------------------------------------------------------
# archive acquisition and validation
# ---------------------------------------------------------------------------
def _validate_archive_url(url: str) -> None:
    """Accept only ``https://nodejs.org/...`` exactly (no substring matches)."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ARCHIVE_HOST:
        raise PrepareError(f"下载地址必须是 https://{ARCHIVE_HOST} 下的官方地址：{url}")


def _verify_archive(path: Path) -> None:
    """Validate the whole archive before any member is read or extracted."""
    if not path.is_file():
        raise PrepareError(f"压缩包不存在：{path}")
    size = path.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        raise PrepareError(f"压缩包超过大小上限：{size} 字节")
    if _sha256_file(path) != ARCHIVE_SHA256:
        raise PrepareError("压缩包 SHA-256 与固定值不一致，拒绝使用")


def _download_archive(destination: Path) -> None:
    """Fetch the fixed official archive over HTTPS into ``destination``."""
    _validate_archive_url(ARCHIVE_URL)
    request = urllib.request.Request(
        ARCHIVE_URL, headers={"User-Agent": "gk-memory-tool-prepare-node"}
    )
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except (urllib.error.URLError, OSError) as error:
        raise PrepareError(f"无法下载官方 Node.js 压缩包：{error}") from error
    digest = hashlib.sha256()
    total = 0
    try:
        final_url = response.geturl()
        _validate_archive_url(final_url)
        with open(destination, "wb") as sink:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise PrepareError("下载的 Node 压缩包超过大小上限")
                sink.write(chunk)
                digest.update(chunk)
            sink.flush()
            os.fsync(sink.fileno())
    finally:
        response.close()
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise PrepareError("Node 压缩包 SHA-256 校验失败（可能下载不完整）")


def _read_member(archive: zipfile.ZipFile, member: str) -> bytes:
    """Read one exact archive member, bounded by size; never a general extract."""
    try:
        info = archive.getinfo(member)
    except KeyError as error:
        raise PrepareError(f"压缩包缺少成员：{member}") from error
    if info.is_dir():
        raise PrepareError(f"压缩包成员不是文件：{member}")
    if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
        raise PrepareError(f"压缩包成员超过大小上限：{member}")
    if info.compress_size < 0 or info.compress_size > MAX_ARCHIVE_BYTES:
        raise PrepareError(f"压缩包成员压缩大小超过上限：{member}")
    with archive.open(info) as source:
        data = source.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise PrepareError(f"压缩包成员超过大小上限：{member}")
    if len(data) != info.file_size:
        raise PrepareError(f"压缩包成员大小不一致：{member}")
    return data


# ---------------------------------------------------------------------------
# output files
# ---------------------------------------------------------------------------
def _check_existing(target: Path, expected: str) -> bool:
    """Return ``True`` when ``target`` already holds the expected bytes.

    A present-but-different (or redirected) file is a hard error: we refuse to
    overwrite it so an unrelated file is never clobbered.
    """
    if not os.path.lexists(str(target)):
        return False
    if _is_link(target) or not target.is_file():
        raise PrepareError(f"目标路径已存在且不是普通文件：{target}")
    if _sha256_file(target) != expected:
        raise PrepareError(
            f"已存在的 {target} 与固定哈希不一致，拒绝覆盖；请先检查或手动移除。"
        )
    return True


def _atomic_install(target: Path, data: bytes, expected: str) -> None:
    """Publish ``data`` to ``target`` atomically and without clobbering.

    A self-created temp file in the same directory is hard-linked onto the
    target (``os.link`` is atomic and fails when the target already exists).
    If a file appeared concurrently, its exact content decides the outcome: a
    matching file is retained untouched, a mismatching one stays unchanged and
    the operation fails.  Only the temp file this call created is removed.
    """
    handle, temp_name = tempfile.mkstemp(
        prefix=".prepare-node-", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as sink:
            sink.write(data)
            sink.flush()
            os.fsync(sink.fileno())
        try:
            os.link(str(temp_path), str(target))
        except FileExistsError:
            if _is_link(target) or not target.is_file() or _sha256_file(target) != expected:
                raise PrepareError(
                    f"已存在且校验不一致的文件，拒绝覆盖：{target}"
                ) from None
            # A correct file won the race; keep it exactly as it is.
        except OSError as error:
            raise PrepareError(f"无法原子安装 {target}：{error}") from error
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def prepare(archive: Path | None = None, *, repo_root: Path = REPO_ROOT) -> None:
    """Materialise the pinned Node runtime, downloading only when needed."""
    target_dir = repo_root / "runtime" / "node-bin"
    # Prove the promised output tree cannot escape the repository before we
    # read, create or write anything.
    _assert_within_repo(target_dir, repo_root)

    pending: dict[str, Path] = {}
    for member, (expected, name) in MEMBERS.items():
        target = target_dir / name
        if not _check_existing(target, expected):
            pending[member] = target

    if not pending:
        print(f"Node 运行时已就绪（{NODE_VERSION} {NODE_ARCH}）：{target_dir}")
        return

    temp_archive: Path | None = None
    try:
        if archive is not None:
            source = Path(archive).expanduser()
            if not source.is_file():
                raise PrepareError(f"指定的压缩包不存在：{source}")
        else:
            handle, temp_name = tempfile.mkstemp(prefix="prepare-node-", suffix=".zip")
            os.close(handle)
            temp_archive = Path(temp_name)
            _download_archive(temp_archive)
            source = temp_archive

        _verify_archive(source)

        try:
            archive_file = zipfile.ZipFile(source)
        except (zipfile.BadZipFile, OSError) as error:
            raise PrepareError(f"压缩包无法解析：{error}") from error

        with archive_file:
            payload: dict[str, bytes] = {}
            for member, _target in pending.items():
                data = _read_member(archive_file, member)
                if _sha256_bytes(data) != MEMBERS[member][0]:
                    raise PrepareError(f"压缩包成员校验失败：{member}")
                payload[member] = data

        # Only now create the output directory, and publish each file
        # individually and atomically.
        _ensure_directory(target_dir, repo_root)
        for member, target in pending.items():
            _atomic_install(target, payload[member], MEMBERS[member][0])
    finally:
        if temp_archive is not None:
            try:
                temp_archive.unlink()
            except OSError:
                pass

    print(f"已准备 Node 运行时（{NODE_VERSION} {NODE_ARCH}）：{target_dir}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="准备固定的 Node.js 运行时（node.exe 不纳入 Git）"
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help=f"使用已下载的官方 {ARCHIVE_NAME}，而不是联网下载",
    )
    args = parser.parse_args(argv)
    try:
        prepare(args.archive)
    except PrepareError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
