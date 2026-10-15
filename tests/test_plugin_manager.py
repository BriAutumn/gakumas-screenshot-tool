"""Tests for gkshot.plugin_manager (fake game dir + fake DLL)."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gkshot import plugin_manager as pm_mod
from gkshot.plugin_manager import (
    GameRunningError,
    PluginError,
    PluginManager,
    detect_game_dirs,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_config(user_home: Path) -> bytes:
    text = "[GKMemoryExport]\r\nExportDirectory=%s\r\n" % (
        str(Path(os.path.abspath(str(user_home))) / "native-export")
    )
    return b"\xff\xfe" + text.encode("utf-16-le")


def _receipt_path(user_home: Path, game_root: Path) -> Path:
    key = _sha256(str(game_root).casefold().encode("utf-8"))
    return Path(os.path.abspath(str(user_home))) / "plugin-installations" / (key + ".json")


class PluginManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "gakumas.exe").write_bytes(b"fake game executable")
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.dll_v1 = b"PROXY-DLL-V1"
        self._write_bundle(self.dll_v1, "1.2.3")
        self.manager = PluginManager(self.bundle, self.home, running_check=lambda: False)
        self.game_real = Path(os.path.realpath(self.game))

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _write_bundle(self, dll_bytes: bytes, version: str) -> str:
        (self.bundle / "version.dll").write_bytes(dll_bytes)
        digest = _sha256(dll_bytes)
        (self.bundle / "plugin.json").write_text(
            json.dumps(
                {"schema_version": 1, "version": version, "sha256": digest}
            ),
            encoding="utf-8",
        )
        return digest

    def _write_receipt(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def _staged_leftovers(self) -> list:
        return [p.name for p in self.game.glob(".gkplugin-stage-*")]

    def _assert_clean_game_dir(self):
        self.assertEqual(self._staged_leftovers(), [])

    # -- first install / idempotency ---------------------------------------

    def test_first_install_writes_files_and_receipt(self):
        result = self.manager.install(self.game)
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"], "installed")
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["game_dir"], str(self.game_real))
        self.assertEqual(result["export_dir"], str(self.home / "native-export"))
        self.assertEqual((self.game / "version.dll").read_bytes(), self.dll_v1)

        config = (self.game / "GKMemoryExport.ini").read_bytes()
        self.assertEqual(config, _expected_config(self.home))
        self.assertTrue(config.startswith(b"\xff\xfe"))

        receipt_path = _receipt_path(self.home, self.game_real)
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(result["receipt"], str(receipt_path))
        record = self._write_receipt(receipt_path)
        self.assertEqual(record["dll_sha256"], _sha256(self.dll_v1))
        self.assertEqual(record["config_sha256"], _sha256(config))
        self.assertEqual(record["game_dir"], str(self.game_real))

        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "installed")
        self.assertFalse(status["changed"])
        self.assertEqual(status["conflicts"], [])
        self.assertFalse(status["running"])
        self._assert_clean_game_dir()

    def test_repeated_install_is_idempotent(self):
        self.manager.install(self.game)
        second = self.manager.install(self.game)
        self.assertFalse(second["changed"])
        self.assertEqual(second["state"], "installed")
        self.assertEqual((self.game / "version.dll").read_bytes(), self.dll_v1)
        self._assert_clean_game_dir()

    def test_status_not_installed_on_fresh_dir(self):
        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "not_installed")
        self.assertFalse(status["dll_present"])
        self.assertFalse(status["config_present"])
        self.assertFalse(status["changed"])

    # -- upgrade + uninstall ------------------------------------------------

    def test_valid_owned_upgrade_then_uninstall(self):
        self.manager.install(self.game)

        dll_v2 = b"PROXY-DLL-V2-LONGER"
        digest_v2 = self._write_bundle(dll_v2, "2.0.0")
        upgraded = self.manager.install(self.game)
        self.assertTrue(upgraded["changed"])
        self.assertEqual(upgraded["version"], "2.0.0")
        self.assertEqual((self.game / "version.dll").read_bytes(), dll_v2)
        receipt_path = _receipt_path(self.home, self.game_real)
        self.assertEqual(self._write_receipt(receipt_path)["dll_sha256"], digest_v2)

        result = self.manager.uninstall(self.game)
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"], "not_installed")
        self.assertFalse((self.game / "version.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(receipt_path.exists())
        self._assert_clean_game_dir()

    def test_prototype_hash_can_be_replaced(self):
        prototype = b"OLD-PROTOTYPE-PROXY"
        (self.game / "version.dll").write_bytes(prototype)
        with mock.patch.object(
            pm_mod, "KNOWN_PROTOTYPE_SHA256", _sha256(prototype)
        ):
            result = self.manager.install(self.game)
        self.assertTrue(result["changed"])
        self.assertEqual((self.game / "version.dll").read_bytes(), self.dll_v1)

    # -- conflict preservation ---------------------------------------------

    def test_conflicting_dll_is_preserved(self):
        foreign = b"SOMEONE-ELSES-VERSION-DLL"
        (self.game / "version.dll").write_bytes(foreign)
        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(_receipt_path(self.home, self.game_real).exists())
        self._assert_clean_game_dir()

    def test_conflicting_config_is_preserved(self):
        foreign = "[Other]\r\nValue=1\r\n".encode("utf-8")
        (self.game / "GKMemoryExport.ini").write_bytes(foreign)
        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        self.assertEqual((self.game / "GKMemoryExport.ini").read_bytes(), foreign)
        self.assertFalse((self.game / "version.dll").exists())
        self._assert_clean_game_dir()

    def test_uninstall_preserves_modified_dll(self):
        self.manager.install(self.game)
        modified = b"USER-MODIFIED-DLL"
        (self.game / "version.dll").write_bytes(modified)

        result = self.manager.uninstall(self.game)
        self.assertIn("version.dll", result["conflicts"])
        self.assertEqual(result["state"], "conflict")
        self.assertTrue(result["changed"])
        self.assertEqual((self.game / "version.dll").read_bytes(), modified)
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self._assert_clean_game_dir()

    def test_uninstall_preserves_modified_config(self):
        self.manager.install(self.game)
        modified = b"\xff\xfe" + "edited".encode("utf-16-le")
        (self.game / "GKMemoryExport.ini").write_bytes(modified)

        result = self.manager.uninstall(self.game)
        self.assertIn("GKMemoryExport.ini", result["conflicts"])
        self.assertEqual((self.game / "GKMemoryExport.ini").read_bytes(), modified)
        self.assertFalse((self.game / "version.dll").exists())
        self._assert_clean_game_dir()

    # -- running game refusal ----------------------------------------------

    def test_running_game_refuses_mutation(self):
        manager = PluginManager(self.bundle, self.home, running_check=lambda: True)
        with self.assertRaises(GameRunningError):
            manager.install(self.game)
        with self.assertRaises(GameRunningError):
            manager.uninstall(self.game)
        self.assertFalse((self.game / "version.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(_receipt_path(self.home, self.game_real).exists())

    # -- invalid bundle -----------------------------------------------------

    def test_invalid_bundle_hash_is_rejected(self):
        (self.bundle / "plugin.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "1.2.3",
                    "sha256": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        with self.assertRaises(PluginError):
            self.manager.status(self.game)
        self.assertFalse((self.game / "version.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())

    # -- game dir validation ------------------------------------------------

    def test_game_dir_without_exe_is_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(PluginError) as ctx:
            self.manager.status(empty)
        self.assertIn("gakumas.exe", str(ctx.exception))

    def test_missing_game_dir_is_rejected(self):
        with self.assertRaises(PluginError):
            self.manager.status(self.root / "nope")

    # -- mid-install failure restores original state ------------------------

    def test_mid_install_failure_restores_original_state(self):
        self.manager.install(self.game)
        original_dll = (self.game / "version.dll").read_bytes()
        original_cfg = (self.game / "GKMemoryExport.ini").read_bytes()
        receipt_path = _receipt_path(self.home, self.game_real)
        original_receipt = self._write_receipt(receipt_path)

        dll_v2 = b"PROXY-DLL-V2"
        self._write_bundle(dll_v2, "2.0.0")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected mid-install failure")
            return real_replace(src, dst)

        with mock.patch("gkshot.plugin_manager.os.replace", side_effect=flaky_replace):
            with self.assertRaises(RuntimeError):
                self.manager.install(self.game)

        self.assertEqual((self.game / "version.dll").read_bytes(), original_dll)
        self.assertEqual((self.game / "GKMemoryExport.ini").read_bytes(), original_cfg)
        self.assertEqual(self._write_receipt(receipt_path), original_receipt)
        self._assert_clean_game_dir()

    # -- symlink escapes ----------------------------------------------------

    def test_receipt_failure_restores_owned_plugin_and_receipt(self):
        self.manager.install(self.game)
        before = {name: (self.game / name).read_bytes() for name in ('version.dll', 'GKMemoryExport.ini')}
        receipt = _receipt_path(self.home, self.game_real)
        original_receipt = receipt.read_bytes()
        self._write_bundle(b'PROXY-DLL-V2', '2.0.0')
        with mock.patch.object(self.manager, '_write_receipt', side_effect=PermissionError('receipt denied')):
            with self.assertRaises(PermissionError): self.manager.install(self.game)
        for name, content in before.items(): self.assertEqual((self.game / name).read_bytes(), content)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self._assert_clean_game_dir()

    def test_failed_process_query_blocks_plugin_changes(self):
        with mock.patch.object(pm_mod.subprocess, 'run', side_effect=OSError('query denied')):
            with self.assertRaises(PluginError): pm_mod._default_running_check()

    def test_native_process_query_ignores_console_encoding(self):
        for raw, expected in [('信息: 没有匹配任务'.encode('gbk'), False),
                              (b'"gakumas.exe","1234","Console","1","2048 K"\r\n', True)]:
            with self.subTest(output=raw), mock.patch.object(pm_mod.subprocess, 'run',
                    return_value=mock.Mock(returncode=0, stdout=raw)):
                self.assertEqual(pm_mod._default_running_check(), expected)

    def test_escaping_directory_symlink_is_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "gakumas.exe").write_bytes(b"fake")
        link = self.root / "game-link"
        try:
            os.symlink(outside.name, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this platform")
        with self.assertRaises(PluginError):
            self.manager.status(link)

    def test_escaping_file_symlink_is_rejected(self):
        outside = self.root / "outside.dll"
        outside.write_bytes(b"not ours")
        dll_link = self.game / "version.dll"
        try:
            os.symlink(outside, dll_link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported on this platform")
        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        self.assertEqual(outside.read_bytes(), b"not ours")

    # -- detection ----------------------------------------------------------

    def test_detect_game_dirs_uses_settings(self):
        (self.home / "settings.json").write_text(
            json.dumps({"game_dir": str(self.game)}), encoding="utf-8"
        )
        with mock.patch.object(pm_mod, "_dmm_install_locations", return_value=[]), \
                mock.patch.object(pm_mod, "_running_game_dir", return_value=None):
            found = detect_game_dirs(self.home)
        self.assertIn(self.game_real, found)

    def test_detect_game_dirs_merges_registry_and_dedupes(self):
        (self.home / "settings.json").write_text(
            json.dumps({"game_dir": str(self.game)}), encoding="utf-8"
        )
        registry_dir = self.root / "dmm"
        registry_dir.mkdir()
        with mock.patch.object(
            pm_mod, "_dmm_install_locations", return_value=[str(registry_dir), str(self.game)]
        ), mock.patch.object(pm_mod, "_running_game_dir", return_value=None):
            found = detect_game_dirs(self.home)
        expected_registry = Path(os.path.realpath(registry_dir))
        self.assertIn(self.game_real, found)
        self.assertIn(expected_registry, found)
        # Deduplicated: the settings dir and registry dir appear once.
        self.assertEqual(found.count(self.game_real), 1)

    def test_detect_game_dirs_running_game(self):
        with mock.patch.object(pm_mod, "_dmm_install_locations", return_value=[]), \
                mock.patch.object(
                    pm_mod, "_running_game_dir", return_value=str(self.game)
                ):
            found = detect_game_dirs(self.home)
        self.assertIn(self.game_real, found)

    def test_detect_game_dirs_ignores_missing_paths(self):
        (self.home / "settings.json").write_text(
            json.dumps({"game_dir": str(self.root / "gone")}), encoding="utf-8"
        )
        with mock.patch.object(pm_mod, "_dmm_install_locations", return_value=[]), \
                mock.patch.object(pm_mod, "_running_game_dir", return_value=None):
            found = detect_game_dirs(self.home)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
