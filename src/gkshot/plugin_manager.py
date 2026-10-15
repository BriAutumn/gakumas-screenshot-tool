"""Installation manager for the native memory-export plugin.

The plugin is a proxy ``version.dll`` that the game loads, plus a small
``GKMemoryExport.ini`` file telling it where to write snapshots.  This
module owns *only* files it installed: the bundled proxy DLL, the config
file it generated, and a per-game installation receipt under
``user_home/plugin-installations``.

Safety rules implemented here:

* every path is resolved and a ``game_dir`` must be a real directory that
  contains ``gakumas.exe``; symlinks that escape that directory are
  rejected;
* while ``gakumas.exe`` is running (checked via ``tasklist`` with
  ``CREATE_NO_WINDOW``) *all* mutation is refused;
* an existing ``version.dll`` may only be replaced when it is exactly the
  bundled hash, a hash recorded by one of *our* receipts, or the known
  prototype hash;
* an existing ``GKMemoryExport.ini`` may only be replaced when it is
  byte-identical to the config we generate or was recorded by our receipt;
* previous owned files are backed up under ``user_home`` and the new files
  are staged next to their targets and swapped in atomically; both files
  are restored if any step fails.

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

DLL_NAME = "version.dll"
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
    """A validated bundled plugin."""

    version: str
    sha256: str


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

        dll_path = game_root / DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(dll_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        dll_present = dll_path.is_file()
        dll_sha = _sha256_file(dll_path) if dll_present else None
        cfg_present = cfg_path.is_file()
        cfg_sha = _sha256_file(cfg_path) if cfg_present else None
        expected_cfg_sha = _sha256_bytes(self._config_bytes())

        dll_owned = dll_present and self._dll_is_owned(dll_sha, bundle, receipt)
        cfg_owned = cfg_present and self._cfg_is_owned(
            cfg_sha, expected_cfg_sha, receipt
        )
        conflicts = []
        if dll_present and not dll_owned:
            conflicts.append(DLL_NAME)
        if cfg_present and not cfg_owned:
            conflicts.append(CONFIG_NAME)

        cfg_ok = (not cfg_present) or cfg_owned
        if not dll_present and not cfg_present:
            state = "not_installed"
        elif dll_present and dll_sha == bundle.sha256 and cfg_sha == expected_cfg_sha:
            state = "installed"
        elif (
            dll_present
            and dll_sha == KNOWN_PROTOTYPE_SHA256
            and not cfg_present
        ):
            state = "prototype"
        elif dll_present and dll_owned and cfg_ok:
            state = "update_available"
        else:
            state = "conflict"

        result = self._result(bundle, game_root, changed=False, state=state)
        result.update(
            {
                "running": self._is_running(),
                "dll_present": dll_present,
                "dll_sha256": dll_sha,
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

        dll_path = game_root / DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(dll_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        expected_cfg = self._config_bytes()
        expected_cfg_sha = _sha256_bytes(expected_cfg)

        original_dll = dll_path.read_bytes() if dll_path.is_file() else None
        original_cfg = cfg_path.read_bytes() if cfg_path.is_file() else None
        existing_dll_sha = (
            _sha256_bytes(original_dll) if original_dll is not None else None
        )
        existing_cfg_sha = (
            _sha256_bytes(original_cfg) if original_cfg is not None else None
        )

        dll_owned = existing_dll_sha is None or self._dll_is_owned(
            existing_dll_sha, bundle, receipt
        )
        cfg_owned = existing_cfg_sha is None or self._cfg_is_owned(
            existing_cfg_sha, expected_cfg_sha, receipt
        )
        if not dll_owned:
            raise PluginError(
                "游戏目录中已存在非本工具安装的 version.dll，已拒绝覆盖：%s"
                % dll_path,
                state="conflict",
            )
        if not cfg_owned:
            raise PluginError(
                "游戏目录中已存在非本工具生成的 GKMemoryExport.ini，已拒绝覆盖：%s"
                % cfg_path,
                state="conflict",
            )

        already = (
            existing_dll_sha == bundle.sha256
            and existing_cfg_sha == expected_cfg_sha
            and receipt is not None
            and receipt.get("dll_sha256") == bundle.sha256
            and receipt.get("config_sha256") == expected_cfg_sha
        )
        if already:
            return self._result(bundle, game_root, changed=False, state="installed")

        # Back up any owned files we are about to replace.
        backup_items = []
        if original_dll is not None:
            backup_items.append((dll_path, original_dll))
        if original_cfg is not None:
            backup_items.append((cfg_path, original_cfg))
        self._backup_owned(game_root, backup_items)

        staged = []
        try:
            staged_dll = self._stage_file(
                game_root, (self.bundle_dir / DLL_NAME).read_bytes()
            )
            staged.append(staged_dll)
            staged_cfg = self._stage_file(game_root, expected_cfg)
            staged.append(staged_cfg)
            os.replace(staged_dll, dll_path)
            os.replace(staged_cfg, cfg_path)
            self._write_receipt(receipt_path, {
                'schema_version': RECEIPT_SCHEMA_VERSION,
                'game_dir': str(game_root), 'version': bundle.version,
                'dll_sha256': bundle.sha256, 'config_sha256': expected_cfg_sha,
                'export_dir': str(self.export_dir), 'installed_at': _now_iso(),
            })
        except BaseException:
            for path in staged:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            self._restore_file(dll_path, original_dll)
            self._restore_file(cfg_path, original_cfg)
            raise

        result = self._result(bundle, game_root, changed=True, state="installed")
        result.update(
            {
                "dll_sha256": bundle.sha256,
                "receipt": str(receipt_path),
            }
        )
        return result

    def uninstall(self, game_dir) -> dict:
        game_root = self._resolve_game_dir(game_dir)
        bundle = self._load_bundle()
        if self._is_running():
            raise GameRunningError()

        dll_path = game_root / DLL_NAME
        cfg_path = game_root / CONFIG_NAME
        self._assert_managed_file(dll_path, game_root)
        self._assert_managed_file(cfg_path, game_root)

        receipt_path = self._receipt_path(game_root)
        receipt = self._read_receipt(receipt_path)

        expected_cfg_sha = _sha256_bytes(self._config_bytes())
        owned_dll_hashes = {bundle.sha256, KNOWN_PROTOTYPE_SHA256}
        if receipt and _looks_like_sha256(receipt.get("dll_sha256")):
            owned_dll_hashes.add(receipt["dll_sha256"])
        owned_cfg_hashes = {expected_cfg_sha}
        if receipt and _looks_like_sha256(receipt.get("config_sha256")):
            owned_cfg_hashes.add(receipt["config_sha256"])

        dll_present = dll_path.is_file()
        dll_sha = _sha256_file(dll_path) if dll_present else None
        cfg_present = cfg_path.is_file()
        cfg_sha = _sha256_file(cfg_path) if cfg_present else None

        remove_dll = dll_present and dll_sha in owned_dll_hashes
        remove_cfg = cfg_present and cfg_sha in owned_cfg_hashes

        conflicts = []
        if dll_present and not remove_dll:
            conflicts.append(DLL_NAME)
        if cfg_present and not remove_cfg:
            conflicts.append(CONFIG_NAME)

        moved = []
        try:
            if remove_dll:
                backup = self._unique_adjacent(game_root)
                os.replace(dll_path, backup)
                moved.append((backup, dll_path))
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

        if remove_dll and receipt_path.is_file():
            receipt_path.unlink()

        removed = []
        if remove_dll:
            removed.append(DLL_NAME)
        if remove_cfg:
            removed.append(CONFIG_NAME)

        state = "conflict" if conflicts else "not_installed"
        result = self._result(bundle, game_root, changed=bool(moved), state=state)
        result.update(
            {
                "dll_present": dll_path.is_file(),
                "config_present": cfg_path.is_file(),
                "conflicts": conflicts,
                "removed": removed,
            }
        )
        return result

    # -- validation ---------------------------------------------------------

    def _load_bundle(self) -> Bundle:
        manifest_path = self.bundle_dir / MANIFEST_NAME
        dll_path = self.bundle_dir / DLL_NAME
        if not manifest_path.is_file():
            raise PluginError("插件包不完整：缺少 %s" % manifest_path)
        if not dll_path.is_file():
            raise PluginError("插件包不完整：缺少 %s" % dll_path)
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
        actual = _sha256_file(dll_path)
        if actual != declared:
            raise PluginError(
                "插件包 version.dll 校验失败：文件哈希与清单不一致（%s）" % dll_path
            )
        return Bundle(version=version, sha256=actual)

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

    # -- ownership ----------------------------------------------------------

    @staticmethod
    def _dll_is_owned(sha256, bundle: Bundle, receipt) -> bool:
        if sha256 is None:
            return False
        if sha256 == bundle.sha256 or sha256 == KNOWN_PROTOTYPE_SHA256:
            return True
        return bool(receipt and receipt.get("dll_sha256") == sha256)

    @staticmethod
    def _cfg_is_owned(sha256, expected_sha256: str, receipt) -> bool:
        if sha256 is None:
            return False
        if sha256 == expected_sha256:
            return True
        return bool(receipt and receipt.get("config_sha256") == sha256)

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

    def _result(self, bundle, game_root, *, changed, state):
        return {
            "version": bundle.version,
            "state": state,
            "game_dir": str(game_root),
            "export_dir": str(self.export_dir),
            "changed": changed,
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
