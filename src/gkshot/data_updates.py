"""Transactional direct-from-upstream updater for the native memory tool.

The updater fetches the engine, catalog, icons and native mapping straight from
public upstream sources, validates everything in a staging sibling directory and
only then atomically publishes a new release directory and rewrites the active
pointer.  Nothing is ever fetched "latest" during an install: the caller passes
explicit 40-hex revisions that were previously returned by :meth:`DataUpdater.check`.

Layout produced for ``user_home``::

    runtime/releases/<engineSHA>/assets/recognition     catalog + icons
    runtime/releases/<engineSHA>/runtime/node           engine runtime
    runtime/releases/<engineSHA>/assets/native/mapping.json
    runtime/releases/<engineSHA>/release.json
    runtime/releases/active.json                        the active pointer

``active.json`` (schema 1)::

    {"schema_version": 1,
     "active":   {"revision": <sha>, "release": "runtime/releases/<sha>",
                  "release_manifest_sha256": <sha256>},
     "history":  [{"revision": null}, {"revision": <sha>}, ...]}

``history`` is the chronological list of activations with the currently active
entry last; the baseline is represented by ``{"revision": null}``.  A failed
install or rollback always leaves the pointer, completed release directories and
the user's library untouched.

Safety properties implemented here:

* staging is a sibling of the final directory and published with ``os.replace``;
* the whole install/rollback runs under an exclusive cross-process lock;
* every staged byte is re-hashed before the pointer moves;
* relative paths are confined, symlinks/junctions and case collisions rejected;
* selected upstream blobs are fetched from ``raw.githubusercontent.com`` at the
  explicit revision and verified against their Git blob SHA-1 *and* tree size --
  the rate-limited base64 ``git/blobs`` API is never used; only small metadata
  (default branch, commit SHA, recursive tree) touches the API host;
* every catalog id gets a real icon plan built from the fetched engine tree; a
  known bundled icon is retained byte-for-byte when upstream is unavailable, so
  existing icons never regress, and only genuinely absent icons are warned about;
* the bundled copy of a file is reused whenever its blob SHA-1 matches upstream;
* upstream regression tests and a worker/reference parity smoke run must pass;
* existing bundled "owned memory" mappings must keep resolving to the same
  stable simulator identities, otherwise the update fails instead of silently
  dropping them;
* the multi-megabyte native YAML sources are cached outside the immutable
  release directory, never inside it.

Network access, hashing, subprocess execution and the node smoke checks are all
seamed through small overridable methods/objects so callers (and tests) can
inject deterministic behaviour.  No shell, npm or system Git is required.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gkshot import app_paths, resource_paths
from gkshot.catalog import Catalog
from gkshot.models import Loadout
from gkshot.native_mapping_builder import build as build_native_mapping

__all__ = [
    "DataUpdateError",
    "DataUpdater",
    "GitHubClient",
    "SOURCE_REPOSITORY",
    "MASTER_REPOSITORY",
    "raw_url",
    "YAML_TABLES",
]

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
RELEASES_RELATIVE_POSIX = "runtime/releases"
ACTIVE_NAME = "active.json"
CACHE_DIRNAME = "data-update-cache"
LOCK_NAME = ".update.lock"

#: Canonical repository; the legacy ``surisuririsu`` name redirects here.
SOURCE_REPOSITORY = "gakumas-tools/gakumas-tools"
#: Secondary public source for native game identifiers.
MASTER_REPOSITORY = "vertesan/gakumasu-diff"

API_HOSTS = frozenset({"api.github.com"})
RAW_HOSTS = frozenset({"raw.githubusercontent.com"})

_USER_AGENT = "gk-memory-tool-data-updater"
_TIMEOUT = 30.0

MAX_BLOB_BYTES = 64 << 20
MAX_TOTAL_BYTES = 512 << 20
MAX_API_BYTES = 32 << 20
MAX_TREE_ENTRIES = 200_000

UPSTREAM_TEST_TIMEOUT = 900.0
SMOKE_TIMEOUT = 300.0
SMOKE_TRIALS = 3
SMOKE_SEED = 610397104

#: Selected runtime sources (all small tracked blobs under these prefixes).
RUNTIME_PREFIXES = ("packages/gakumas-engine/", "packages/gakumas-data/")
RUNTIME_EXTRA_FILES = (
    "LICENSE",
    "scripts/extensionless-loader.mjs",
    "tests/lib.mjs",
    "tests/run.mjs",
    "tests/suite.jsonl",
    "gakumas-tools/utils/stamina.js",
    "gakumas-tools/contexts/LoadoutContext.js",
)
#: Trusted local files taken from the bundled runtime, never from upstream.
TRUSTED_RUNTIME_FILES = ("worker.mjs", "reference.mjs", "package.json")

CATALOG_SOURCE_DIR = "packages/gakumas-data/json"
CATALOG_TABLES = ("skill_cards", "p_items", "stages", "customizations")
TABLE_FIELDS = {
    "skill_cards": ("id", "name", "upgraded", "availableCustomizations"),
    "p_items": ("id", "name", "mode", "upgraded"),
    "stages": ("id", "name", "type"),
    "customizations": ("id", "name", "max"),
}

#: Icon directory -> default upstream template (``<ID>`` placeholder, optional
#: ``[_1]`` alternative suffix).  The official tree still ships legacy PNGs at
#: these paths, so no submodule / WebP conversion is required.
ICON_SPECS = (
    ("card-icons", "packages/gakumas-images/images/skillCards/icons/<ID>[_1].png"),
    ("pitem-icons", "packages/gakumas-images/images/pItems/icons/<ID>[_1].png"),
)

YAML_TABLES = (
    "ProduceCard",
    "ProduceItem",
    "IdolCard",
    "ProduceCardCustomize",
    "ProduceCardGrowEffect",
    "ProduceExamEffect",
    "ProduceDescriptionProduceCardGrowEffect",
    "ProduceExamTrigger",
    "Character",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")


class DataUpdateError(ValueError):
    """The upstream source, staged data or active pointer is unusable."""


# ---------------------------------------------------------------------------
# byte helpers
# ---------------------------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha1(data: bytes) -> str:
    """Git's content-addressed blob hash, used to verify upstream tree entries."""
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


def _is_hex40(value) -> bool:
    return isinstance(value, str) and bool(_SHA1_RE.fullmatch(value))


def _require_revision(value, label: str) -> str:
    if not _is_hex40(value):
        raise DataUpdateError(f"{label} 必须是 40 位小写十六进制提交号：{value!r}")
    return value


def _validate_relative_path(name) -> str:
    """Reject absolute, drive-qualified, backslash or ``..`` confining names."""
    if not isinstance(name, str) or not name:
        raise DataUpdateError("路径必须是非空字符串。")
    if "\x00" in name:
        raise DataUpdateError("路径包含空字节。")
    if "\\" in name:
        raise DataUpdateError(f"路径包含反斜杠：{name!r}")
    if ":" in name:
        raise DataUpdateError(f"路径包含盘符或流标记：{name!r}")
    if name.startswith("/") or Path(name).is_absolute():
        raise DataUpdateError(f"路径不能是绝对路径：{name!r}")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise DataUpdateError(f"路径包含非法片段：{name!r}")
        stem = part.split(".")[0].upper()
        if (
            part.endswith((".", " "))
            or any(ord(c) < 32 or c in '<>"|?*' for c in part)
            or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)
        ):
            raise DataUpdateError(f"路径不适用于 Windows：{name!r}")
    return name


def _expand_template(template: str, icon_id: int) -> list:
    """Expand an icon template into concrete candidate paths (``[_1]`` first)."""
    if "<ID>" not in template:
        return [_validate_relative_path(template)] if template else []
    body = template.replace("<ID>", str(icon_id))
    if "[_1]" in body:
        candidates = [body.replace("[_1]", "_1"), body.replace("[_1]", "")]
    else:
        candidates = [body]
    return [_validate_relative_path(candidate) for candidate in candidates if candidate]


def raw_url(repository: str, revision: str, path: str) -> str:
    """Pinned raw content URL for one tracked path at one explicit revision."""
    return (
        f"https://raw.githubusercontent.com/{repository}/{revision}/"
        f"{quote(path, safe='/')}"
    )


def _walk_files(directory) -> dict:
    """Map relative POSIX paths to files, rejecting symlinks and case clashes."""
    directory = Path(directory)
    present = {}
    lowered = {}
    if not directory.is_dir():
        return present
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise DataUpdateError(f"目录包含符号链接：{path}")
        if path.is_dir():
            continue
        rel = path.relative_to(directory).as_posix()
        _validate_relative_path(rel)
        low = rel.lower()
        if low in lowered:
            raise DataUpdateError(f"目录包含大小写冲突：{rel!r} 与 {lowered[low]!r}")
        lowered[low] = rel
        present[rel] = path
    return present


def _atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
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


def _atomic_write_json(path, payload) -> None:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _atomic_write_bytes(path, data)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DataUpdateError(f"无法读取 JSON：{path}：{error}") from error


def _validate_url(url, allowed_hosts, label="下载地址") -> None:
    if not isinstance(url, str) or not url:
        raise DataUpdateError(f"{label}缺失。")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise DataUpdateError(f"只允许 HTTPS 地址：{url!r}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise DataUpdateError(f"{label}主机不受信任：{url!r}")


def _query_from_loadout(loadout) -> str:
    """Reconstruct the upstream loadout URL query (``loadoutFromQuery`` shape).

    The canonical URL keys are produced by the shared GKTools adapter when the
    integration ships one; otherwise we reproduce the exact same query shape
    (``stage``/``support_bonus``/``params``/``items``/``cards``/``customizations``).
    """
    try:  # pragma: no cover - only present in the full integration
        from gkshot.gktools_url import GKToolsAdapter  # type: ignore
    except Exception:
        GKToolsAdapter = None
    if GKToolsAdapter is not None:  # pragma: no cover - exercised by integration
        try:
            url = GKToolsAdapter().to_url(loadout)
            if isinstance(url, str) and "?" in url:
                return urlsplit(url).query
        except Exception:
            pass
    parameters = loadout.parameters
    params = f"{parameters.vocal}-{parameters.dance}-{parameters.visual}-{parameters.stamina}"
    items = "-".join(str(value) for value in loadout.item_ids)
    cards = "_".join("-".join(str(value) for value in row) for row in loadout.card_ids)
    customs = "_".join("-".join(token or "" for token in row) for row in loadout.customizations)
    return (
        f"stage={loadout.stage_id}&support_bonus={loadout.support_bonus}"
        f"&params={params}&items={items}&cards={cards}&customizations={customs}"
    )


def _default_opener():
    class PinnedRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _validate_url(newurl, API_HOSTS | RAW_HOSTS)
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


# ---------------------------------------------------------------------------
# cross-process lock
# ---------------------------------------------------------------------------
def _lock_handle(handle) -> None:
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            raise DataUpdateError("另一个数据更新正在进行，请稍后再试") from error
    try:
        import msvcrt
    except ImportError:
        return
    try:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        raise DataUpdateError("另一个数据更新正在进行，请稍后再试") from error


def _unlock_handle(handle) -> None:
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        import msvcrt
    except ImportError:
        return
    try:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# network client
# ---------------------------------------------------------------------------
class GitHubClient:
    """Bounded, read-only HTTPS client for the public GitHub metadata + raw host.

    File bytes are fetched from ``raw.githubusercontent.com`` at a pinned
    revision and verified by the caller against the Git blob SHA-1; only small
    metadata (default branch, commit SHA, recursive tree) uses the API.  This
    avoids the unauthenticated 60 requests/hour limit and the base64 newline
    pitfalls of the ``git/blobs`` endpoint.
    """

    def __init__(self, opener=None, timeout=_TIMEOUT, user_agent=_USER_AGENT):
        self._timeout = timeout
        self._user_agent = user_agent
        self._opener = opener or _default_opener()

    # -- public surface used by DataUpdater --------------------------------
    def default_branch(self, repository: str) -> str:
        data = self._api(f"/repos/{repository}")
        branch = data.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise DataUpdateError("上游仓库未返回默认分支。")
        return branch

    def commit_sha(self, repository: str, ref: str) -> str:
        data = self._api(f"/repos/{repository}/commits/{quote(ref, safe='')}")
        sha = data.get("sha")
        if not _is_hex40(sha):
            raise DataUpdateError("上游提交号不合法。")
        return sha

    def tree(self, repository: str, revision: str) -> list:
        data = self._api(f"/repos/{repository}/git/trees/{revision}?recursive=1")
        if data.get("truncated"):
            raise DataUpdateError("上游目录树被截断（recursive API），拒绝使用。")
        entries = data.get("tree")
        if not isinstance(entries, list):
            raise DataUpdateError("上游目录树格式不正确。")
        return entries

    def raw(self, repository: str, revision: str, path: str, limit: int = MAX_BLOB_BYTES) -> bytes:
        """Fetch one tracked blob from the pinned raw host (no API quota)."""
        _validate_relative_path(path)
        _require_revision(revision, "revision")
        return self._fetch(raw_url(repository, revision, path), limit, RAW_HOSTS)

    # -- internals ---------------------------------------------------------
    def _api(self, path: str, limit: int = MAX_API_BYTES):
        url = "https://api.github.com" + path
        body = self._fetch(url, limit, API_HOSTS)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DataUpdateError(f"上游响应无法解析：{error}") from error

    def _fetch(self, url, limit, allowed):
        _validate_url(url, allowed)
        try:
            response = self._opener(url, timeout=self._timeout)
        except TypeError:
            response = self._opener(url)
        except OSError as error:
            raise DataUpdateError(f"无法访问上游：{error}") from error
        try:
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                final = geturl()
                if final:
                    _validate_url(final, allowed)
            chunks = []
            total = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise DataUpdateError("上游响应超过大小上限。")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# catalog validation (never trust the Catalog constructor alone)
# ---------------------------------------------------------------------------
def _validate_catalog_table(name: str, rows) -> None:
    if not isinstance(rows, list) or not rows:
        raise DataUpdateError(f"目录 {name} 必须是非空列表。")
    seen = set()
    required = TABLE_FIELDS[name]
    for row in rows:
        if not isinstance(row, dict):
            raise DataUpdateError(f"目录 {name} 含非对象行。")
        entity_id = row.get("id")
        if isinstance(entity_id, bool) or not isinstance(entity_id, int) or entity_id <= 0:
            raise DataUpdateError(f"目录 {name} 含非法 ID：{entity_id!r}")
        if entity_id in seen:
            raise DataUpdateError(f"目录 {name} 含重复 ID：{entity_id}")
        seen.add(entity_id)
        for field in required:
            if field not in row:
                raise DataUpdateError(f"目录 {name} 的行 {entity_id} 缺少字段 {field}")


def _validate_catalog_references(tables: dict) -> None:
    custom_ids = {row["id"] for row in tables["customizations"]}
    for row in tables["customizations"]:
        max_level = row.get("max")
        if isinstance(max_level, bool) or not isinstance(max_level, int) or max_level <= 0:
            raise DataUpdateError(f"自定义效果 {row['id']} 的 max 不合法。")
    for row in tables["skill_cards"]:
        if not isinstance(row.get("upgraded"), bool):
            raise DataUpdateError(f"卡片 {row['id']} 的 upgraded 必须是布尔值。")
        raw = row.get("availableCustomizations")
        if not isinstance(raw, str):
            raise DataUpdateError(f"卡片 {row['id']} 的 availableCustomizations 必须是字符串。")
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit() or int(part) not in custom_ids:
                raise DataUpdateError(f"卡片 {row['id']} 引用了未知自定义效果 {part!r}")
    for row in tables["stages"]:
        if not isinstance(row.get("type"), str):
            raise DataUpdateError(f"场地 {row['id']} 的 type 不合法。")
    for row in tables["p_items"]:
        if not isinstance(row.get("mode"), str):
            raise DataUpdateError(f"道具 {row['id']} 的 mode 不合法。")


# ---------------------------------------------------------------------------
# main updater
# ---------------------------------------------------------------------------
class DataUpdater:
    """Direct upstream updater for engine, catalog, icons and native mapping."""

    def __init__(self, user_home=app_paths.USER_ROOT, bundled_root=app_paths.RESOURCE_ROOT, progress=None):
        self.home = Path(user_home).resolve()
        self.bundled_root = Path(bundled_root).resolve()
        self._progress = progress
        self._client = GitHubClient()
        self._bytes_fetched = 0
        self._missing_icons = []

    # -- small logging -----------------------------------------------------
    def _log(self, message: str) -> None:
        if callable(self._progress):
            try:
                self._progress(str(message))
            except Exception:
                pass

    # -- paths -------------------------------------------------------------
    def _releases_root(self) -> Path:
        return self.home / "runtime" / "releases"

    def _pointer_path(self) -> Path:
        return self._releases_root() / ACTIVE_NAME

    def _cache_root(self) -> Path:
        return self.home / CACHE_DIRNAME

    def _release_dir(self, revision: str) -> Path:
        return self._releases_root() / revision

    # -- pointer -----------------------------------------------------------
    def _read_pointer(self):
        try:
            return resource_paths.read_pointer(self.home)
        except resource_paths.ResourceError as error:
            raise DataUpdateError(f"活动数据指针无法读取：{error}") from error

    def _write_pointer(self, history) -> None:
        if not history:
            history = [{"revision": None}]
        pointer = {"schema_version": SCHEMA_VERSION, "active": history[-1], "history": history}
        _atomic_write_json(self._pointer_path(), pointer)

    # -- status / check ----------------------------------------------------
    def _bundled_revision(self):
        path = self.bundled_root / "runtime" / "node" / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("revision")
        except (OSError, ValueError):
            return None

    def _running_revision(self):
        # The process-pinned runtime root never changes on disk activation.
        path = resource_paths.RUNTIME_ROOT / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("revision")
        except (OSError, ValueError):
            return None

    def _current_revision(self):
        pointer = self._read_pointer()
        if pointer:
            active = pointer.get("active") or {}
            value = active.get("revision")
            if _is_hex40(value):
                return value
        return self._bundled_revision()

    def status(self) -> dict:
        pointer = self._read_pointer()
        revision = None
        if pointer:
            active = pointer.get("active") or {}
            value = active.get("revision")
            if _is_hex40(value):
                revision = value
        source = "upstream" if revision else "bundled"
        if revision is None:
            revision = self._bundled_revision()
        return {"revision": revision, "source": source, "running_revision": self._running_revision()}

    def _latest_revisions(self):
        try:
            branch = self._client.default_branch(SOURCE_REPOSITORY)
            revision = self._client.commit_sha(SOURCE_REPOSITORY, branch)
            master_branch = self._client.default_branch(MASTER_REPOSITORY)
            master_revision = self._client.commit_sha(MASTER_REPOSITORY, master_branch)
        except DataUpdateError:
            raise
        except OSError as error:
            raise DataUpdateError(f"无法访问上游数据源：{error}") from error
        return revision, master_revision

    def check(self) -> dict:
        current = self._current_revision()
        revision, master_revision = self._latest_revisions()
        update_available = revision != current
        change_summary = {
            "current": current,
            "latest": revision,
            "master": master_revision,
            "update_available": update_available,
            "message": "发现新的上游数据" if update_available else "当前已是最新数据",
        }
        return {
            "revision": revision,
            "master_revision": master_revision,
            "current_revision": current,
            "update_available": update_available,
            "change_summary": change_summary,
        }

    # -- lock --------------------------------------------------------------
    @contextmanager
    def _exclusive_lock(self):
        root = self._releases_root()
        root.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(root / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_handle(handle)
            yield
        finally:
            try:
                _unlock_handle(handle)
            finally:
                os.close(handle)

    # -- install -----------------------------------------------------------
    def install(self, revision, master_revision) -> dict:
        rev = _require_revision(revision, "revision")
        mrev = _require_revision(master_revision, "master_revision")
        with self._exclusive_lock():
            return self._install_locked(rev, mrev)

    def _install_locked(self, revision: str, master_revision: str) -> dict:
        releases = self._releases_root()
        releases.mkdir(parents=True, exist_ok=True)
        target = releases / revision
        if target.exists():
            if not target.is_dir():
                raise DataUpdateError(f"目标路径已存在且不是目录：{target}")
            return self._install_existing(target, revision, master_revision)

        staging = Path(tempfile.mkdtemp(prefix=".stage-" + revision + "-", dir=str(releases)))
        try:
            counts, summary = self._populate(staging, revision, master_revision)
            self._verify_release(staging, revision, check_name=False)
            os.replace(str(staging), str(target))
            staging = None
        finally:
            if staging is not None:
                shutil.rmtree(str(staging), ignore_errors=True)
        self._log(f"已暂存 {revision}，正在激活")
        return self._activate(target, revision, counts, summary)

    def _install_existing(self, target: Path, revision: str, master_revision: str) -> dict:
        record = _read_json(target / "release.json")
        validation = record.get("validation") or {}
        if record.get("revision") != revision:
            raise DataUpdateError("已存在同目录但 revision 不一致的数据版本。")
        if validation.get("source_master_revision") != master_revision:
            raise DataUpdateError("已存在同引擎版本但来源修订不一致的数据包，拒绝覆盖。")
        self._verify_release(target, revision)
        provenance = (_read_json(target / "assets" / "native" / "mapping.json").get("provenance") or {})
        if provenance.get("master_revision") != master_revision:
            raise DataUpdateError("既有版本的映射来源修订与请求不一致，拒绝复用。")
        if provenance.get("catalog_revision") != revision:
            raise DataUpdateError("既有版本的映射 catalog_revision 与请求不一致，拒绝复用。")
        counts = validation.get("counts") or {}
        summary = validation.get("mapping_summary") or {}
        self._log(f"{revision} 已存在且校验通过，保持激活")
        return self._activate(target, revision, counts, summary)

    def _activate(self, target: Path, revision: str, counts: dict, summary: dict) -> dict:
        verified = self._verify_release(target, revision)
        pointer = self._read_pointer()
        if pointer:
            history = [entry for entry in (pointer.get("history") or []) if isinstance(entry, dict)]
        else:
            history = []
        if not history:
            history = [{"revision": None}]
        entry = {
            "revision": revision,
            "release": f"{RELEASES_RELATIVE_POSIX}/{revision}",
            "release_manifest_sha256": verified["release_manifest_sha256"],
        }
        if history[-1].get("revision") == revision:
            history[-1] = entry
        else:
            history.append(entry)
        self._write_pointer(history)
        return {
            "revision": revision,
            "requires_restart": True,
            "counts": counts,
            "mapping_summary": summary,
            "source": "upstream",
        }

    # -- rollback ----------------------------------------------------------
    def rollback(self) -> dict:
        with self._exclusive_lock():
            pointer = self._read_pointer()
            if pointer is None:
                raise DataUpdateError("没有可回滚的数据版本。")
            history = [entry for entry in (pointer.get("history") or []) if isinstance(entry, dict)]
            if len(history) < 2:
                raise DataUpdateError("没有更早的数据版本可回滚。")
            previous = history[-2]
            revision = previous.get("revision")
            if revision:
                _require_revision(revision, "revision")
                self._verify_release(self._release_dir(revision), revision)
            new_history = history[:-1]
            self._write_pointer(new_history)
            if revision:
                return {"revision": revision, "requires_restart": True, "source": "upstream"}
            return {
                "revision": self._bundled_revision(),
                "requires_restart": True,
                "source": "bundled",
            }

    # -- staging -----------------------------------------------------------
    def _populate(self, stage: Path, revision: str, master_revision: str):
        self._bytes_fetched = 0
        self._missing_icons = []
        self._log(f"下载上游引擎数据 {revision}")
        index = self._fetch_sources(stage, revision)
        self._copy_trusted_runtime(stage)
        counts = self._stage_recognition(stage, revision, index)
        self._write_runtime_manifest(stage, revision)
        self._log("构建原生映射")
        info = self._build_native_mapping(stage, revision, master_revision)
        self._validate_native(stage, revision, master_revision, info)
        self._log("运行上游回归测试")
        upstream_tests = self._validate_upstream_tests(stage, revision)
        self._log("运行 worker/reference 冒烟对比")
        smoke = self._validate_smoke(stage, revision)
        validation = {
            "status": "passed",
            "upstream_tests": upstream_tests,
            "smoke": smoke,
            "source_master_revision": master_revision,
            "counts": counts,
            "mapping_summary": info.get("summary") or {},
            "icons": {"missing": list(self._missing_icons)},
        }
        self._write_release(stage, revision, validation)
        return counts, info.get("summary") or {}

    def _write_release(self, stage: Path, revision: str, validation: dict) -> None:
        files = {rel: _sha256_file(path) for rel, path in _walk_files(stage).items()}
        record = {
            "schema_version": SCHEMA_VERSION,
            "revision": revision,
            "validation": validation,
            "files": files,
        }
        _atomic_write_json(stage / "release.json", record)

    # -- sources -----------------------------------------------------------
    def _load_tree(self, repository: str, revision: str) -> list:
        try:
            entries = self._client.tree(repository, revision)
        except DataUpdateError:
            raise
        except OSError as error:
            raise DataUpdateError(f"无法读取上游目录树：{error}") from error
        if not isinstance(entries, list):
            raise DataUpdateError("上游目录树格式不正确。")
        if len(entries) > MAX_TREE_ENTRIES:
            raise DataUpdateError("上游目录树过大。")
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str):
                _validate_relative_path(path)
            result.append(entry)
        return result

    def _index_tree(self, entries) -> dict:
        index = {}
        for entry in entries:
            if entry.get("type") == "blob" and isinstance(entry.get("path"), str):
                index.setdefault(entry["path"], entry)
        return index

    def _select_runtime_files(self, index: dict) -> dict:
        selected = {}
        for path, entry in index.items():
            if path.startswith(RUNTIME_PREFIXES) or path in RUNTIME_EXTRA_FILES:
                _validate_relative_path(path)
                selected[path] = entry
        for path in RUNTIME_EXTRA_FILES:
            if path not in selected:
                raise DataUpdateError(f"上游缺少必需文件：{path}")
        for prefix in RUNTIME_PREFIXES:
            if not any(path.startswith(prefix) for path in selected):
                raise DataUpdateError(f"上游缺少目录：{prefix}")
        return selected

    def _fetch_sources(self, stage: Path, revision: str) -> dict:
        entries = self._load_tree(SOURCE_REPOSITORY, revision)
        index = self._index_tree(entries)
        selected = self._select_runtime_files(index)
        upstream = stage / "runtime" / "node" / "upstream"
        upstream.mkdir(parents=True, exist_ok=True)
        for path in sorted(selected):
            data = self._fetch_blob(SOURCE_REPOSITORY, revision, path, selected[path])
            destination = upstream.joinpath(*path.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return index

    def _bundled_upstream_bytes(self, path: str):
        candidate = self.bundled_root / "runtime" / "node" / "upstream" / Path(path)
        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                return None
        return None

    def _cached_blob_path(self, sha: str) -> Path:
        return self._cache_root() / "blobs" / sha

    def _fetch_blob(self, repository: str, revision: str, path: str, entry: dict) -> bytes:
        _validate_relative_path(path)
        sha = entry.get("sha")
        if not _is_hex40(sha):
            raise DataUpdateError(f"目录项缺少合法 blob 校验和：{path!r}")
        size = entry.get("size")
        if isinstance(size, int) and size > MAX_BLOB_BYTES:
            raise DataUpdateError(f"上游文件超过大小上限：{path!r}")
        cached = self._cached_blob_path(sha)
        if cached.is_file():
            try:
                data = cached.read_bytes()
            except OSError:
                data = b""
            if data and _git_blob_sha1(data) == sha:
                return data
            try:
                cached.unlink()
            except OSError:
                pass
        bundled = self._bundled_upstream_bytes(path)
        if bundled is not None and _git_blob_sha1(bundled) == sha:
            self._store_cache(sha, bundled)
            return bundled
        try:
            data = self._client.raw(repository, revision, path, limit=MAX_BLOB_BYTES)
        except DataUpdateError:
            raise
        except OSError as error:
            raise DataUpdateError(f"无法下载上游文件 {path!r}：{error}") from error
        if not isinstance(data, (bytes, bytearray)):
            raise DataUpdateError(f"上游文件 {path!r} 返回类型不正确。")
        data = bytes(data)
        if _git_blob_sha1(data) != sha:
            raise DataUpdateError(f"上游文件内容与 Git blob 校验和不符：{path!r}")
        if isinstance(size, int) and len(data) != size:
            raise DataUpdateError(f"上游文件大小与目录树不一致：{path!r}")
        self._bytes_fetched += len(data)
        if self._bytes_fetched > MAX_TOTAL_BYTES:
            raise DataUpdateError("更新数据超过总大小上限。")
        self._store_cache(sha, data)
        return data

    def _store_cache(self, sha: str, data: bytes) -> None:
        path = self._cached_blob_path(sha)
        if path.is_file():
            return
        try:
            _atomic_write_bytes(path, data)
        except OSError:
            pass

    def _copy_trusted_runtime(self, stage: Path) -> None:
        target_dir = stage / "runtime" / "node"
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in TRUSTED_RUNTIME_FILES:
            source = self.bundled_root / "runtime" / "node" / name
            if not source.is_file():
                raise DataUpdateError(f"内置运行时缺少受信文件：{name}")
            (target_dir / name).write_bytes(source.read_bytes())

    # -- catalog + icons ---------------------------------------------------
    def _stage_recognition(self, stage: Path, revision: str, index: dict) -> dict:
        recognition = stage / "assets" / "recognition"
        recognition.mkdir(parents=True, exist_ok=True)
        data_dir = stage / "runtime" / "node" / "upstream" / Path(CATALOG_SOURCE_DIR)
        tables = {}
        for name in CATALOG_TABLES:
            source = data_dir / (name + ".json")
            if not source.is_file():
                raise DataUpdateError(f"上游缺少目录文件：{name}.json")
            raw = source.read_bytes()
            try:
                rows = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise DataUpdateError(f"目录 {name} 无法解析：{error}") from error
            _validate_catalog_table(name, rows)
            tables[name] = rows
            (recognition / (name + ".json")).write_bytes(raw)
        _validate_catalog_references(tables)

        card_ids = [row["id"] for row in tables["skill_cards"]]
        item_ids = [row["id"] for row in tables["p_items"]]
        icon_entries = self._sync_icons(recognition, index, card_ids, item_ids, revision)

        files = {}
        for name in CATALOG_TABLES:
            path = recognition / (name + ".json")
            files[name + ".json"] = {
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
                "upstream_path": f"{CATALOG_SOURCE_DIR}/{name}.json",
            }
        for rel, upstream_path in icon_entries.items():
            path = recognition / rel
            record = {"sha256": _sha256_file(path), "size": path.stat().st_size}
            if upstream_path:
                record["upstream_path"] = upstream_path
            files[rel] = record
        manifest = {"schema_version": SCHEMA_VERSION, "revision": revision, "files": files}
        _atomic_write_json(recognition / "manifest.json", manifest)
        return self._counts(data_dir, tables)

    def _counts(self, data_dir: Path, tables: dict) -> dict:
        counts = {
            "cards": len(tables["skill_cards"]),
            "items": len(tables["p_items"]),
            "stages": len(tables["stages"]),
            "customizations": len(tables["customizations"]),
        }
        p_idols = data_dir / "p_idols.json"
        if p_idols.is_file():
            try:
                counts["idols"] = len(json.loads(p_idols.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                counts["idols"] = 0
        return counts

    def _read_bundled_recognition_manifest(self) -> dict:
        path = self.bundled_root / "assets" / "recognition" / "manifest.json"
        if not path.is_file():
            return {}
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        files = record.get("files") if isinstance(record, dict) else None
        if not isinstance(files, dict):
            return {}
        result = {}
        for name, entry in files.items():
            if isinstance(entry, dict) and isinstance(entry.get("upstream_path"), str):
                result[name] = entry["upstream_path"]
        return result

    def _icon_template(self, directory: str) -> str:
        """Prefer the bundled same-path template (when it carries ``<ID>``)."""
        for rel, upstream_path in self._read_bundled_recognition_manifest().items():
            if rel.startswith(directory + "/") and "<ID>" in upstream_path:
                return upstream_path
        for name, template in ICON_SPECS:
            if name == directory:
                return template
        raise DataUpdateError(f"未知图标目录：{directory}")

    def _plan_icons(self, recognition: Path, index: dict, directory: str, ids, revision: str) -> dict:
        bundled = self.bundled_root / "assets" / "recognition"
        upstream_paths = self._read_bundled_recognition_manifest()
        template = self._icon_template(directory)
        entries = {}
        (recognition / directory).mkdir(parents=True, exist_ok=True)
        for entity_id in ids:
            rel = f"{directory}/{entity_id}.png"
            target = recognition / rel
            chosen = None
            for candidate in _expand_template(template, entity_id):
                entry = index.get(candidate)
                if entry is not None and entry.get("type") == "blob":
                    chosen = (candidate, entry)
                    break
            if chosen is not None:
                path, entry = chosen
                data = None
                retained_bytes = bundled / rel
                if retained_bytes.is_file():
                    try:
                        candidate_bytes = retained_bytes.read_bytes()
                    except OSError:
                        candidate_bytes = None
                    if candidate_bytes is not None and _git_blob_sha1(candidate_bytes) == entry.get("sha"):
                        data = candidate_bytes
                if data is None:
                    data = self._fetch_blob(SOURCE_REPOSITORY, revision, path, entry)
                target.write_bytes(data)
                entries[rel] = path
                continue
            retained_bytes = bundled / rel
            if retained_bytes.is_file():
                target.write_bytes(retained_bytes.read_bytes())
                entries[rel] = upstream_paths.get(rel) or template
                continue
            self._missing_icons.append(rel)
        return entries

    def _sync_icons(self, recognition: Path, index: dict, card_ids, item_ids, revision: str) -> dict:
        entries = {}
        entries.update(self._plan_icons(recognition, index, "card-icons", card_ids, revision))
        entries.update(self._plan_icons(recognition, index, "pitem-icons", item_ids, revision))
        if self._missing_icons:
            self._log(f"缺少 {len(self._missing_icons)} 个图标，保留既有图标并给出警告")
        return entries

    # -- runtime manifest --------------------------------------------------
    def _write_runtime_manifest(self, stage: Path, revision: str) -> None:
        upstream = stage / "runtime" / "node" / "upstream"
        files = {rel: _sha256_file(path) for rel, path in _walk_files(upstream).items()}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "repository": f"https://github.com/{SOURCE_REPOSITORY}",
            "revision": revision,
            "files": files,
        }
        _atomic_write_json(stage / "runtime" / "node" / "manifest.json", manifest)

    # -- native mapping ----------------------------------------------------
    def _build_native_mapping(self, stage: Path, revision: str, master_revision: str) -> dict:
        entries = self._load_tree(MASTER_REPOSITORY, master_revision)
        index = self._index_tree(entries)
        # Multi-megabyte YAML must never end up inside the immutable release;
        # it is cached (content-addressed) under userhome/data-update-cache.
        source = self._cache_root() / "native-sources" / master_revision
        source.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for name in YAML_TABLES:
            path = name + ".yaml"
            entry = index.get(path)
            if entry is None:
                raise DataUpdateError(f"上游缺少 YAML 表：{path}")
            data = self._fetch_blob(MASTER_REPOSITORY, master_revision, path, entry)
            (source / path).write_bytes(data)
            hashes[name] = _sha256_bytes(data)
        cache = self._cache_root() / "native-json"
        output = stage / "assets" / "native"
        output.mkdir(parents=True, exist_ok=True)
        build_native_mapping(
            source,
            cache,
            output,
            catalog_root=stage / "assets" / "recognition",
            sim_data=stage / "runtime" / "node" / "upstream" / Path(CATALOG_SOURCE_DIR),
        )
        mapping = _read_json(output / "mapping.json")
        correspondence = _read_json(output / "full-correspondence.json")
        summary = _read_json(output / "mapping-summary.json")
        provenance = mapping.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        source_hashes = provenance.get("source_sha256")
        source_hashes = dict(source_hashes) if isinstance(source_hashes, dict) else {}
        source_hashes.update(hashes)
        provenance["master_repository"] = MASTER_REPOSITORY
        provenance["master_revision"] = master_revision
        provenance["source_sha256"] = source_hashes
        mapping["provenance"] = provenance
        _atomic_write_json(output / "mapping.json", mapping)
        return {"mapping": mapping, "correspondence": correspondence, "summary": summary}

    def _read_bundled_mapping(self) -> dict:
        path = self.bundled_root / "assets" / "native" / "mapping.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _bundled_catalog(self):
        root = self.bundled_root / "assets" / "recognition"
        if not (root / "manifest.json").is_file():
            return None
        try:
            return Catalog(root)
        except (OSError, ValueError):
            return None

    def _validate_native(self, stage: Path, revision: str, master_revision: str, info: dict) -> None:
        mapping = info.get("mapping")
        if not isinstance(mapping, dict):
            raise DataUpdateError("原生映射格式不正确。")
        provenance = mapping.get("provenance")
        if not isinstance(provenance, dict):
            raise DataUpdateError("原生映射缺少 provenance。")
        if provenance.get("catalog_revision") != revision:
            raise DataUpdateError("原生映射的 catalog_revision 与引擎修订不一致。")
        # Persist the pinned master revision so re-install / rollback can prove
        # the immutable set came from the exact requested native source.
        provenance["master_revision"] = master_revision
        mapping["provenance"] = provenance
        _atomic_write_json(stage / "assets" / "native" / "mapping.json", mapping)

        recognition = stage / "assets" / "recognition"
        try:
            new_catalog = Catalog(recognition)
        except (OSError, ValueError) as error:
            raise DataUpdateError(f"暂存目录不完整：{error}") from error
        sim_idols = self._read_id_set(
            stage / "runtime" / "node" / "upstream" / Path(CATALOG_SOURCE_DIR) / "p_idols.json"
        )
        self._check_mapping_references(mapping, info.get("correspondence"), new_catalog, sim_idols)
        self._compare_with_bundled(mapping, new_catalog)

    @staticmethod
    def _read_id_set(path: Path) -> set:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(rows, list):
            return set()
        return {row["id"] for row in rows if isinstance(row, dict) and "id" in row}

    def _check_mapping_references(self, mapping, correspondence, new_catalog, sim_idols) -> None:
        inventory = (correspondence or {}).get("inventory") if isinstance(correspondence, dict) else None
        inventory = inventory if isinstance(inventory, dict) else {}

        def known_ids(kind):
            return {row.get("native_id") for row in inventory.get(kind, []) if isinstance(row, dict)}

        cards = mapping.get("cards") or {}
        if not isinstance(cards, dict):
            raise DataUpdateError("mapping.cards 必须是对象。")
        known = known_ids("cards")
        for native_id, variants in cards.items():
            if known and native_id not in known:
                raise DataUpdateError(f"卡片映射引用不存在的原生 ID：{native_id}")
            if not isinstance(variants, dict):
                raise DataUpdateError(f"卡片 {native_id} 的映射必须是对象。")
            for key, sim_id in variants.items():
                if not isinstance(key, str) or not key.isdigit():
                    raise DataUpdateError(f"卡片 {native_id} 的升级级别不合法：{key!r}")
                if sim_id not in new_catalog.skill_cards:
                    raise DataUpdateError(f"卡片映射指向未知的模拟器 ID：{sim_id}")

        items = mapping.get("items") or {}
        if not isinstance(items, dict):
            raise DataUpdateError("mapping.items 必须是对象。")
        known = known_ids("items")
        for native_id, sim_id in items.items():
            if known and native_id not in known:
                raise DataUpdateError(f"道具映射引用不存在的原生 ID：{native_id}")
            if sim_id not in new_catalog.p_items:
                raise DataUpdateError(f"道具映射指向未知的模拟器 ID：{sim_id}")

        idols = mapping.get("idols") or {}
        if not isinstance(idols, dict):
            raise DataUpdateError("mapping.idols 必须是对象。")
        known = known_ids("idols")
        for native_id, sim_id in idols.items():
            if known and native_id not in known:
                raise DataUpdateError(f"偶像映射引用不存在的原生 ID：{native_id}")
            if sim_id not in sim_idols:
                raise DataUpdateError(f"偶像映射指向未知的模拟器 ID：{sim_id}")

        customs = mapping.get("customizations") or {}
        if not isinstance(customs, dict):
            raise DataUpdateError("mapping.customizations 必须是对象。")
        known = known_ids("customizations")
        for native_id, sim_id in customs.items():
            if known and native_id not in known:
                raise DataUpdateError(f"自定义映射引用不存在的原生 ID：{native_id}")
            if sim_id not in new_catalog.customizations:
                raise DataUpdateError(f"自定义映射指向未知的模拟器 ID：{sim_id}")

    def _compare_with_bundled(self, mapping, new_catalog) -> None:
        bundled = self._read_bundled_mapping()
        if not bundled:
            return
        bundled_catalog = self._bundled_catalog()
        for kind in ("cards", "items", "idols", "customizations"):
            old = bundled.get(kind) or {}
            new = mapping.get(kind) or {}
            if not isinstance(old, dict) or not old:
                continue
            if kind == "cards":
                for native_id, variants in old.items():
                    target = new.get(native_id)
                    if not isinstance(variants, dict) or not isinstance(target, dict):
                        raise DataUpdateError(f"上游变更导致已映射卡片 {native_id} 不再可用，拒绝丢弃已拥有的记忆映射。")
                    for upgrade, sim_id in variants.items():
                        if target.get(str(upgrade)) != sim_id:
                            raise DataUpdateError(f"卡片 {native_id} 升级 {upgrade} 的映射发生变化，拒绝静默改变。")
                continue
            if not isinstance(new, dict):
                raise DataUpdateError(f"原生 {kind} 映射格式不正确。")
            for native_id, sim_id in old.items():
                if new.get(native_id) != sim_id:
                    raise DataUpdateError(f"原生 {kind} 映射 {native_id} 丢失或改变，拒绝静默丢弃。")
            if kind == "customizations" and bundled_catalog is not None:
                for native_id, sim_id in old.items():
                    before = bundled_catalog.customizations.get(sim_id)
                    after = new_catalog.customizations.get(sim_id)
                    if after is None:
                        raise DataUpdateError(f"自定义效果 {sim_id} 已从目录移除。")
                    if before is not None and before.get("max") != after.get("max"):
                        raise DataUpdateError(f"自定义效果 {sim_id} 的等级上限发生变化。")

    # -- node validation ---------------------------------------------------
    def _node_binary(self) -> str:
        bundled = self.bundled_root / "runtime" / "node-bin" / "node.exe"
        if bundled.is_file():
            return str(bundled)
        fallback = shutil.which("node")
        if not fallback:
            raise DataUpdateError("未找到可用的 Node.js 运行时。")
        return fallback

    def _run_node(self, runtime: Path, script: Path, *, stdin=None, timeout=SMOKE_TIMEOUT):
        loader = runtime / "upstream" / "scripts" / "extensionless-loader.mjs"
        command = [
            self._node_binary(),
            "--no-warnings",
            "--loader",
            loader.as_uri(),
            str(script),
        ]
        try:
            return subprocess.run(
                command,
                cwd=str(runtime),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise DataUpdateError(f"Node 校验超时：{script.name}") from error
        except OSError as error:
            raise DataUpdateError(f"无法启动 Node 校验：{error}") from error

    def _validate_upstream_tests(self, stage: Path, revision: str) -> dict:
        runtime = stage / "runtime" / "node"
        runner = runtime / "upstream" / "tests" / "run.mjs"
        if not runner.is_file():
            raise DataUpdateError("暂存运行时缺少上游回归测试入口。")
        result = self._run_node(runtime, runner, timeout=UPSTREAM_TEST_TIMEOUT)
        if result.returncode != 0:
            detail = (result.stdout or "")[-500:] + (result.stderr or "")[-500:]
            raise DataUpdateError(f"上游回归测试失败（退出码 {result.returncode}）：{detail}")
        return {"status": "passed", "exit_code": 0}

    def _validate_smoke(self, stage: Path, revision: str) -> dict:
        runtime = stage / "runtime" / "node"
        loadout_path = self.bundled_root / "examples" / "simulation" / "original.loadout.json"
        loadout = Loadout.load(loadout_path)

        from gkshot.adapters.local_engine import LocalSimulator

        with LocalSimulator(node=self._node_binary(), runtime=runtime) as simulator:
            result = simulator.simulate(
                loadout, trials=SMOKE_TRIALS, seed=SMOKE_SEED, timeout=SMOKE_TIMEOUT
            )
        scores = list(result.get("scores") or [])
        if not scores:
            raise DataUpdateError("冒烟测试未返回分数。")
        if result.get("engine_revision") != revision or result.get("data_revision") != revision:
            raise DataUpdateError("冒烟测试返回的引擎/数据修订与目标不一致。")
        reference = self._reference_scores(runtime, loadout)
        if scores != reference:
            raise DataUpdateError("worker 与 reference 结果不一致，拒绝激活。")
        return {"status": "passed", "trials": len(scores)}

    def _reference_scores(self, runtime: Path, loadout) -> list:
        script = runtime / "reference.mjs"
        payload = json.dumps({"query": _query_from_loadout(loadout),
                              "trials": SMOKE_TRIALS, "seed": SMOKE_SEED})
        result = self._run_node(runtime, script, stdin=payload, timeout=SMOKE_TIMEOUT)
        if result.returncode != 0:
            raise DataUpdateError("reference 校验执行失败。")
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise DataUpdateError("reference 未返回结果。")
        try:
            parsed = json.loads(lines[-1])
        except ValueError as error:
            raise DataUpdateError("reference 结果无法解析。") from error
        scores = parsed.get("scores")
        if not isinstance(scores, list) or not scores:
            raise DataUpdateError("reference 分数格式不正确。")
        return scores

    # -- verification ------------------------------------------------------
    def _verify_release(self, directory, revision: str, *, check_name: bool = True) -> dict:
        directory = Path(directory)
        if check_name and directory.name != revision:
            raise DataUpdateError("数据版本目录名与 revision 不一致。")
        manifest_path = directory / "release.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise DataUpdateError(f"数据版本缺少 release.json：{directory}")
        raw = manifest_path.read_bytes()
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DataUpdateError(f"release.json 无法解析：{error}") from error
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise DataUpdateError("release.json schema_version 不受支持。")
        if record.get("revision") != revision:
            raise DataUpdateError("release.json 的 revision 与请求不一致。")
        validation = record.get("validation")
        if not isinstance(validation, dict) or validation.get("status") != "passed":
            raise DataUpdateError("release.json 未记录通过验证，拒绝使用。")
        listed = record.get("files")
        if not isinstance(listed, dict) or not listed:
            raise DataUpdateError("release.json 缺少文件清单。")
        present = _walk_files(directory)
        present.pop("release.json", None)
        if set(present) != set(listed):
            raise DataUpdateError("数据版本文件清单与磁盘内容不一致。")
        for rel, path in present.items():
            digest = listed.get(rel)
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise DataUpdateError(f"release.json 哈希格式不正确：{rel}")
            if _sha256_file(path) != digest:
                raise DataUpdateError(f"数据版本校验失败：{rel}")
        return {"revision": revision, "release_manifest_sha256": hashlib.sha256(raw).hexdigest()}
