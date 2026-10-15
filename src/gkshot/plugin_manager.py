"""Installation manager for the native memory-export plugin.

The plugin ships a proxy DLL that the game loads, plus a small
``GKMemoryExport.ini`` file telling it where to write snapshots.  Two backend
DLL names are supported:

* ``version.dll`` -- the legacy/default proxy name;
* ``winhttp.dll`` -- an independent alternate backend used for coexistence
  when a third-party ``version.dll`` (for example Gakumas Localify) already
  occupies the default slot.

This module owns *only* files it installed: the selected proxy DLL, the
config file it generated, and a per-game installation receipt under
``user_home/plugin-installations``.  Foreign DLLs are never overwritten or
backed up: when the default ``version.dll`` slot is taken by another tool the
alternate ``winhttp.dll`` backend is installed instead, and if both slots are
occupied the operation is refused without touching anything.

Safety rules implemented here:

* every path is resolved and a ``game_dir`` must be a real directory that
  contains ``gakumas.exe``; symlinks that escape that directory are
  rejected;
* while ``gakumas.exe`` is running (checked via ``tasklist`` with
  ``CREATE_NO_WINDOW``) *all* mutation is refused;
* a foreign (not-owned) ``version.dll``/``winhttp.dll`` is preserved
  byte-identically; ownership is scoped to a specific file name so a hash
  recorded for one backend never makes the other backend look owned;
* an existing ``GKMemoryExport.ini`` may only be replaced when it is
  byte-identical to the config we generate or was recorded by our receipt;
* previous owned files are backed up under ``user_home`` and the new files
  are staged next to their targets and swapped in atomically; both the DLL
  and the config are restored if any step fails.

The module never elevates privileges: a ``PermissionError`` from the OS
bubbles up to the caller unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "PluginManager",
    "PluginError",
    "GameRunningError",
    "detect_game_dirs",
    "KNOWN_PROTOTYPE_SHA256",
]

VERSION_DLL_NAME = "version.dll"
WINHTTP_DLL_NAME = "winhttp.dll"
# Legacy alias: the default proxy name is still ``version.dll``.
DLL_NAME = VERSION_DLL_NAME
KNOWN_DLL_NAMES = (VERSION_DLL_NAME, WINHTTP_DLL_NAME)
GAME_EXE = "gakumas.exe"
MANIFEST_NAME = "plugin.json"
CONFIG_NAME = "GKMemoryExport.ini"
CONFIG_SECTION = "GKMemoryExport"
EXPORT_DIR_NAME = "native-export"
RECEIPTS_DIR_NAME = "plugin-installations"
BACKUPS_DIR_NAME = "backups"
PLUGIN_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1

# Hash of the previously shipped prototype proxy.  It is treated as owned so
# that an upgrade can replace it, but it is never mistaken for a foreign DLL.
# It only ever applies to the legacy ``version.dll`` name.
KNOWN_PROTOTYPE_SHA256 = (
    "4c7cd711ef3ed0f92e4d1def1be54bd1e19076eaf92d600f3cfa52111ded70af"
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_STAGE_PREFIX = ".gkplugin-stage-"
_HEX_DIGITS = set("0123456789abcdef")


class PluginError(RuntimeError):
    """A plugin operation was refused or is impossible."""

    def __init__(self, message: str, *, state: str | None = None) -> None:
        super().__init__(message)
        self.state = state


class GameRunningError(PluginError):
    """The game is running, so no file may be touched."""

    def __init__(
        self, message: str = "游戏正在运行，已阻止对游戏目录的任何修改。"
    ) -> None:
        super().__init__(message, state="running")


@dataclass(frozen=True)
class Bundle:
    """A validated bundled plugin.

    ``sha256`` is always the ``version.dll`` hash declared at the top level of
    ``plugin.json`` (schema 1).  ``winhttp_sha256`` is only set when the
    manifest carries an optional ``files`` map that includes a verified
    ``winhttp.dll`` alternate backend.
    """

    version: str
    sha256: str
    winhttp_sha256: str | None = None

    @property
    def has_alternate(self) -> bool:
        return self.winhttp_sha256 is not None

    def expected_sha(self, name: str) -> str | None:
        return self.winhttp_sha256 if name == WINHTTP_DLL_NAME else self.sha256


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _is_link(path: Path) -> bool:
    try:
        if Path(path).is_symlink():
            return True
    except OSError:
        return False
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None:
        try:
            return bool(is_junction(path))
        except OSError:
            return False
    return False


def _realpath(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        Path(path).relative_to(Path(parent))
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_like_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX_DIGITS for ch in value)
    )


class PluginManager:
    """Manage the proxy DLL + config for a single game installation."""

    def __init__(self, bundle_dir, user_home, running_check=None) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.user_home = Path(os.path.abspath(str(user_home)))
        self._running_check = running_check

    # -- public API ---------------------------------------------------------

    @property
    def export_dir(self) -> Path:
        """Absolute directory the plugin should export snapshots into."""
        return self.user_home / EXPORT_DIR_NAME

    def status(self, game_dir) -> dict:
        game_root = self._resolve_game_dir(game_dir)
        bundle = self._load_bundle()

        version_path = game_root / VERSION_DLL_NAME
        winhttp_path = game_root / WINHTTP_DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(version_path, game_root)
        self._assert_managed_file(winhttp_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        version_present = version_path.is_file()
        version_sha = _sha256_file(version_path) if version_present else None
        winhttp_present = winhttp_path.is_file()
        winhttp_sha = _sha256_file(winhttp_path) if winhttp_present else None
        cfg_present = cfg_path.is_file()
        cfg_sha = _sha256_file(cfg_path) if cfg_present else None
        expected_cfg_sha = _sha256_bytes(self._config_bytes())

        version_owned = self._dll_owned(
            VERSION_DLL_NAME, version_sha, bundle, receipt
        )
        winhttp_owned = self._dll_owned(
            WINHTTP_DLL_NAME, winhttp_sha, bundle, receipt
        )
        cfg_owned = cfg_present and self._cfg_is_owned(
            cfg_sha, expected_cfg_sha, receipt
        )

        analysis = self._analyze_selection(
            bundle, version_sha, version_owned, winhttp_sha, winhttp_owned
        )
        selected = analysis["selected"]
        conflicts = list(analysis["conflicts"])

        if analysis["blocked"]:
            state = "conflict"
        elif not version_present and not winhttp_present and not cfg_present:
            state = "not_installed"
        elif (
            version_present
            and version_sha == KNOWN_PROTOTYPE_SHA256
            and not cfg_present
        ):
            state = "prototype"
        else:
            expected = bundle.expected_sha(selected)
            present = (
                version_present
                if selected == VERSION_DLL_NAME
                else winhttp_present
            )
            sha = version_sha if selected == VERSION_DLL_NAME else winhttp_sha
            owned = self._dll_owned(selected, sha, bundle, receipt)
            if present and sha == expected and cfg_sha == expected_cfg_sha:
                state = "installed"
            elif present and owned and (not cfg_present or cfg_owned):
                state = "update_available"
            elif not present:
                state = "not_installed"
            else:
                state = "conflict"
                if selected not in conflicts:
                    conflicts.append(selected)

        if cfg_present and not cfg_owned:
            state = "conflict"
            if CONFIG_NAME not in conflicts:
                conflicts.append(CONFIG_NAME)
        result = self._result(
            bundle, game_root, changed=False, state=state, dll_name=selected
        )
        result.update(
            {
                "running": self._is_running(),
                "dll_present": version_present,
                "dll_sha256": version_sha,
                "winhttp_present": winhttp_present,
                "winhttp_sha256": winhttp_sha,
                "config_present": cfg_present,
                "config_sha256": cfg_sha,
                "receipt": str(receipt_path) if receipt is not None else None,
                "conflicts": conflicts,
            }
        )
        return result

    def install(self, game_dir) -> dict:
        game_root = self._resolve_game_dir(game_dir)
        bundle = self._load_bundle()
        if self._is_running():
            raise GameRunningError()

        version_path = game_root / VERSION_DLL_NAME
        winhttp_path = game_root / WINHTTP_DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(version_path, game_root)
        self._assert_managed_file(winhttp_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        expected_cfg = self._config_bytes()
        expected_cfg_sha = _sha256_bytes(expected_cfg)

        version_bytes = version_path.read_bytes() if version_path.is_file() else None
        winhttp_bytes = winhttp_path.read_bytes() if winhttp_path.is_file() else None
        cfg_bytes = cfg_path.read_bytes() if cfg_path.is_file() else None
        version_sha = (
            _sha256_bytes(version_bytes) if version_bytes is not None else None
        )
        winhttp_sha = (
            _sha256_bytes(winhttp_bytes) if winhttp_bytes is not None else None
        )
        cfg_sha = _sha256_bytes(cfg_bytes) if cfg_bytes is not None else None

        version_owned = self._dll_owned(
            VERSION_DLL_NAME, version_sha, bundle, receipt
        )
        winhttp_owned = self._dll_owned(
            WINHTTP_DLL_NAME, winhttp_sha, bundle, receipt
        )

        analysis = self._analyze_selection(
            bundle, version_sha, version_owned, winhttp_sha, winhttp_owned
        )
        if analysis["blocked"]:
            self._raise_conflict(analysis, game_root)
        target_name = analysis["selected"]
        target_expected_sha = bundle.expected_sha(target_name)
        if target_expected_sha is None:
            raise PluginError(
                "插件包缺少所选后端 %s 的校验哈希" % target_name, state="conflict"
            )

        target_path = game_root / target_name
        target_bytes = (
            version_bytes if target_name == VERSION_DLL_NAME else winhttp_bytes
        )
        target_sha = version_sha if target_name == VERSION_DLL_NAME else winhttp_sha

        if cfg_bytes is not None and not self._cfg_is_owned(
            cfg_sha, expected_cfg_sha, receipt
        ):
            raise PluginError(
                "游戏目录中已存在非本工具生成的 GKMemoryExport.ini，已拒绝覆盖：%s"
                % cfg_path,
                state="conflict",
            )

        already = (
            target_sha == target_expected_sha
            and cfg_sha == expected_cfg_sha
            and self._receipt_matches(
                receipt, target_name, target_expected_sha, expected_cfg_sha
            )
        )
        if already:
            result = self._result(
                bundle, game_root, changed=False, state="installed",
                dll_name=target_name,
            )
            result.update(
                {"dll_sha256": target_expected_sha, "receipt": str(receipt_path)}
            )
            return result

        # Back up any owned files we are about to replace.  Foreign DLLs and
        # foreign configs are never part of this set.
        backup_items = []
        if target_bytes is not None:
            backup_items.append((target_path, target_bytes))
        if cfg_bytes is not None:
            backup_items.append((cfg_path, cfg_bytes))
        self._backup_owned(game_root, backup_items)

        staged = []
        try:
            staged_dll = self._stage_file(
                game_root, (self.bundle_dir / target_name).read_bytes()
            )
            staged.append(staged_dll)
            staged_cfg = self._stage_file(game_root, expected_cfg)
            staged.append(staged_cfg)
            os.replace(staged_dll, target_path)
            os.replace(staged_cfg, cfg_path)
            self._write_receipt(receipt_path, {
                'schema_version': RECEIPT_SCHEMA_VERSION,
                'game_dir': str(game_root), 'version': bundle.version,
                'dll_name': target_name, 'dll_sha256': target_expected_sha,
                'config_sha256': expected_cfg_sha,
                'export_dir': str(self.export_dir), 'installed_at': _now_iso(),
            })
        except BaseException:
            for path in staged:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._restore_file(target_path, target_bytes)
            self._restore_file(cfg_path, cfg_bytes)
            raise

        result = self._result(
            bundle, game_root, changed=True, state="installed",
            dll_name=target_name,
        )
        result.update(
            {
                "dll_sha256": target_expected_sha,
                "receipt": str(receipt_path),
            }
        )
        return result

    def uninstall(self, game_dir) -> dict:
        game_root = self._resolve_game_dir(game_dir)
        bundle = self._load_bundle()
        if self._is_running():
            raise GameRunningError()

        version_path = game_root / VERSION_DLL_NAME
        winhttp_path = game_root / WINHTTP_DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(version_path, game_root)
        self._assert_managed_file(winhttp_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        expected_cfg_sha = _sha256_bytes(self._config_bytes())

        version_present = version_path.is_file()
        version_sha = _sha256_file(version_path) if version_present else None
        winhttp_present = winhttp_path.is_file()
        winhttp_sha = _sha256_file(winhttp_path) if winhttp_present else None
        cfg_present = cfg_path.is_file()
        cfg_sha = _sha256_file(cfg_path) if cfg_present else None

        version_owned = self._dll_owned(
            VERSION_DLL_NAME, version_sha, bundle, receipt
        )
        winhttp_owned = self._dll_owned(
            WINHTTP_DLL_NAME, winhttp_sha, bundle, receipt
        )

        # Only the backend we installed is considered; a third-party DLL in the
        # other slot is intentionally preserved and never marked as a conflict.
        primary = self._uninstall_primary(
            receipt, version_sha, version_owned, winhttp_sha, winhttp_owned
        )
        primary_path = game_root / primary
        primary_present = (
            version_present if primary == VERSION_DLL_NAME else winhttp_present
        )
        primary_sha = version_sha if primary == VERSION_DLL_NAME else winhttp_sha
        primary_owned = self._dll_owned(primary, primary_sha, bundle, receipt)
        remove_dll = primary_present and primary_owned
        remove_cfg = cfg_present and self._cfg_is_owned(
            cfg_sha, expected_cfg_sha, receipt
        )

        conflicts = []
        if primary_present and not remove_dll:
            conflicts.append(primary)
        if cfg_present and not remove_cfg:
            conflicts.append(CONFIG_NAME)

        moved = []
        try:
            if remove_dll:
                backup = self._unique_adjacent(game_root)
                os.replace(primary_path, backup)
                moved.append((backup, primary_path))
            if remove_cfg:
                backup = self._unique_adjacent(game_root)
                os.replace(cfg_path, backup)
                moved.append((backup, cfg_path))
        except BaseException:
            for backup, original in reversed(moved):
                os.replace(backup, original)
            raise

        for backup, _original in moved:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass

        # Keep the receipt whenever our DLL was not removed: it is the recovery
        # evidence for any owned leftovers (for example a replaced backend).
        if remove_dll and receipt_path.is_file():
            receipt_path.unlink()

        removed = []
        if remove_dll:
            removed.append(primary)
        if remove_cfg:
            removed.append(CONFIG_NAME)

        state = "conflict" if conflicts else "not_installed"
        result = self._result(
            bundle, game_root, changed=bool(moved), state=state, dll_name=primary
        )
        result.update(
            {
                "dll_present": version_path.is_file(),
                "winhttp_present": winhttp_path.is_file(),
                "config_present": cfg_path.is_file(),
                "conflicts": conflicts,
                "removed": removed,
            }
        )
        return result

    # -- validation ---------------------------------------------------------

    def _load_bundle(self) -> Bundle:
        manifest_path = self.bundle_dir / MANIFEST_NAME
        version_path = self.bundle_dir / VERSION_DLL_NAME
        if not manifest_path.is_file():
            raise PluginError("插件包不完整：缺少 %s" % manifest_path)
        if not version_path.is_file():
            raise PluginError("插件包不完整：缺少 %s" % version_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise PluginError("插件包清单无法解析：%s：%s" % (manifest_path, error))
        if not isinstance(manifest, dict):
            raise PluginError("插件包清单格式不正确：%s" % manifest_path)
        if manifest.get("schema_version") != PLUGIN_SCHEMA_VERSION:
            raise PluginError(
                "插件包清单 schema_version 不受支持：%r"
                % manifest.get("schema_version")
            )
        version = manifest.get("version")
        if not isinstance(version, str) or not version.strip():
            raise PluginError("插件包清单缺少有效的 version 字段：%s" % manifest_path)
        declared = manifest.get("sha256")
        if not _looks_like_sha256(declared):
            raise PluginError("插件包清单缺少有效的 sha256 字段：%s" % manifest_path)
        actual = _sha256_file(version_path)
        if actual != declared:
            raise PluginError(
                "插件包 version.dll 校验失败：文件哈希与清单不一致（%s）" % version_path
            )

        winhttp_sha = None
        files = manifest.get("files")
        if files is not None:
            if not isinstance(files, dict):
                raise PluginError("插件包清单 files 字段格式不正确：%s" % manifest_path)
            for name, value in files.items():
                if name not in KNOWN_DLL_NAMES:
                    raise PluginError(
                        "插件包清单 files 含未知文件：%r" % (name,)
                    )
                if not _looks_like_sha256(value):
                    raise PluginError(
                        "插件包清单 files[%s] 缺少有效 sha256：%s"
                        % (name, manifest_path)
                    )
                if name == VERSION_DLL_NAME:
                    if value != declared:
                        raise PluginError(
                            "插件包清单 files[version.dll] 与顶层 sha256 不一致：%s"
                            % manifest_path
                        )
                    continue
                winhttp_path = self.bundle_dir / WINHTTP_DLL_NAME
                if not winhttp_path.is_file():
                    raise PluginError(
                        "插件包不完整：清单声明了 winhttp.dll 但文件缺失：%s"
                        % winhttp_path
                    )
                if _sha256_file(winhttp_path) != value:
                    raise PluginError(
                        "插件包 winhttp.dll 校验失败：文件哈希与清单不一致（%s）"
                        % winhttp_path
                    )
                winhttp_sha = value
        return Bundle(version=version, sha256=actual, winhttp_sha256=winhttp_sha)

    def _resolve_game_dir(self, game_dir) -> Path:
        raw = Path(game_dir)
        if not raw.exists():
            raise PluginError("游戏目录不存在：%s" % raw)
        if _is_link(raw):
            parent_real = _realpath(raw.parent)
            target_real = _realpath(raw)
            if not _is_within(target_real, parent_real):
                raise PluginError(
                    "游戏目录符号链接指向目录之外，已拒绝：%s" % raw
                )
        game_root = _realpath(raw)
        if not game_root.is_dir():
            raise PluginError("游戏目录不是有效目录：%s" % game_root)

        exe_path = game_root / GAME_EXE
        if _is_link(exe_path) and not _is_within(_realpath(exe_path), game_root):
            raise PluginError(
                "gakumas.exe 符号链接指向游戏目录之外，已拒绝：%s" % exe_path
            )
        if not exe_path.is_file():
            raise PluginError(
                "游戏目录中未找到 gakumas.exe，无法继续：%s" % game_root
            )
        return game_root

    def _assert_managed_file(self, path: Path, game_root: Path) -> None:
        if _is_link(path) and not _is_within(_realpath(path), game_root):
            raise PluginError(
                "受管文件的符号链接指向游戏目录之外，已拒绝：%s" % path
            )

    # -- backend selection --------------------------------------------------

    def _analyze_selection(
        self, bundle: Bundle, version_sha, version_owned, winhttp_sha, winhttp_owned
    ) -> dict:
        """Decide which backend to use without mutating anything.

        Returns ``{"selected": name|None, "blocked": bool, "conflicts": [...]}``
        where ``conflicts`` lists the file names that prevent coexistence.
        """
        version_present = version_sha is not None
        winhttp_present = winhttp_sha is not None
        if version_present and not version_owned:
            # A third-party version.dll owns the default slot.
            if not bundle.has_alternate:
                return {
                    "selected": None,
                    "blocked": True,
                    "conflicts": [VERSION_DLL_NAME],
                }
            if winhttp_present and not winhttp_owned:
                return {
                    "selected": None,
                    "blocked": True,
                    "conflicts": [VERSION_DLL_NAME, WINHTTP_DLL_NAME],
                }
            return {
                "selected": WINHTTP_DLL_NAME,
                "blocked": False,
                "conflicts": [],
            }
        if version_present:
            # Our own version.dll backend (legacy default) is already in place.
            return {
                "selected": VERSION_DLL_NAME,
                "blocked": False,
                "conflicts": [],
            }
        # version.dll slot is free: prefer an already-owned winhttp backend so a
        # working coexistence install is not migrated back unnecessarily.
        if winhttp_owned and bundle.has_alternate:
            return {
                "selected": WINHTTP_DLL_NAME,
                "blocked": False,
                "conflicts": [],
            }
        return {"selected": VERSION_DLL_NAME, "blocked": False, "conflicts": []}

    def _raise_conflict(self, analysis: dict, game_root: Path) -> None:
        conflicts = analysis["conflicts"]
        if WINHTTP_DLL_NAME in conflicts:
            message = (
                "游戏目录中的 version.dll 与 winhttp.dll 均已被其他插件占用，"
                "为避免覆盖已中止安装：%s" % game_root
            )
        else:
            message = (
                "游戏目录中已存在非本工具安装的 version.dll，且插件包未提供 "
                "winhttp.dll 备用后端，已拒绝覆盖：%s" % (game_root / VERSION_DLL_NAME)
            )
        raise PluginError(message, state="conflict")

    def _uninstall_primary(
        self, receipt, version_sha, version_owned, winhttp_sha, winhttp_owned
    ) -> str:
        """Pick the single DLL name uninstall should consider removing.

        The receipt's file name wins when the recorded hash still matches the
        file on disk (even if that file is no longer owned, it must not be
        silently dropped).  Otherwise fall back to whichever backend is
        actually owned.
        """
        rec = self._receipt_dll(receipt)
        if rec is not None:
            current = version_sha if rec[0] == VERSION_DLL_NAME else winhttp_sha
            if rec[1] == current:
                return rec[0]
        if version_owned:
            return VERSION_DLL_NAME
        if winhttp_owned:
            return WINHTTP_DLL_NAME
        return rec[0] if rec is not None else VERSION_DLL_NAME

    # -- ownership ----------------------------------------------------------

    @staticmethod
    def _receipt_dll(receipt):
        """Return ``(dll_name, sha256)`` recorded by a receipt, or ``None``.

        Legacy receipts (no ``dll_name``) always describe ``version.dll``; the
        recorded hash is therefore scoped to the legacy name only.
        """
        if not receipt:
            return None
        sha = receipt.get("dll_sha256")
        if not _looks_like_sha256(sha):
            return None
        name = receipt.get("dll_name")
        if name is None:
            name = VERSION_DLL_NAME
        if name not in KNOWN_DLL_NAMES:
            return None
        return (name, sha)

    def _dll_owned(self, name, sha256, bundle, receipt) -> bool:
        if sha256 is None or name not in KNOWN_DLL_NAMES:
            return False
        if name == VERSION_DLL_NAME:
            if sha256 == KNOWN_PROTOTYPE_SHA256:
                return True
            if bundle is not None and sha256 == bundle.sha256:
                return True
        elif bundle is not None and bundle.winhttp_sha256 is not None:
            if sha256 == bundle.winhttp_sha256:
                return True
        rec = self._receipt_dll(receipt)
        return bool(rec is not None and rec == (name, sha256))

    @staticmethod
    def _cfg_is_owned(sha256, expected_sha256: str, receipt) -> bool:
        if sha256 is None:
            return False
        if sha256 == expected_sha256:
            return True
        return bool(receipt and receipt.get("config_sha256") == sha256)

    def _receipt_matches(self, receipt, name, dll_sha, cfg_sha) -> bool:
        rec = self._receipt_dll(receipt)
        if rec is None or rec != (name, dll_sha):
            return False
        return receipt.get("config_sha256") == cfg_sha

    # -- config -------------------------------------------------------------

    def _config_bytes(self) -> bytes:
        text = "[%s]\r\nExportDirectory=%s\r\n" % (
            CONFIG_SECTION,
            str(self.export_dir),
        )
        # UTF-16LE with an explicit BOM, as the native plugin expects.
        return b"\xff\xfe" + text.encode("utf-16-le")

    # -- receipts -----------------------------------------------------------

    def _receipt_key(self, game_root: Path) -> str:
        material = str(game_root).casefold().encode("utf-8")
        return _sha256_bytes(material)

    def _receipt_path(self, game_root: Path) -> Path:
        return (
            self.user_home
            / RECEIPTS_DIR_NAME
            / (self._receipt_key(game_root) + ".json")
        )

    @staticmethod
    def _read_receipt(path: Path):
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_receipt(path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            record, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        fd, name = tempfile.mkstemp(
            prefix=".receipt-", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            os.replace(name, path)
        except BaseException:
            try:
                Path(name).unlink()
            except FileNotFoundError:
                pass
            raise

    # -- backups / staging --------------------------------------------------

    def _backup_owned(self, game_root: Path, items) -> Path | None:
        if not items:
            return None
        root = (
            self.user_home
            / RECEIPTS_DIR_NAME
            / BACKUPS_DIR_NAME
            / self._receipt_key(game_root)
        )
        root.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix="backup-", dir=str(root)))
        for path, data in items:
            (backup_dir / path.name).write_bytes(data)
        return backup_dir

    def _stage_file(self, game_root: Path, data: bytes) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=_STAGE_PREFIX, suffix=".tmp", dir=str(game_root)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                Path(name).unlink()
            except FileNotFoundError:
                pass
            raise
        return Path(name)

    def _unique_adjacent(self, game_root: Path) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=_STAGE_PREFIX, suffix=".bak", dir=str(game_root)
        )
        os.close(fd)
        Path(name).unlink()
        return Path(name)

    def _restore_file(self, path: Path, data: bytes | None) -> None:
        if data is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        staged = self._stage_file(path.parent, data)
        os.replace(staged, path)

    # -- process check ------------------------------------------------------

    def _is_running(self) -> bool:
        if self._running_check is not None:
            return bool(self._running_check())
        return _default_running_check()

    # -- misc ---------------------------------------------------------------

    def _result(self, bundle, game_root, *, changed, state, dll_name=None):
        return {
            "version": bundle.version,
            "state": state,
            "game_dir": str(game_root),
            "export_dir": str(self.export_dir),
            "changed": changed,
            "dll_name": dll_name,
            "mode": (
                "winhttp"
                if dll_name == WINHTTP_DLL_NAME
                else ("version" if dll_name else None)
            ),
        }


# ---------------------------------------------------------------------------
# running-process detection
# ---------------------------------------------------------------------------


def _default_running_check() -> bool:
    """Return ``True`` when ``gakumas.exe`` appears in ``tasklist`` output."""
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % GAME_EXE, "/FO", "CSV", "/NH"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PluginError('无法确认游戏是否正在运行，暂不能更改插件：' + str(error)) from error
    if completed.returncode:
        raise PluginError('进程查询失败，暂不能更改插件')
    # The native utility uses the Windows code page, independently of Python's
    # UTF-8 mode. The only required field is the ASCII executable name.
    output = completed.stdout.decode('ascii', errors='replace')
    for row in csv.reader(output.splitlines()):
        if row and row[0].strip().casefold() == GAME_EXE:
            return True
    return False


# ---------------------------------------------------------------------------
# game directory discovery
# ---------------------------------------------------------------------------


def _has_game_exe(path: Path) -> bool:
    try:
        return (Path(path) / GAME_EXE).is_file()
    except OSError:
        return False


def _settings_game_dir(user_home) -> str | None:
    settings = Path(user_home) / "settings.json"
    if not settings.is_file():
        return None
    try:
        data = json.loads(settings.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("game_dir", "gameDir", "game_directory", "gakumas_dir"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _reg_value(key, name: str):
    try:
        import winreg

        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value


def _dmm_install_locations() -> list:
    """Common DMM uninstall ``InstallLocation`` values (read-only)."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows guard
        return []

    locations = []
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, uninstall),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node" + "\\" + uninstall),
        (winreg.HKEY_CURRENT_USER, uninstall),
    )
    keywords = ("gakumas", "ガクマス", "学園アイドル", "dmm")
    for hive, subkey in roots:
        try:
            key = winreg.OpenKey(hive, subkey)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    child = winreg.OpenKey(key, name)
                except OSError:
                    continue
                try:
                    display = _reg_value(child, "DisplayName")
                    publisher = _reg_value(child, "Publisher")
                    location = _reg_value(child, "InstallLocation")
                finally:
                    winreg.CloseKey(child)
                haystack = ("%s %s" % (display or "", publisher or "")).casefold()
                if not any(word in haystack for word in keywords):
                    continue
                if isinstance(location, str) and location.strip():
                    locations.append(location)
        finally:
            winreg.CloseKey(key)
    return locations


def _running_game_dir() -> str | None:
    """Best-effort read-only lookup of a running game's directory."""
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                "Get-Process -Name gakumas -ErrorAction SilentlyContinue "
                "| Select-Object -First 1 -ExpandProperty Path",
            ],
            capture_output=True,
            text=True,
            encoding='utf-8',
            creationflags=CREATE_NO_WINDOW,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (completed.stdout or "").splitlines():
        candidate = line.strip().strip('"')
        if not candidate:
            continue
        exe = Path(candidate)
        if exe.parent.is_dir():
            return str(exe.parent)
    return None


def detect_game_dirs(user_home) -> list:
    """Return likely game directories without scanning whole drives.

    Sources, in priority order: the saved ``settings.json`` ``game_dir``,
    DMM uninstall registry ``InstallLocation`` values, and the directory of a
    running ``gakumas.exe`` discovered through a read-only PowerShell query.
    """
    seen = set()
    ordered = []

    def add(candidate) -> None:
        if not candidate:
            return
        try:
            path = Path(candidate)
            if not path.is_dir():
                return
            real = _realpath(path)
        except (OSError, ValueError):
            return
        key = str(real).casefold()
        if key in seen:
            return
        seen.add(key)
        ordered.append(real)

    add(_settings_game_dir(user_home))
    for location in _dmm_install_locations():
        add(location)
    add(_running_game_dir())

    with_exe = [path for path in ordered if _has_game_exe(path)]
    without_exe = [path for path in ordered if not _has_game_exe(path)]
    return with_exe + without_exe
