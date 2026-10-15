"""Coexistence tests for gkshot.plugin_manager (winhttp alternate backend).

These tests exercise the transparent ``winhttp.dll`` backend used when a
third-party ``version.dll`` (e.g. Gakumas Localify) already occupies the
default slot.  They intentionally use synthetic bundles with an optional
``files`` map so no native binary is required.
"""

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
from gkshot.plugin_manager import PluginError, PluginManager


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expected_config(user_home: Path) -> bytes:
    text = "[GKMemoryExport]\r\nExportDirectory=%s\r\n" % (
        str(Path(os.path.abspath(str(user_home))) / "native-export")
    )
    return b"\xff\xfe" + text.encode("utf-16-le")


def _receipt_path(user_home: Path, game_root: Path) -> Path:
    key = _sha256(str(game_root).casefold().encode("utf-8"))
    return (
        Path(os.path.abspath(str(user_home)))
        / "plugin-installations"
        / (key + ".json")
    )


class CoexistenceTests(unittest.TestCase):
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
        self.dll_v1 = b"PROXY-VERSION-DLL-V1"
        self.http_v1 = b"PROXY-WINHTTP-DLL-V1"
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")
        self.manager = PluginManager(self.bundle, self.home, running_check=lambda: False)
        self.game_real = Path(os.path.realpath(self.game))

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _write_bundle(self, version_bytes, winhttp_bytes, version_str, with_alternate=True):
        (self.bundle / "version.dll").write_bytes(version_bytes)
        manifest = {
            "schema_version": 1,
            "version": version_str,
            "sha256": _sha256(version_bytes),
        }
        if with_alternate:
            (self.bundle / "winhttp.dll").write_bytes(winhttp_bytes)
            manifest["files"] = {
                "version.dll": _sha256(version_bytes),
                "winhttp.dll": _sha256(winhttp_bytes),
            }
        (self.bundle / "plugin.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return _sha256(version_bytes), (
            _sha256(winhttp_bytes) if with_alternate else None
        )

    def _foreign_version(self, data=b"LOCALIFY-VERSION-DLL"):
        (self.game / "version.dll").write_bytes(data)
        return data

    def _foreign_winhttp(self, data=b"OTHER-WINHTTP-DLL"):
        (self.game / "winhttp.dll").write_bytes(data)
        return data

    def _read_receipt(self):
        path = _receipt_path(self.home, self.game_real)
        return path, json.loads(path.read_text(encoding="utf-8-sig"))

    def _staged_leftovers(self):
        return [p.name for p in self.game.glob(".gkplugin-stage-*")]

    # -- compatible install / status ---------------------------------------

    def test_compatible_install_uses_winhttp_and_keeps_version(self):
        foreign = self._foreign_version()
        _version_sha, winhttp_sha = self._write_bundle(
            self.dll_v1, self.http_v1, "1.0.0"
        )
        result = self.manager.install(self.game)

        self.assertTrue(result["changed"])
        self.assertEqual(result["state"], "installed")
        self.assertEqual(result["dll_name"], "winhttp.dll")
        self.assertEqual(result["mode"], "winhttp")
        self.assertEqual((self.game / "winhttp.dll").read_bytes(), self.http_v1)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual(
            (self.game / "GKMemoryExport.ini").read_bytes(),
            _expected_config(self.home),
        )

        _path, record = self._read_receipt()
        self.assertEqual(record["dll_name"], "winhttp.dll")
        self.assertEqual(record["dll_sha256"], winhttp_sha)
        self.assertEqual(self._staged_leftovers(), [])

        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "installed")
        self.assertEqual(status["mode"], "winhttp")
        self.assertEqual(status["dll_name"], "winhttp.dll")
        self.assertEqual(status["conflicts"], [])
        self.assertTrue(status["winhttp_present"])
        self.assertTrue(status["dll_present"])  # third-party version.dll kept

    def test_compatible_status_before_install_is_not_conflict(self):
        self._foreign_version()
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")

        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "not_installed")
        self.assertEqual(status["dll_name"], "winhttp.dll")
        self.assertEqual(status["mode"], "winhttp")
        self.assertEqual(status["conflicts"], [])
        self.assertFalse(status["winhttp_present"])

    def test_foreign_version_without_alternate_is_refused(self):
        foreign = self._foreign_version()
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0", with_alternate=False)

        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertFalse((self.game / "winhttp.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(_receipt_path(self.home, self.game_real).exists())
        self.assertEqual(self._staged_leftovers(), [])

    # -- reinstall / update -------------------------------------------------

    def test_compatible_reinstall_is_idempotent(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)
        second = self.manager.install(self.game)

        self.assertFalse(second["changed"])
        self.assertEqual(second["state"], "installed")
        self.assertEqual(second["dll_name"], "winhttp.dll")
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual(self._staged_leftovers(), [])

    def test_compatible_update_replaces_only_own_winhttp(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)

        http_v2 = b"PROXY-WINHTTP-DLL-V2-LONGER"
        self._write_bundle(self.dll_v1, http_v2, "2.0.0")
        upgraded = self.manager.install(self.game)

        self.assertTrue(upgraded["changed"])
        self.assertEqual(upgraded["version"], "2.0.0")
        self.assertEqual((self.game / "winhttp.dll").read_bytes(), http_v2)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        _path, record = self._read_receipt()
        self.assertEqual(record["dll_name"], "winhttp.dll")
        self.assertEqual(record["dll_sha256"], _sha256(http_v2))
        self.assertEqual(self._staged_leftovers(), [])

    # -- uninstall ----------------------------------------------------------

    def test_compatible_uninstall_removes_winhttp_keeps_version(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)
        receipt = _receipt_path(self.home, self.game_real)

        result = self.manager.uninstall(self.game)
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"], "not_installed")
        self.assertEqual(result["conflicts"], [])
        self.assertIn("winhttp.dll", result["removed"])
        self.assertFalse((self.game / "winhttp.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertFalse(receipt.exists())
        self.assertEqual(self._staged_leftovers(), [])

    def test_modified_alternative_is_preserved_and_receipt_kept(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)
        receipt = _receipt_path(self.home, self.game_real)

        modified = b"OTHER-PLUGIN-REPLACED-WINHTTP"
        (self.game / "winhttp.dll").write_bytes(modified)

        result = self.manager.uninstall(self.game)
        self.assertEqual(result["state"], "conflict")
        self.assertIn("winhttp.dll", result["conflicts"])
        self.assertNotIn("version.dll", result["conflicts"])
        self.assertEqual((self.game / "winhttp.dll").read_bytes(), modified)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        # Recovery evidence for any owned leftovers is retained.
        self.assertTrue(receipt.exists())
        self.assertEqual(self._staged_leftovers(), [])

    # -- occupied alternate slot -------------------------------------------

    def test_both_slots_occupied_is_conflict_no_mutation(self):
        foreign_v = self._foreign_version()
        foreign_w = self._foreign_winhttp()
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")

        with self.assertRaises(PluginError):
            self.manager.install(self.game)

        self.assertEqual((self.game / "version.dll").read_bytes(), foreign_v)
        self.assertEqual((self.game / "winhttp.dll").read_bytes(), foreign_w)
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(_receipt_path(self.home, self.game_real).exists())
        self.assertEqual(self._staged_leftovers(), [])

        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "conflict")
        self.assertIn("version.dll", status["conflicts"])
        self.assertIn("winhttp.dll", status["conflicts"])

    # -- filename-scoped ownership -----------------------------------------

    def test_version_dll_with_winhttp_hash_is_not_owned(self):
        # The foreign version.dll happens to carry the same bytes as our
        # winhttp backend; ownership is scoped per file name, so it must not be
        # mistaken for an owned version.dll.
        foreign = self._foreign_version(self.http_v1)
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")

        result = self.manager.install(self.game)
        self.assertEqual(result["dll_name"], "winhttp.dll")
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual((self.game / "winhttp.dll").read_bytes(), self.http_v1)

    def test_winhttp_receipt_does_not_own_version_dll(self):
        other = b"OUR-WINHTTP-ONLY"
        shared = _sha256(other)
        receipt = _receipt_path(self.home, self.game_real)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "game_dir": str(self.game_real),
                    "version": "1.0.0",
                    "dll_name": "winhttp.dll",
                    "dll_sha256": shared,
                    "config_sha256": "0" * 64,
                    "export_dir": str(self.home / "native-export"),
                }
            ),
            encoding="utf-8",
        )
        (self.game / "version.dll").write_bytes(other)
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")

        status = self.manager.status(self.game)
        # version.dll must not be classified as our winhttp backend.
        self.assertNotEqual(status["mode"], "version")
        self.assertEqual(status["state"], "not_installed")
        self.assertEqual(status["conflicts"], [])

        result = self.manager.install(self.game)
        self.assertEqual(result["dll_name"], "winhttp.dll")
        self.assertEqual((self.game / "version.dll").read_bytes(), other)

    # -- legacy receipts ----------------------------------------------------

    def test_legacy_receipt_without_dll_name_still_owned(self):
        self.manager.install(self.game)
        receipt = _receipt_path(self.home, self.game_real)
        record = json.loads(receipt.read_text(encoding="utf-8-sig"))
        record.pop("dll_name")  # simulate a receipt written by an older build
        receipt.write_text(json.dumps(record), encoding="utf-8")

        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "installed")
        self.assertEqual(status["mode"], "version")

        result = self.manager.uninstall(self.game)
        self.assertTrue(result["changed"])
        self.assertFalse((self.game / "version.dll").exists())
        self.assertFalse((self.game / "GKMemoryExport.ini").exists())
        self.assertFalse(receipt.exists())
        self.assertEqual(self._staged_leftovers(), [])

    # -- transactional failure ---------------------------------------------

    def test_compatible_atomic_failure_restores_both_files(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)
        original_http = (self.game / "winhttp.dll").read_bytes()
        original_cfg = (self.game / "GKMemoryExport.ini").read_bytes()
        receipt = _receipt_path(self.home, self.game_real)
        original_receipt = receipt.read_bytes()

        self._write_bundle(self.dll_v1, b"PROXY-WINHTTP-DLL-V2", "2.0.0")

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

        self.assertEqual((self.game / "winhttp.dll").read_bytes(), original_http)
        self.assertEqual((self.game / "GKMemoryExport.ini").read_bytes(), original_cfg)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(self._staged_leftovers(), [])

    def test_compatible_receipt_failure_restores_both_files(self):
        foreign = self._foreign_version()
        self.manager.install(self.game)
        before = {
            name: (self.game / name).read_bytes()
            for name in ("winhttp.dll", "GKMemoryExport.ini", "version.dll")
        }
        receipt = _receipt_path(self.home, self.game_real)
        original_receipt = receipt.read_bytes()

        self._write_bundle(self.dll_v1, b"PROXY-WINHTTP-DLL-V2", "2.0.0")
        with mock.patch.object(
            self.manager, "_write_receipt", side_effect=PermissionError("receipt denied")
        ):
            with self.assertRaises(PermissionError):
                self.manager.install(self.game)

        for name, content in before.items():
            self.assertEqual((self.game / name).read_bytes(), content)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(self._staged_leftovers(), [])

    # -- bundle validation --------------------------------------------------

    def test_foreign_config_is_reported_and_preserved(self):
        foreign = self._foreign_version()
        config = b"foreign export configuration"
        (self.game / "GKMemoryExport.ini").write_bytes(config)
        status = self.manager.status(self.game)
        self.assertEqual(status["state"], "conflict")
        self.assertIn("GKMemoryExport.ini", status["conflicts"])
        with self.assertRaises(PluginError):
            self.manager.install(self.game)
        self.assertEqual((self.game / "version.dll").read_bytes(), foreign)
        self.assertEqual((self.game / "GKMemoryExport.ini").read_bytes(), config)
        self.assertFalse((self.game / "winhttp.dll").exists())

    def test_manifest_files_hash_mismatch_is_rejected(self):
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0")
        (self.bundle / "winhttp.dll").write_bytes(b"TAMPERED-WINHTTP")
        with self.assertRaises(PluginError):
            self.manager.status(self.game)

    def test_legacy_single_dll_bundle_has_no_alternate(self):
        self._write_bundle(self.dll_v1, self.http_v1, "1.0.0", with_alternate=False)
        bundle = self.manager._load_bundle()
        self.assertFalse(bundle.has_alternate)
        self.assertIsNone(bundle.winhttp_sha256)
        self.assertEqual(bundle.sha256, _sha256(self.dll_v1))


if __name__ == "__main__":
    unittest.main()
