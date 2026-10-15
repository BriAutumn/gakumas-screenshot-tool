"""Focused tests for gkshot.program_updates.

Everything runs on tiny fake executable bundles and injected HTTPS responses;
no network, no shell and no real executable is involved.
"""
import hashlib
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gkshot import program_updates as updates


DEFAULT_FILES = {
    "GKMemoryTool.exe": b"MZ-fake-exe",
    "program/engine.dll": b"engine-dll",
    "catalog/catalog.json": b'{"schema":1}',
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_for(
    files,
    *,
    version="2.0.0",
    product="gk-memory-tool",
    schema=1,
    entrypoint="GKMemoryTool.exe",
    files_map=None,
):
    if files_map is None:
        files_map = {name: sha256(data) for name, data in files.items()}
    return {
        "schema_version": schema,
        "product": product,
        "version": version,
        "entrypoint": entrypoint,
        "files": files_map,
    }


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return Path(path)


def symlink_entry(name, target=b"somewhere"):
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info, target


class FakeResponse:
    def __init__(self, data, url):
        self._data = data
        self._url = url
        self._position = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._position
        chunk = self._data[self._position:self._position + size]
        self._position += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_urlopen(mapping):
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        if url not in mapping:
            raise AssertionError(f"unexpected url: {url}")
        body = mapping[url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body, url)

    opener.calls = calls
    return opener


def release_payload(version="2.0.0", repository="BriAutumn/gakumas-screenshot-tool"):
    archive = f"GKMemoryTool-{version}-windows-x64.zip"
    base = f"https://github.com/{repository}/releases/download/memory-tool-{version}/"
    return {
        "available": True,
        "version": version,
        "assets": {
            "archive": {"name": archive, "url": base + archive, "size": 1024},
            "checksum": {"name": archive + ".sha256", "url": base + archive + ".sha256"},
        },
    }


class ProgramUpdateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.updates_dir = self.tmp / "program-updates"
        self.user_home = self.tmp / "home"
        (self.user_home / "program-updates").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def bundle_zip(self, name="bundle.zip", files=None, manifest=None, entries=None):
        files = dict(files or DEFAULT_FILES)
        if manifest is None:
            manifest = manifest_for(files)
        if entries is None:
            entries = [("release.json", json.dumps(manifest).encode("utf-8"))]
            entries += list(files.items())
        return write_zip(self.tmp / name, entries)

    def stage(self, **kwargs):
        zip_path = kwargs.pop("zip_path", None) or self.bundle_zip()
        return updates.stage_archive(zip_path, self.updates_dir, **kwargs)

    def version_dirs(self):
        if not self.updates_dir.exists():
            return []
        return sorted(p for p in self.updates_dir.glob("version-*"))

    def staging_leftovers(self):
        if not self.updates_dir.exists():
            return []
        return sorted(p for p in self.updates_dir.iterdir() if p.name.startswith(".stage-"))

    def assert_clean_reject(self):
        self.assertEqual(self.version_dirs(), [])
        self.assertEqual(self.staging_leftovers(), [])


class StageArchiveTests(ProgramUpdateTestBase):
    def test_valid_bundle_stages(self):
        result = self.stage()
        self.assertEqual(result["version"], "2.0.0")
        self.assertEqual(result["entrypoint"], "GKMemoryTool.exe")
        directory = Path(result["directory"])
        self.assertTrue(directory.is_dir())
        self.assertTrue(directory.name.startswith("version-"))
        self.assertEqual(len(directory.name), len("version-") + 12)
        self.assertEqual(directory.name, "version-" + result["manifest_sha256"][:12])
        for relative in DEFAULT_FILES:
            self.assertTrue((directory / relative).is_file())
        self.assertEqual(
            (directory / "GKMemoryTool.exe").read_bytes(), DEFAULT_FILES["GKMemoryTool.exe"]
        )

    def test_expected_sha256_checked(self):
        zip_path = self.bundle_zip()
        result = updates.stage_archive(
            zip_path, self.updates_dir, expected_sha256=sha256(zip_path.read_bytes())
        )
        self.assertEqual(result["version"], "2.0.0")

    def test_expected_sha256_mismatch_rejected(self):
        with self.assertRaises(updates.ProgramUpdateError):
            self.stage(expected_sha256="0" * 64)
        self.assert_clean_reject()

    def test_missing_archive_rejected(self):
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(self.tmp / "nope.zip", self.updates_dir)

    def test_rejects_traversal_absolute_backslash_drive(self):
        bad_names = ["../evil.txt", "/abs.txt", "nested\\evil.txt", "C:/evil.txt",
                     '.. /evil.txt', 'CON.txt', 'nested/NUL', 'bad./file', 'file:stream']
        for index, name in enumerate(bad_names):
            with self.subTest(name=name):
                zip_path = self.bundle_zip(
                    name=f"bad{index}.zip",
                    entries=[("release.json", json.dumps(manifest_for(DEFAULT_FILES)).encode())]
                    + list(DEFAULT_FILES.items())
                    + [(name, b"boom")],
                )
                with self.assertRaises(updates.ProgramUpdateError):
                    updates.stage_archive(zip_path, self.updates_dir)
                self.assert_clean_reject()

    def test_rejects_symlink_entry(self):
        zip_path = self.bundle_zip(
            name="link.zip",
            entries=[("release.json", json.dumps(manifest_for(DEFAULT_FILES)).encode())]
            + list(DEFAULT_FILES.items())
            + [symlink_entry("program/link.dll")],
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_rejects_duplicate_entry(self):
        zip_path = self.bundle_zip(
            name="dup.zip",
            entries=[("release.json", json.dumps(manifest_for(DEFAULT_FILES)).encode())]
            + list(DEFAULT_FILES.items())
            + [("program/engine.dll", b"engine-dll")],
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_rejects_case_collision(self):
        zip_path = self.bundle_zip(
            name="case.zip",
            entries=[("release.json", json.dumps(manifest_for(DEFAULT_FILES)).encode())]
            + list(DEFAULT_FILES.items())
            + [("Catalog/catalog.json", b'{"schema":1}')],
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_rejects_oversized_member_and_total(self):
        big = b"x" * 8
        files = {"GKMemoryTool.exe": big, "program/engine.dll": b"engine"}
        with mock.patch.object(updates, "MAX_MEMBER_BYTES", 4):
            zip_path = self.bundle_zip(files=files, name="member.zip")
            with self.assertRaises(updates.ProgramUpdateError):
                updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()
        with mock.patch.object(updates, "MAX_TOTAL_BYTES", 5):
            zip_path = self.bundle_zip(files=files, name="total.zip")
            with self.assertRaises(updates.ProgramUpdateError):
                updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_rejects_incompatible_manifest(self):
        cases = {
            "product": manifest_for(DEFAULT_FILES, product="screenshot-tool"),
            "schema": manifest_for(DEFAULT_FILES, schema=2),
            "version": manifest_for(DEFAULT_FILES, version="beta"),
            "entrypoint": manifest_for(
                DEFAULT_FILES, entrypoint="../evil.exe"
            ),
        }
        for index, (label, manifest) in enumerate(cases.items()):
            with self.subTest(label=label):
                zip_path = self.bundle_zip(name=f"mf{index}.zip", manifest=manifest)
                with self.assertRaises(updates.ProgramUpdateError):
                    updates.stage_archive(zip_path, self.updates_dir)
                self.assert_clean_reject()

    def test_rejects_missing_and_extra_coverage(self):
        files = dict(DEFAULT_FILES)
        manifest = manifest_for(files)
        del files["catalog/catalog.json"]
        zip_path = self.bundle_zip(name="missing.zip", files=files, manifest=manifest)
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

        zip_path = self.bundle_zip(
            name="extra.zip",
            entries=[("release.json", json.dumps(manifest_for(DEFAULT_FILES)).encode())]
            + list(DEFAULT_FILES.items())
            + [("program/extra.dll", b"extra")],
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_rejects_mismatched_hash(self):
        files = dict(DEFAULT_FILES)
        manifest = manifest_for(files)
        manifest["files"]["program/engine.dll"] = "0" * 64
        zip_path = self.bundle_zip(name="hash.zip", files=files, manifest=manifest)
        with self.assertRaises(updates.ProgramUpdateError):
            updates.stage_archive(zip_path, self.updates_dir)
        self.assert_clean_reject()

    def test_repeat_is_idempotent_and_conflict_is_rejected(self):
        zip_path = self.bundle_zip()
        first = updates.stage_archive(zip_path, self.updates_dir)
        second = updates.stage_archive(zip_path, self.updates_dir)
        self.assertEqual(first, second)
        self.assertEqual(len(self.version_dirs()), 1)

        (Path(first["directory"]) / "program/engine.dll").write_bytes(b"tampered")
        with self.assertRaises(FileExistsError):
            updates.stage_archive(zip_path, self.updates_dir)


class CheckReleaseTests(ProgramUpdateTestBase):
    API_URL = (
        "https://api.github.com/repos/BriAutumn/gakumas-screenshot-tool/releases?per_page=100"
    )

    def api(self, releases):
        opener = fake_urlopen({self.API_URL: json.dumps(releases).encode("utf-8")})
        return opener

    @staticmethod
    def release(tag, *, draft=False, prerelease=False, with_assets=True, version=None):
        version = version or tag.split("-")[-1]
        archive = f"GKMemoryTool-{version}-windows-x64.zip"
        base = f"https://github.com/BriAutumn/gakumas-screenshot-tool/releases/download/{tag}/"
        assets = []
        if with_assets:
            assets = [
                {"name": archive, "browser_download_url": base + archive, "size": 10},
                {
                    "name": archive + ".sha256",
                    "browser_download_url": base + archive + ".sha256",
                },
            ]
        return {
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "html_url": f"https://github.com/x/y/releases/tag/{tag}",
            "published_at": "2025-01-01T00:00:00Z",
            "assets": assets,
        }

    def test_picks_highest_supported_release(self):
        releases = [
            self.release("v0.1.0"),  # old screenshot release, wrong prefix
            self.release("memory-tool-9.9.9", draft=True),
            self.release("memory-tool-8.0.0", prerelease=True),
            self.release("memory-tool-1.1.0"),
            self.release("memory-tool-2.0.0", with_assets=False),
            self.release("memory-tool-1.2.0"),
        ]
        result = updates.check_release("1.0.0", urlopen=self.api(releases))
        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "1.2.0")
        self.assertEqual(result["tag"], "memory-tool-1.2.0")
        self.assertEqual(
            result["assets"]["archive"]["name"], "GKMemoryTool-1.2.0-windows-x64.zip"
        )
        self.assertEqual(
            result["assets"]["checksum"]["name"],
            "GKMemoryTool-1.2.0-windows-x64.zip.sha256",
        )

    def test_no_update_returns_chinese_message(self):
        releases = [self.release("memory-tool-1.2.0"), self.release("memory-tool-1.0.0")]
        result = updates.check_release("1.2.0", urlopen=self.api(releases))
        self.assertFalse(result["available"])
        self.assertIn("正式版本", result["message"])

    def test_no_matching_prefix_ignored(self):
        releases = [self.release("screenshot-3.0.0"), self.release("v3.0.0")]
        result = updates.check_release("1.0.0", urlopen=self.api(releases))
        self.assertFalse(result["available"])

    def test_non_numeric_version_ignored_and_malformed_api_rejected(self):
        releases = [self.release("memory-tool-abc")]
        result = updates.check_release("1.0.0", urlopen=self.api(releases))
        self.assertFalse(result["available"])
        opener = fake_urlopen({self.API_URL: b"not json"})
        with self.assertRaises(updates.ProgramUpdateError):
            updates.check_release("1.0.0", urlopen=opener)

    def test_invalid_current_version_rejected(self):
        with self.assertRaises(updates.ProgramUpdateError):
            updates.check_release("latest", urlopen=self.api([]))


class DownloadReleaseTests(ProgramUpdateTestBase):
    def test_download_and_verify(self):
        payload = release_payload()
        archive = payload["assets"]["archive"]
        body = b"PK-fake-archive-bytes"
        checksum_body = f"{sha256(body)}  {archive['name']}\n".encode()
        opener = fake_urlopen(
            {archive["url"]: body, payload["assets"]["checksum"]["url"]: checksum_body}
        )
        result = updates.download_release(payload, self.updates_dir, urlopen=opener)
        final = Path(result["zip_path"])
        self.assertTrue(final.is_file())
        self.assertEqual(final.read_bytes(), body)
        self.assertEqual(result["sha256"], sha256(body))
        self.assertFalse(result["reused"])
        self.assertEqual(
            [p.name for p in self.updates_dir.iterdir() if p.name.startswith(".download-")],
            [],
        )

        reused = updates.download_release(payload, self.updates_dir, urlopen=opener)
        self.assertTrue(reused["reused"])

    def test_checksum_mismatch_rejected(self):
        payload = release_payload()
        archive = payload["assets"]["archive"]
        body = b"PK-fake"
        opener = fake_urlopen(
            {
                archive["url"]: body,
                payload["assets"]["checksum"]["url"]: f"{'0' * 64}  {archive['name']}\n".encode(),
            }
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release(payload, self.updates_dir, urlopen=opener)
        self.assertEqual(list(self.updates_dir.iterdir()), [])

    def test_checksum_filename_mismatch_rejected(self):
        payload = release_payload()
        archive = payload["assets"]["archive"]
        opener = fake_urlopen(
            {
                archive["url"]: b"PK-fake",
                payload["assets"]["checksum"]["url"]: f"{'a' * 64}  other.zip\n".encode(),
            }
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release(payload, self.updates_dir, urlopen=opener)

    def test_untrusted_host_rejected_without_network(self):
        payload = release_payload()
        payload["assets"]["checksum"]["url"] = "https://evil.example.com/checksum"
        opener = fake_urlopen({})
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release(payload, self.updates_dir, urlopen=opener)
        self.assertEqual(opener.calls, [])

    def test_non_https_rejected(self):
        payload = release_payload()
        payload["assets"]["checksum"]["url"] = "http://github.com/x/y"
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release(payload, self.updates_dir, urlopen=fake_urlopen({}))

    def test_oversized_download_rejected(self):
        payload = release_payload()
        archive = payload["assets"]["archive"]
        body = b"x" * 64
        opener = fake_urlopen(
            {
                archive["url"]: body,
                payload["assets"]["checksum"]["url"]: f"{sha256(body)}  {archive['name']}\n".encode(),
            }
        )
        with mock.patch.object(updates, "MAX_ARCHIVE_BYTES", 8):
            with self.assertRaises(updates.ProgramUpdateError):
                updates.download_release(payload, self.updates_dir, urlopen=opener)
        self.assertEqual(list(self.updates_dir.iterdir()), [])

    def test_unavailable_release_rejected(self):
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release({"available": False}, self.updates_dir)

    def test_mismatched_asset_names_rejected(self):
        payload = release_payload()
        payload["assets"]["archive"]["name"] = "SomethingElse.zip"
        with self.assertRaises(updates.ProgramUpdateError):
            updates.download_release(payload, self.updates_dir, urlopen=fake_urlopen({}))


class ActivationTests(ProgramUpdateTestBase):
    def stage_version(self, version, files=None):
        files = dict(files or DEFAULT_FILES)
        zip_path = self.bundle_zip(
            name=f"bundle-{version}.zip",
            files=files,
            manifest=manifest_for(files, version=version),
        )
        return updates.stage_archive(zip_path, self.user_home / "program-updates")

    def pointer(self):
        return json.loads((self.user_home / "program-active.json").read_text("utf-8"))

    def test_activate_from_baseline_records_previous_none(self):
        staged = self.stage_version("1.1.0")
        pointer = updates.activate_staged(Path(staged["directory"]), self.user_home)
        self.assertEqual(pointer["schema_version"], 1)
        self.assertEqual(pointer["active"]["version"], "1.1.0")
        self.assertEqual(pointer["active"]["directory"], staged["directory"])
        self.assertEqual(pointer["active"]["entrypoint"], "GKMemoryTool.exe")
        self.assertEqual(pointer["active"]["manifest_sha256"], staged["manifest_sha256"])
        self.assertIsNone(pointer["previous"])
        self.assertEqual(self.pointer(), pointer)
        self.assertTrue(Path(pointer["active"]["directory"], "GKMemoryTool.exe").is_file())

    def test_second_activation_preserves_previous(self):
        first = self.stage_version("1.1.0")
        second = self.stage_version("1.2.0")
        updates.activate_staged(Path(first["directory"]), self.user_home)
        pointer = updates.activate_staged(Path(second["directory"]), self.user_home)
        self.assertEqual(pointer["active"]["version"], "1.2.0")
        self.assertEqual(pointer["previous"]["version"], "1.1.0")
        self.assertEqual(pointer["previous"]["directory"], first["directory"])

    def test_repeated_activation_is_idempotent(self):
        first = self.stage_version("1.1.0")
        second = self.stage_version("1.2.0")
        updates.activate_staged(Path(first["directory"]), self.user_home)
        updates.activate_staged(Path(second["directory"]), self.user_home)
        again = updates.activate_staged(Path(second["directory"]), self.user_home)
        self.assertEqual(again["active"]["version"], "1.2.0")
        self.assertEqual(again["previous"]["version"], "1.1.0")

    def test_activation_outside_updates_root_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        manifest = manifest_for(DEFAULT_FILES)
        (outside / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
        for name, data in DEFAULT_FILES.items():
            target = outside / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with self.assertRaises(updates.ProgramUpdateError):
            updates.activate_staged(outside, self.user_home)
        with self.assertRaises(updates.ProgramUpdateError):
            updates.activate_staged(self.user_home / "program-updates", self.user_home)

    def test_activation_reverifies_bytes(self):
        staged = self.stage_version("1.1.0")
        (Path(staged["directory"]) / "program/engine.dll").write_bytes(b"tampered")
        with self.assertRaises(updates.ProgramUpdateError):
            updates.activate_staged(Path(staged["directory"]), self.user_home)
        self.assertFalse((self.user_home / "program-active.json").exists())


class RollbackTests(ProgramUpdateTestBase):
    def stage_version(self, version):
        files = dict(DEFAULT_FILES)
        zip_path = self.bundle_zip(
            name=f"bundle-{version}.zip",
            files=files,
            manifest=manifest_for(files, version=version),
        )
        return updates.stage_archive(zip_path, self.user_home / "program-updates")

    def pointer(self):
        return json.loads((self.user_home / "program-active.json").read_text("utf-8"))

    def test_rollback_to_previous_then_to_baseline(self):
        first = self.stage_version("1.1.0")
        second = self.stage_version("1.2.0")
        updates.activate_staged(Path(first["directory"]), self.user_home)
        updates.activate_staged(Path(second["directory"]), self.user_home)

        rolled = updates.rollback(self.user_home)
        self.assertEqual(rolled["active"]["version"], "1.1.0")
        self.assertIsNone(rolled["previous"])
        self.assertEqual(self.pointer(), rolled)
        # History directories are preserved.
        self.assertTrue(Path(first["directory"]).is_dir())
        self.assertTrue(Path(second["directory"]).is_dir())

        cleared = updates.rollback(self.user_home)
        self.assertIsNone(cleared["active"])
        self.assertIsNone(cleared["previous"])
        self.assertEqual(self.pointer(), cleared)

    def test_rollback_without_pointer_rejected(self):
        with self.assertRaises(updates.ProgramUpdateError):
            updates.rollback(self.user_home)

    def test_rollback_rejects_corrupt_previous(self):
        first = self.stage_version("1.1.0")
        second = self.stage_version("1.2.0")
        updates.activate_staged(Path(first["directory"]), self.user_home)
        updates.activate_staged(Path(second["directory"]), self.user_home)
        before = self.pointer()
        (Path(first["directory"]) / "program/engine.dll").write_bytes(b"tampered")
        with self.assertRaises(updates.ProgramUpdateError):
            updates.rollback(self.user_home)
        self.assertEqual(self.pointer(), before)

    def test_rollback_rejects_previous_outside_updates_root(self):
        staged = self.stage_version("1.1.0")
        updates.activate_staged(Path(staged["directory"]), self.user_home)
        pointer = self.pointer()
        pointer["previous"] = {
            "directory": str(self.tmp / "elsewhere"),
            "version": "1.0.0",
            "entrypoint": "GKMemoryTool.exe",
            "manifest_sha256": "0" * 64,
        }
        (self.user_home / "program-active.json").write_text(
            json.dumps(pointer), encoding="utf-8"
        )
        with self.assertRaises(updates.ProgramUpdateError):
            updates.rollback(self.user_home)


if __name__ == "__main__":
    unittest.main()
