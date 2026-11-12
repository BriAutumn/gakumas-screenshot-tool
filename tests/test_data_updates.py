"""Focused tests for gkshot.data_updates.

Everything runs on tiny synthetic repositories and injected clients: no network,
no Node, no shell.  The node-only validation seams and the native builder are
replaced with deterministic fakes, but source selection, raw blob verification,
the real icon plan, catalog validation, native mapping comparison, atomic
activation and rollback are all exercised for real.
"""
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from gkshot.game_master import TABLES as GAME_MASTER_TABLES, snapshot_id
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("LOCALAPPDATA", tempfile.mkdtemp(prefix="gkshot-env-"))

from gkshot import data_updates as du
from gkshot import resource_paths
from gkshot.data_updates import DataUpdateError, DataUpdater

REV = "a" * 40
REV2 = "d" * 40
MREV = "b" * 40
MREV2 = "e" * 40
BUNDLED_REV = "c" * 40

TABLES = {
    "skill_cards": [
        {"id": 1, "name": "Card A", "upgraded": False, "availableCustomizations": "1"}
    ],
    "p_items": [{"id": 1, "name": "Item A", "mode": "stage", "upgraded": False}],
    "stages": [{"id": 160, "name": "Stage", "type": "contest", "plan": "free"}],
    "customizations": [{"id": 1, "name": "Cus", "max": 2}],
}
P_IDOLS = [{"id": 10, "idolId": 1, "title": "Idol"}]

MAPPING = {
    "cards": {"p_card-1": {"0": 1}},
    "items": {"p_item-1": 1},
    "idols": {"idol-1": 10},
    "customizations": {"cus-1": 1},
    "provenance": {"catalog_revision": BUNDLED_REV},
    "evidence": {},
}

#: Column order of every upstream ``packages/gakumas-data/csv/*.csv`` table.
CSV_FIELDS = {
    "idols": ("id", "name"),
    "p_drinks": ("id", "name"),
    "p_idols": ("id", "idolId", "title"),
    "p_items": ("id", "name", "mode", "upgraded"),
    "skill_cards": ("id", "name", "upgraded", "availableCustomizations"),
    "stages": ("id", "name", "type", "plan"),
    "customizations": ("id", "name", "max"),
}

CSV_ROWS = dict(TABLES)
# Numeric-only CSV cells become integers in upstream JS; use an actual list.
CSV_ROWS["skill_cards"] = [dict(TABLES["skill_cards"][0], availableCustomizations="1,1")]
CSV_ROWS["idols"] = [{"id": 1, "name": "花海 咲季"}]
CSV_ROWS["p_drinks"] = [{"id": 1, "name": "初星水"}]
CSV_ROWS["p_idols"] = P_IDOLS

CSV_DIR = "packages/gakumas-data/csv"
JSON_DIR = "packages/gakumas-data/json"


def jdump(obj):
    return json.dumps(obj).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _csv_text(value) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return str(value)


def csv_bytes(rows, fields, *, header=None, newline="\r\n", prefix=b"") -> bytes:
    """Serialise rows the way upstream's committed CSV files look."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=newline)
    writer.writerow(list(fields) if header is None else list(header))
    for row in rows:
        writer.writerow([_csv_text(row[field]) for field in fields])
    return prefix + buffer.getvalue().encode("utf-8")


def source_files():
    files = {}
    for name, rows in TABLES.items():
        files[f"{JSON_DIR}/{name}.json"] = jdump(rows)
    files[f"{JSON_DIR}/p_idols.json"] = jdump(P_IDOLS)
    files["packages/gakumas-engine/index.js"] = b"export const version = 1;\n"
    files["LICENSE"] = b"MIT-license"
    files["scripts/extensionless-loader.mjs"] = b"export async function resolve() {}\n"
    files["tests/lib.mjs"] = b"export function loadoutFromQuery() {}\n"
    files["tests/run.mjs"] = b"process.exit(0);\n"
    files["tests/suite.jsonl"] = b"{}\n"
    files["gakumas-tools/utils/stamina.js"] = b"export const stamina = 1;\n"
    files["gakumas-tools/contexts/LoadoutContext.js"] = b"export const context = 1;\n"
    for name in du.YAML_TABLES:
        files[f"{name}.yaml"] = f"{name}: []\n".encode("utf-8")
    return files


def csv_source_files(rows=None):
    """Upstream tree that only carries ``csv/*.csv`` (no committed JSON)."""
    rows = CSV_ROWS if rows is None else rows
    files = source_files()
    for name in CSV_FIELDS:
        files.pop(f"{JSON_DIR}/{name}.json", None)
        files[f"{CSV_DIR}/{name}.csv"] = csv_bytes(rows[name], CSV_FIELDS[name])
    return files


def new_mapping(revision):
    mapping = json.loads(json.dumps(MAPPING))
    mapping["provenance"] = {"catalog_revision": revision}
    return mapping


def correspondence_for(mapping):
    inventory = {"cards": [], "items": [], "idols": [], "customizations": []}
    for native_id, variants in (mapping.get("cards") or {}).items():
        for upgrade in variants:
            inventory["cards"].append({"native_id": native_id, "upgrade": int(upgrade)})
    for native_id in mapping.get("items") or {}:
        inventory["items"].append({"native_id": native_id})
    for native_id in mapping.get("idols") or {}:
        inventory["idols"].append({"native_id": native_id})
    for native_id in mapping.get("customizations") or {}:
        inventory["customizations"].append({"native_id": native_id})
    return {"inventory": inventory, "summary": {}}


class FakeClient:
    """Offline stand-in for GitHubClient (api metadata + pinned raw host)."""

    def __init__(self, files, *, source_revision=REV, master_revision=MREV,
                 truncated=False, corrupt=False, network_fail=False, bad_sha=False):
        self.files = dict(files)
        self.entries = []
        for path, data in self.files.items():
            sha = blob_sha(data)
            if bad_sha:
                sha = "z" * 40
            self.entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": sha, "size": len(data)}
            )
        self.branches = {du.SOURCE_REPOSITORY: "master", du.MASTER_REPOSITORY: "main"}
        self.commits = {du.SOURCE_REPOSITORY: source_revision, du.MASTER_REPOSITORY: master_revision}
        self.truncated = truncated
        self.corrupt = corrupt
        self.network_fail = network_fail
        self.tree_calls = []
        self.raw_calls = []

    def default_branch(self, repository):
        if self.network_fail:
            raise OSError("network down")
        return self.branches[repository]

    def commit_sha(self, repository, ref):
        return self.commits[repository]

    def tree(self, repository, revision):
        self.tree_calls.append((repository, revision))
        if self.truncated:
            raise DataUpdateError("上游目录树被截断（recursive API），拒绝使用。")
        return list(self.entries)

    def raw(self, repository, revision, path, limit=None):
        self.raw_calls.append((repository, revision, path))
        if self.corrupt:
            return b"not-the-real-blob"
        if path in self.files:
            return self.files[path]
        raise DataUpdateError(f"missing raw {path}")


class StubUpdater(DataUpdater):
    """DataUpdater with the node-only validation seams overridden."""

    def __init__(self, user_home, bundled_root, *, client=None, fail_tests=False, fail_smoke=False,
                 game_master=False):
        super().__init__(user_home, bundled_root, game_master=game_master)
        if client is not None:
            self._client = client
        self._fail_tests = fail_tests
        self._fail_smoke = fail_smoke

    def _validate_upstream_tests(self, stage, revision):
        if self._fail_tests:
            raise DataUpdateError("upstream regression suite failed")
        return {"status": "passed", "exit_code": 0}

    def _validate_smoke(self, stage, revision):
        if self._fail_smoke:
            raise DataUpdateError("worker/reference smoke failed")
        return {"status": "passed", "trials": 3}


def make_bundled_root(base: Path) -> Path:
    root = base / "bundled"
    (root / "runtime" / "node").mkdir(parents=True)
    (root / "runtime" / "node-bin").mkdir(parents=True)
    (root / "runtime" / "node-bin" / "node.exe").write_bytes(b"")
    (root / "runtime" / "node" / "worker.mjs").write_text("// trusted worker\n", encoding="utf-8")
    (root / "runtime" / "node" / "reference.mjs").write_text("// trusted reference\n", encoding="utf-8")
    (root / "runtime" / "node" / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (root / "runtime" / "node" / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "revision": BUNDLED_REV, "files": {}}), encoding="utf-8"
    )

    recognition = root / "assets" / "recognition"
    recognition.mkdir(parents=True)
    files = {}
    for name, rows in TABLES.items():
        raw = jdump(rows)
        (recognition / (name + ".json")).write_bytes(raw)
        files[name + ".json"] = {
            "sha256": sha256(raw),
            "size": len(raw),
            "upstream_path": f"packages/gakumas-data/json/{name}.json",
        }
    icons = recognition / "card-icons"
    icons.mkdir()
    icon_bytes = b"png-bytes"
    (icons / "1.png").write_bytes(icon_bytes)
    files["card-icons/1.png"] = {
        "sha256": sha256(icon_bytes),
        "size": len(icon_bytes),
        "upstream_path": "packages/gakumas-images/images/skillCards/icons/<ID>[_1].png",
    }
    (recognition / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "revision": BUNDLED_REV, "files": files}), encoding="utf-8"
    )

    native = root / "assets" / "native"
    native.mkdir(parents=True)
    (native / "mapping.json").write_text(json.dumps(MAPPING), encoding="utf-8")

    example = root / "examples" / "simulation"
    example.mkdir(parents=True)
    (example / "original.loadout.json").write_text(
        json.dumps({
            "schema_version": 1,
            "stage_id": 160,
            "support_bonus": 0.0,
            "parameters": {"vocal": 1, "dance": 1, "visual": 1, "stamina": 10},
            "item_ids": [0, 0, 0, 0],
            "card_ids": [[1, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0]],
            "customizations": [["", "", "", "", "", ""], ["", "", "", "", "", ""]],
        }),
        encoding="utf-8",
    )
    return root


def write_pointer(home: Path, revision, history=None) -> None:
    root = home / "runtime" / "releases"
    root.mkdir(parents=True, exist_ok=True)
    entry = {
        "revision": revision,
        "release": f"runtime/releases/{revision}",
        "release_manifest_sha256": "0" * 64,
    }
    pointer = {
        "schema_version": 1,
        "active": entry,
        "history": history or [{"revision": None}, entry],
    }
    (root / "active.json").write_text(json.dumps(pointer), encoding="utf-8")


class DataUpdateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bundled = make_bundled_root(self.tmp)
        self.files = source_files()
        self.builder_mapping = None
        self.builder_catalog_revision = None
        self.builder_error = False
        self.builder_sources = []
        self._builder_patch = mock.patch.object(du, "build_native_mapping", side_effect=self._fake_build)
        self._builder_patch.start()
        self.addCleanup(self._builder_patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_build(self, source, cache, output, catalog_root=None, sim_data=None, *, game_master=None):
        if game_master is None:
            self.builder_sources.append(Path(source))
        else:
            self.builder_sources.append(game_master.directory)
            for name in GAME_MASTER_TABLES:
                game_master.read(name)
        if self.builder_error:
            raise DataUpdateError("builder exploded")
        catalog_revision = json.loads((Path(catalog_root) / "manifest.json").read_text("utf-8"))["revision"]
        if self.builder_catalog_revision is not None:
            catalog_revision = self.builder_catalog_revision
        mapping = json.loads(json.dumps(self.builder_mapping if self.builder_mapping is not None else MAPPING))
        mapping["provenance"] = {"catalog_revision": catalog_revision}
        out = Path(output)
        out.mkdir(parents=True, exist_ok=True)
        (out / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
        (out / "full-correspondence.json").write_text(json.dumps(correspondence_for(mapping)), encoding="utf-8")
        (out / "mapping-summary.json").write_text(json.dumps({"cards": {"identity-matched": 1}}), encoding="utf-8")

    def updater(self, **kwargs):
        client = kwargs.pop("client", None)
        if client is None:
            client = FakeClient(self.files)
        return StubUpdater(self.home, self.bundled, client=client, **kwargs)

    def pointer_path(self) -> Path:
        return self.home / "runtime" / "releases" / "active.json"

    def read_pointer(self) -> dict:
        return json.loads(self.pointer_path().read_text("utf-8"))

    def release_dir(self, revision) -> Path:
        return self.home / "runtime" / "releases" / revision

    def upstream_json_dir(self, revision) -> Path:
        return self.release_dir(revision) / "runtime" / "node" / "upstream" / "packages" / "gakumas-data" / "json"

    def staging_leftovers(self):
        root = self.home / "runtime" / "releases"
        if not root.exists():
            return []
        return [p for p in root.iterdir() if p.name.startswith(".stage-")]


class StatusCheckTests(DataUpdateTestBase):
    def test_status_falls_back_to_bundled_revision(self):
        status = self.updater().status()
        self.assertEqual(status["source"], "bundled")
        self.assertEqual(status["revision"], BUNDLED_REV)
        expected = json.loads((resource_paths.RUNTIME_ROOT / "manifest.json").read_text("utf-8"))["revision"]
        self.assertEqual(status["running_revision"], expected)
        self.assertNotEqual(status["running_revision"], status["revision"])

    def test_status_reports_active_upstream_revision(self):
        write_pointer(self.home, REV)
        status = self.updater().status()
        self.assertEqual(status["source"], "upstream")
        self.assertEqual(status["revision"], REV)

    def test_check_returns_explicit_revisions_without_fetching_trees(self):
        client = FakeClient(self.files)
        result = self.updater(client=client).check()
        self.assertEqual(result["revision"], REV)
        self.assertEqual(result["master_revision"], MREV)
        self.assertEqual(result["current_revision"], BUNDLED_REV)
        self.assertTrue(result["update_available"])
        self.assertIn("change_summary", result)
        self.assertEqual(client.tree_calls, [])

    def test_check_reports_up_to_date(self):
        client = FakeClient(self.files, source_revision=BUNDLED_REV)
        result = self.updater(client=client).check()
        self.assertFalse(result["update_available"])
        self.assertEqual(result["revision"], BUNDLED_REV)

    def test_check_network_error_is_distinct_from_up_to_date(self):
        client = FakeClient(self.files, network_fail=True)
        with self.assertRaises(DataUpdateError):
            self.updater(client=client).check()


class InstallSourceTests(DataUpdateTestBase):
    def test_install_requires_explicit_40hex_revisions(self):
        updater = self.updater()
        for bad in ("main", REV.upper(), REV[:39], None, 123):
            with self.subTest(value=bad):
                with self.assertRaises(DataUpdateError):
                    updater.install(bad, MREV)
        with self.assertRaises(DataUpdateError):
            updater.install(REV, "master")

    def test_install_uses_requested_revision_not_latest(self):
        client = FakeClient(self.files, source_revision="9" * 40)
        updater = self.updater(client=client)
        result = updater.install(REV, MREV)
        self.assertEqual(result["revision"], REV)
        self.assertTrue(result["requires_restart"])
        self.assertEqual(client.tree_calls[0], (du.SOURCE_REPOSITORY, REV))

    def test_sources_fetched_from_pinned_raw_not_blob_api(self):
        client = FakeClient(self.files)
        self.updater(client=client).install(REV, MREV)
        self.assertFalse(hasattr(FakeClient, "blob"))
        self.assertTrue(client.raw_calls)
        # Every fetch is pinned to the explicitly requested revision.
        self.assertEqual({revision for _, revision, _ in client.raw_calls}, {REV, MREV})
        self.assertIn((du.SOURCE_REPOSITORY, REV, "packages/gakumas-engine/index.js"), client.raw_calls)
        # Character.yaml is part of the native source set.
        self.assertIn((du.MASTER_REPOSITORY, MREV, "Character.yaml"), client.raw_calls)

    def test_native_sources_are_not_inside_release(self):
        updater = self.updater()
        updater.install(REV, MREV)
        release = self.release_dir(REV)
        self.assertFalse((release / "native-sources").exists())
        cache = self.home / "data-update-cache" / "native-sources" / MREV
        self.assertTrue((cache / "Character.yaml").is_file())
        self.assertEqual(self.builder_sources[0], cache)

    def test_install_happy_path_publishes_release_and_pointer(self):
        updater = self.updater()
        result = updater.install(REV, MREV)
        release = self.release_dir(REV)
        self.assertTrue((release / "release.json").is_file())
        self.assertTrue((release / "assets" / "recognition" / "manifest.json").is_file())
        self.assertTrue((release / "assets" / "native" / "mapping.json").is_file())
        self.assertTrue((release / "runtime" / "node" / "manifest.json").is_file())

        recognition = json.loads((release / "assets" / "recognition" / "manifest.json").read_text("utf-8"))
        self.assertEqual(recognition["schema_version"], 1)
        self.assertEqual(recognition["revision"], REV)
        for name in ("skill_cards.json", "p_items.json", "stages.json", "customizations.json"):
            self.assertIn(name, recognition["files"])
            self.assertIn("sha256", recognition["files"][name])
            self.assertIn("upstream_path", recognition["files"][name])

        runtime = json.loads((release / "runtime" / "node" / "manifest.json").read_text("utf-8"))
        self.assertEqual(runtime["revision"], REV)
        self.assertIn("packages/gakumas-engine/index.js", runtime["files"])

        mapping = json.loads((release / "assets" / "native" / "mapping.json").read_text("utf-8"))
        self.assertEqual(mapping["provenance"]["catalog_revision"], REV)
        self.assertEqual(mapping["provenance"]["master_revision"], MREV)

        release_record = json.loads((release / "release.json").read_text("utf-8"))
        self.assertEqual(release_record["schema_version"], 1)
        self.assertEqual(release_record["revision"], REV)
        self.assertEqual(release_record["validation"]["status"], "passed")
        self.assertEqual(release_record["validation"]["source_master_revision"], MREV)
        self.assertNotIn("release.json", release_record["files"])

        pointer = self.read_pointer()
        self.assertEqual(pointer["schema_version"], 1)
        self.assertEqual(pointer["active"]["revision"], REV)
        self.assertEqual(pointer["history"][0], {"revision": None})
        self.assertEqual(pointer["history"][-1]["revision"], REV)
        self.assertEqual(
            pointer["active"]["release_manifest_sha256"],
            sha256((release / "release.json").read_bytes()),
        )

        self.assertEqual(updater.status()["source"], "upstream")
        self.assertEqual(updater.status()["revision"], REV)
        self.assertEqual(result["counts"]["cards"], 1)
        self.assertEqual(self.staging_leftovers(), [])

    def test_existing_known_icon_is_carried_over(self):
        self.updater().install(REV, MREV)
        icon = self.release_dir(REV) / "assets" / "recognition" / "card-icons" / "1.png"
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.read_bytes(), b"png-bytes")

    def test_added_catalog_id_downloads_new_icon(self):
        files = source_files()
        files["packages/gakumas-data/json/skill_cards.json"] = jdump(
            [
                {"id": 1, "name": "Card A", "upgraded": False, "availableCustomizations": "1"},
                {"id": 2, "name": "Card B", "upgraded": False, "availableCustomizations": "1"},
            ]
        )
        icon_path = "packages/gakumas-images/images/skillCards/icons/2_1.png"
        files[icon_path] = b"new-card-png"
        client = FakeClient(files)
        self.updater(client=client).install(REV, MREV)

        release = self.release_dir(REV)
        new_icon = release / "assets" / "recognition" / "card-icons" / "2.png"
        self.assertTrue(new_icon.is_file())
        self.assertEqual(new_icon.read_bytes(), b"new-card-png")
        manifest = json.loads((release / "assets" / "recognition" / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["files"]["card-icons/2.png"]["upstream_path"], icon_path)
        self.assertIn((du.SOURCE_REPOSITORY, REV, icon_path), client.raw_calls)
        # The known icon is still carried over unchanged.
        self.assertEqual(
            (release / "assets" / "recognition" / "card-icons" / "1.png").read_bytes(), b"png-bytes"
        )

    def test_install_is_idempotent(self):
        updater = self.updater()
        updater.install(REV, MREV)
        updater.install(REV, MREV)
        pointer = self.read_pointer()
        self.assertEqual(len(pointer["history"]), 2)

    def test_conflicting_master_revision_refused(self):
        self.updater().install(REV, MREV)
        with self.assertRaises(DataUpdateError):
            self.updater().install(REV, MREV2)
        self.assertEqual(self.read_pointer()["active"]["revision"], REV)

    def test_existing_release_mapping_provenance_is_validated(self):
        self.updater().install(REV, MREV)
        release = self.release_dir(REV)
        mapping_path = release / "assets" / "native" / "mapping.json"
        mapping = json.loads(mapping_path.read_text("utf-8"))
        mapping["provenance"]["master_revision"] = MREV2
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
        # Keep the strict inventory consistent so only the provenance check fails.
        record_path = release / "release.json"
        record = json.loads(record_path.read_text("utf-8"))
        record["files"]["assets/native/mapping.json"] = sha256(mapping_path.read_bytes())
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(DataUpdateError):
            self.updater().install(REV, MREV)
        self.assertEqual(self.read_pointer()["active"]["revision"], REV)


class GameMasterSyncTests(DataUpdateTestBase):
    def publish_snapshot(self, value):
        root = self.home / "native-export" / "game-master"
        tables = {}
        for name in GAME_MASTER_TABLES:
            row = {"id": "native", "upgradeCount": 0, "customizeCount": 1, "type": "label",
                   "name": value}
            tables[name + ".json"] = jdump([row])
        files = {name: hashlib.sha256(data).hexdigest() for name, data in tables.items()}
        revision = snapshot_id(files)
        folder = root / "snapshots" / revision
        folder.mkdir(parents=True)
        manifest = jdump({"schema_version": 1, "snapshot": revision, "files": files})
        (root / "active.json").write_bytes(manifest)
        (folder / "snapshot.json").write_bytes(manifest)
        for name, data in tables.items():
            (folder / name).write_bytes(data)
        return revision, folder

    def test_same_engine_can_install_new_game_snapshot_and_roll_back(self):
        client = FakeClient(csv_source_files())
        del client.branches[du.MASTER_REPOSITORY]
        updater = self.updater(client=client, game_master=True)
        first, _ = self.publish_snapshot("first")
        self.assertEqual(updater.check()["master_revision"], first)
        updater.install(REV, first)
        from gkshot.resource_paths import resolve_paths
        first_path = resolve_paths(self.home)[0]
        self.assertFalse(updater.check()["update_available"])
        second, _ = self.publish_snapshot("second")
        self.assertTrue(updater.check()["update_available"])
        updater.install(REV, second)
        second_path = resolve_paths(self.home)[0]
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.read_pointer()["active"]["game_master_snapshot"], second)
        updater.rollback()
        self.assertEqual(resolve_paths(self.home)[0], first_path)
        self.assertTrue(all(repo == du.SOURCE_REPOSITORY for repo, _ in client.tree_calls))
        self.assertTrue(all(repo == du.SOURCE_REPOSITORY for repo, _, _ in client.raw_calls))

    def test_tampered_local_table_cannot_activate(self):
        revision, folder = self.publish_snapshot("original")
        (folder / "ProduceCard.json").write_bytes(b"[]")
        updater = self.updater(client=FakeClient(csv_source_files()), game_master=True)
        with self.assertRaisesRegex(DataUpdateError, r"ProduceCard.json.*校验失败"):
            updater.install(REV, revision)
        self.assertFalse(self.pointer_path().exists())

    def test_no_game_export_can_update_with_existing_mapping(self):
        old_idols = self.bundled / "runtime/node/upstream/packages/gakumas-data/json/p_idols.json"
        old_idols.parent.mkdir(parents=True, exist_ok=True)
        old_idols.write_bytes(jdump(P_IDOLS))
        updater = self.updater(client=FakeClient(source_files()), game_master=True)
        update = updater.check()
        self.assertTrue(update["change_summary"]["uses_existing_mapping"])
        updater.install(REV, update["master_revision"])
        self.assertFalse(updater.check()["update_available"])
        self.assertEqual(self.read_pointer()["active"]["revision"], REV)


class CsvOnlySourceTests(DataUpdateTestBase):
    """Upstream no longer commits ``packages/gakumas-data/json/*.json``."""

    def install_csv_only(self, rows=None):
        files = csv_source_files(rows)
        self.updater(client=FakeClient(files)).install(REV, MREV)
        return files

    def test_every_upstream_csv_table_is_materialized(self):
        files = self.install_csv_only()
        json_dir = self.upstream_json_dir(REV)
        for name in CSV_FIELDS:
            with self.subTest(table=name):
                path = json_dir / (name + ".json")
                self.assertTrue(path.is_file())
                rows = json.loads(path.read_text("utf-8"))
                self.assertTrue(rows)
        # Generated bytes are exactly what upstream's csv_to_json.py produced.
        for name in CSV_FIELDS:
            expected = json.dumps(CSV_ROWS[name], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.assertEqual((json_dir / (name + ".json")).read_bytes(), expected)
        # The CSV sources are still part of the immutable release.
        csv_dir = self.release_dir(REV) / "runtime" / "node" / "upstream" / "packages" / "gakumas-data" / "csv"
        for name in CSV_FIELDS:
            self.assertEqual((csv_dir / (name + ".csv")).read_bytes(), files[f"{CSV_DIR}/{name}.csv"])

    def test_description_csv_keeps_records_that_share_an_entity_id(self):
        files = csv_source_files()
        files[f"{CSV_DIR}/descriptions.csv"] = (
            b"type,id,description\nskillCard,1,Card text\npItem,1,Item text\npDrink,1,Drink text\n"
        )
        self.updater(client=FakeClient(files)).install(REV, MREV)
        path = self.upstream_json_dir(REV) / "descriptions.json"
        self.assertEqual(json.loads(path.read_bytes()), [
            {"type": "skillCard", "id": 1, "description": "Card text"},
            {"type": "pItem", "id": 1, "description": "Item text"},
            {"type": "pDrink", "id": 1, "description": "Drink text"},
        ])

    def test_duplicate_description_type_and_id_blocks_activation(self):
        files = csv_source_files()
        files[f"{CSV_DIR}/descriptions.csv"] = (
            b"type,id,description\nskillCard,1,First\npItem,1,Item\nskillCard,1,Second\n"
        )
        with self.assertRaisesRegex(DataUpdateError, r"descriptions.*skillCard, 1"):
            self.updater(client=FakeClient(files)).install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())

    def test_conversion_types_reach_the_runtime_tables(self):
        self.install_csv_only()
        json_dir = self.upstream_json_dir(REV)
        cards = json.loads((json_dir / "skill_cards.json").read_text("utf-8"))
        self.assertEqual(cards[0]["id"], 1)
        self.assertIs(cards[0]["upgraded"], False)
        self.assertEqual(cards[0]["availableCustomizations"], "1,1")
        items = json.loads((json_dir / "p_items.json").read_text("utf-8"))
        self.assertIs(items[0]["upgraded"], False)
        self.assertEqual(items[0]["mode"], "stage")
        self.assertEqual(json.loads((json_dir / "p_idols.json").read_text("utf-8")), P_IDOLS)
        self.assertEqual(json.loads((json_dir / "idols.json").read_text("utf-8"))[0]["name"], "花海 咲季")

    def test_generated_tables_are_integrity_protected(self):
        self.install_csv_only()
        release = self.release_dir(REV)
        json_dir = self.upstream_json_dir(REV)
        runtime = json.loads((release / "runtime" / "node" / "manifest.json").read_text("utf-8"))
        record = json.loads((release / "release.json").read_text("utf-8"))
        for name in CSV_FIELDS:
            rel = f"packages/gakumas-data/json/{name}.json"
            with self.subTest(table=name):
                self.assertIn(rel, runtime["files"])
                self.assertEqual(runtime["files"][rel], sha256((json_dir / (name + ".json")).read_bytes()))
                self.assertIn(f"runtime/node/upstream/{rel}", record["files"])
        # The 4 recognition tables are still copied and hashed for the Catalog.
        recognition = json.loads((release / "assets" / "recognition" / "manifest.json").read_text("utf-8"))
        for name in ("skill_cards", "p_items", "stages", "customizations"):
            self.assertEqual(
                recognition["files"][name + ".json"]["upstream_path"], f"{CSV_DIR}/{name}.csv"
            )
            self.assertEqual(
                (release / "assets" / "recognition" / (name + ".json")).read_bytes(),
                (json_dir / (name + ".json")).read_bytes(),
            )

    def test_csv_only_install_activates_and_keeps_native_ids(self):
        self.install_csv_only()
        pointer = self.read_pointer()
        self.assertEqual(pointer["active"]["revision"], REV)
        record = json.loads((self.release_dir(REV) / "release.json").read_text("utf-8"))
        self.assertEqual(record["validation"]["status"], "passed")
        counts = record["validation"]["counts"]
        self.assertEqual(counts["cards"], 1)
        self.assertEqual(counts["idols"], len(P_IDOLS))
        mapping = json.loads((self.release_dir(REV) / "assets" / "native" / "mapping.json").read_text("utf-8"))
        # Native idol identities survive because p_idols.json was generated.
        self.assertEqual(mapping["idols"], {"idol-1": 10})
        self.assertEqual(mapping["cards"], {"p_card-1": {"0": 1}})
        self.assertEqual(self.staging_leftovers(), [])

    def test_generated_tables_are_byte_deterministic(self):
        updater = self.updater(client=FakeClient(csv_source_files()))
        updater.install(REV, MREV)
        updater.install(REV2, MREV)
        for name in CSV_FIELDS:
            with self.subTest(table=name):
                self.assertEqual(
                    (self.upstream_json_dir(REV) / (name + ".json")).read_bytes(),
                    (self.upstream_json_dir(REV2) / (name + ".json")).read_bytes(),
                )

    def test_missing_p_idols_is_refused(self):
        files = csv_source_files()
        files.pop(f"{CSV_DIR}/p_idols.csv")
        with self.assertRaises(DataUpdateError):
            self.updater(client=FakeClient(files)).install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())
        self.assertFalse(self.release_dir(REV).exists())
        self.assertEqual(self.staging_leftovers(), [])

    def test_malformed_non_catalog_csv_blocks_activation(self):
        files = csv_source_files()
        files[f"{CSV_DIR}/p_drinks.csv"] = b"id,name\r\n1,drink,extra\r\n"
        with self.assertRaises(DataUpdateError):
            self.updater(client=FakeClient(files)).install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())
        self.assertFalse(self.release_dir(REV).exists())
        self.assertEqual(self.staging_leftovers(), [])

    def test_invalid_row_ids_block_activation(self):
        rows = dict(CSV_ROWS)
        rows["p_idols"] = [{"id": "not-a-number", "idolId": 1, "title": "Idol"}]
        files = csv_source_files(rows)
        with self.assertRaises(DataUpdateError):
            self.updater(client=FakeClient(files)).install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())


class UpstreamJsonPreferenceTests(DataUpdateTestBase):
    def test_existing_upstream_json_is_preferred_and_not_rewritten(self):
        original = jdump(
            [{"id": 1, "name": "JSON wins", "upgraded": True, "availableCustomizations": "1"}]
        )
        files = csv_source_files()
        files[f"{JSON_DIR}/skill_cards.json"] = original
        files[f"{CSV_DIR}/skill_cards.csv"] = csv_bytes(
            [{"id": 1, "name": "CSV loses", "upgraded": False, "availableCustomizations": "1"}],
            CSV_FIELDS["skill_cards"],
        )
        self.updater(client=FakeClient(files)).install(REV, MREV)

        json_dir = self.upstream_json_dir(REV)
        self.assertEqual((json_dir / "skill_cards.json").read_bytes(), original)
        recognition = json.loads(
            (self.release_dir(REV) / "assets" / "recognition" / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(
            recognition["files"]["skill_cards.json"]["upstream_path"],
            f"{JSON_DIR}/skill_cards.json",
        )
        # Tables without committed JSON are still generated from CSV.
        self.assertIs(
            json.loads((json_dir / "p_items.json").read_text("utf-8"))[0]["upgraded"], False
        )

    def test_malformed_csv_is_ignored_when_upstream_json_exists(self):
        files = csv_source_files()
        files[f"{JSON_DIR}/p_drinks.json"] = jdump([{"id": 1, "name": "初星水"}])
        files[f"{CSV_DIR}/p_drinks.csv"] = b"id,name\r\n1,drink,extra\r\n"
        self.updater(client=FakeClient(files)).install(REV, MREV)
        self.assertEqual(
            json.loads((self.upstream_json_dir(REV) / "p_drinks.json").read_text("utf-8")),
            [{"id": 1, "name": "初星水"}],
        )
        self.assertEqual(self.read_pointer()["active"]["revision"], REV)


class CsvConversionUnitTests(unittest.TestCase):
    """The conversion mirrors upstream ``scripts/csv_to_json.py`` exactly."""

    def parse(self, text, table="t"):
        return du._parse_csv_table(table, text.encode("utf-8"))

    def test_scalar_types_match_upstream(self):
        rows = self.parse(
            "id,name,flag,off,price,ratio,neg,text\r\n"
            '1,"a,b","TRUE","FALSE",70,0.5,-1,やる気+\r\n'
        )
        self.assertEqual(
            rows,
            [{
                "id": 1,
                "name": "a,b",
                "flag": True,
                "off": False,
                "price": 70,
                "ratio": "0.5",
                "neg": "-1",
                "text": "やる気+",
            }],
        )

    def test_header_order_is_preserved(self):
        rows = self.parse("b,a\n1,2\n")
        self.assertEqual(list(rows[0].items()), [("b", 1), ("a", 2)])

    def test_quoted_commas_and_embedded_newlines_are_preserved(self):
        rows = self.parse('id,text\r\n1,"line1\r\nline2"\r\n')
        self.assertEqual(rows[0]["text"], "line1\r\nline2")
        rows = self.parse('id,text\n1,"a,b,c"\n')
        self.assertEqual(rows[0]["text"], "a,b,c")

    def test_bom_is_ignored_and_unicode_preserved(self):
        raw = b"\xef\xbb\xbfid,name\r\n1,Caf\xc3\xa9 \xe2\x99\xa1\r\n"
        rows = du._parse_csv_table("t", raw)
        self.assertEqual(rows, [{"id": 1, "name": "Café ♡"}])

    def test_empty_cells_stay_empty_strings(self):
        self.assertEqual(self.parse("id,title\r\n131,\r\n"), [{"id": 131, "title": ""}])

    def test_encoding_is_compact_utf8(self):
        rows = du._parse_csv_table("t", b"id,name\n1,\xe5\x88\x9d\xe6\x98\x9f\xe6\xb0\xb4\n")
        self.assertEqual(
            du._encode_json_table(rows),
            '[{"id":1,"name":"初星水"}]'.encode("utf-8"),
        )

    def test_extra_field_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("id,name\n1,a,b\n")

    def test_missing_field_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("id,name\n1\n")

    def test_duplicate_header_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("id,id\n1,2\n")

    def test_empty_header_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("id,\n1,2\n")

    def test_missing_header_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("")

    def test_padded_header_rejected(self):
        with self.assertRaises(DataUpdateError):
            self.parse("id, name\n1,2\n")

    def test_non_ascii_numeric_stays_string_like_upstream_js(self):
        self.assertEqual(self.parse("id,text\n1,²\n"), [{"id": 1, "text": "²"}])

    def test_invalid_utf8_rejected(self):
        with self.assertRaises(DataUpdateError):
            du._parse_csv_table("t", b"id,name\n1,\xff\xfe\n")

    def test_non_numeric_and_duplicate_ids_rejected(self):
        with self.assertRaises(DataUpdateError):
            du._validate_generated_table("t", du._parse_csv_table("t", b"id,name\nx,y\n"))
        with self.assertRaises(DataUpdateError):
            du._validate_generated_table("t", du._parse_csv_table("t", b"id\n1\n1\n"))
        with self.assertRaises(DataUpdateError):
            du._validate_generated_table("t", [])

    def test_real_upstream_csv_semantics(self):
        # Truthy cells, digits and quoted commas, as shipped by upstream.
        raw = (
            "id,alias,name,type,price,max,forceInitialHand,conditions,cost,actions,limit,effects\r\n"
            "36,forceInitialHand1,開始時手札に入る,effect,70,1,TRUE,,,,,\r\n"
            '56,motivation2,やる気+,buff,"40,70",2,,,,,,level:1 { a } level:2 { b }\r\n'
        ).encode("utf-8")
        rows = du._parse_csv_table("customizations", raw)
        self.assertIs(rows[0]["forceInitialHand"], True)
        self.assertEqual(rows[0]["conditions"], "")
        self.assertEqual(rows[1]["price"], "40,70")
        self.assertEqual(rows[1]["max"], 2)


class InstallFailureTests(DataUpdateTestBase):
    def test_hash_failure_preserves_active_pointer(self):
        self.updater().install(REV, MREV)
        before = self.read_pointer()
        changed = source_files()
        changed["packages/gakumas-engine/index.js"] = b"export const version = 2;\n"
        bad = self.updater(client=FakeClient(changed, corrupt=True))
        with self.assertRaises(DataUpdateError):
            bad.install(REV2, MREV)
        self.assertEqual(self.read_pointer(), before)
        self.assertFalse(self.release_dir(REV2).exists())
        self.assertEqual(self.staging_leftovers(), [])

    def test_truncated_tree_is_rejected(self):
        updater = self.updater(client=FakeClient(self.files, truncated=True))
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())
        self.assertEqual(self.staging_leftovers(), [])

    def test_bad_tree_blob_sha_is_rejected(self):
        updater = self.updater(client=FakeClient(self.files, bad_sha=True))
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())

    def test_builder_failure_leaves_no_partial_release(self):
        self.builder_error = True
        updater = self.updater()
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.release_dir(REV).exists())
        self.assertFalse(self.pointer_path().exists())
        self.assertEqual(self.staging_leftovers(), [])

    def test_upstream_test_failure_prevents_activation(self):
        updater = self.updater(fail_tests=True)
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())
        self.assertFalse(self.release_dir(REV).exists())

    def test_catalog_schema_error_blocks_activation(self):
        files = source_files()
        files["packages/gakumas-data/json/skill_cards.json"] = jdump(
            [{"id": 1, "upgraded": False, "availableCustomizations": "1"}]
        )
        updater = self.updater(client=FakeClient(files))
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())

    def test_duplicate_catalog_id_blocks_activation(self):
        files = source_files()
        files["packages/gakumas-data/json/p_items.json"] = jdump(
            [
                {"id": 1, "name": "Item A", "mode": "stage", "upgraded": False},
                {"id": 1, "name": "Item B", "mode": "stage", "upgraded": False},
            ]
        )
        updater = self.updater(client=FakeClient(files))
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)

    def test_unknown_customization_reference_blocks_activation(self):
        files = source_files()
        files["packages/gakumas-data/json/skill_cards.json"] = jdump(
            [{"id": 1, "name": "Card", "upgraded": False, "availableCustomizations": "9"}]
        )
        updater = self.updater(client=FakeClient(files))
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)

    def test_weakened_native_mapping_is_rejected(self):
        self.builder_mapping = new_mapping(REV)
        self.builder_mapping["cards"] = {}
        updater = self.updater()
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)
        self.assertFalse(self.pointer_path().exists())

    def test_mapping_to_unknown_simulator_id_is_rejected(self):
        self.builder_mapping = new_mapping(REV)
        self.builder_mapping["cards"] = {"p_card-1": {"0": 999}}
        updater = self.updater()
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)

    def test_wrong_catalog_revision_is_rejected(self):
        self.builder_catalog_revision = REV2
        updater = self.updater()
        with self.assertRaises(DataUpdateError):
            updater.install(REV, MREV)

    def test_reinstall_over_corrupt_release_is_refused(self):
        self.updater().install(REV, MREV)
        before = self.read_pointer()
        target = self.release_dir(REV) / "runtime" / "node" / "manifest.json"
        target.write_bytes(b"corrupted")
        with self.assertRaises(DataUpdateError):
            self.updater().install(REV, MREV)
        self.assertEqual(self.read_pointer(), before)


class RollbackTests(DataUpdateTestBase):
    def test_rollback_returns_to_previous_then_baseline(self):
        updater = self.updater()
        updater.install(REV, MREV)
        updater.install(REV2, MREV)
        self.assertEqual(self.read_pointer()["active"]["revision"], REV2)

        rolled = updater.rollback()
        self.assertEqual(rolled["revision"], REV)
        self.assertTrue(rolled["requires_restart"])
        self.assertEqual(self.read_pointer()["active"]["revision"], REV)
        # Completed versions are never deleted.
        self.assertTrue(self.release_dir(REV).is_dir())
        self.assertTrue(self.release_dir(REV2).is_dir())

        cleared = updater.rollback()
        self.assertEqual(cleared["source"], "bundled")
        pointer = self.read_pointer()
        self.assertIsNone(pointer["active"]["revision"])
        self.assertEqual(updater.status()["source"], "bundled")

    def test_rollback_without_pointer_is_refused(self):
        with self.assertRaises(DataUpdateError):
            self.updater().rollback()

    def test_rollback_refuses_corrupt_previous_and_preserves_pointer(self):
        updater = self.updater()
        updater.install(REV, MREV)
        updater.install(REV2, MREV)
        before = self.read_pointer()
        (self.release_dir(REV) / "assets" / "recognition" / "manifest.json").write_bytes(b"tampered")
        with self.assertRaises(DataUpdateError):
            updater.rollback()
        self.assertEqual(self.read_pointer(), before)


class HelperTests(DataUpdateTestBase):
    def test_git_blob_sha1_matches_git(self):
        self.assertEqual(
            du._git_blob_sha1(b"hello\n"),
            "ce013625030ba8dba906f756967f9e9ca394464a",
        )

    def test_validate_relative_path_rejects_escapes(self):
        for bad in ("../evil", "/abs", "a\\b", "C:/x", "a//b", "CON.txt", "trail."):
            with self.subTest(value=bad):
                with self.assertRaises(DataUpdateError):
                    du._validate_relative_path(bad)
        self.assertEqual(du._validate_relative_path("packages/gakumas-engine/index.js"),
                         "packages/gakumas-engine/index.js")

    def test_expand_template_prefers_underscore_variant(self):
        candidates = du._expand_template(
            "packages/gakumas-images/images/skillCards/icons/<ID>[_1].png", 42
        )
        self.assertEqual(
            candidates,
            [
                "packages/gakumas-images/images/skillCards/icons/42_1.png",
                "packages/gakumas-images/images/skillCards/icons/42.png",
            ],
        )

    def test_query_from_loadout_uses_upstream_keys(self):
        from urllib.parse import parse_qs

        loadout = du.Loadout.from_dict({
            "schema_version": 1,
            "stage_id": 160,
            "support_bonus": 0.0,
            "parameters": {"vocal": 1, "dance": 2, "visual": 3, "stamina": 10},
            "item_ids": [0, 0, 0, 0],
            "card_ids": [[1, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0]],
            "customizations": [["", "", "", "", "", ""], ["", "", "", "", "", ""]],
        })
        parsed = parse_qs(du._query_from_loadout(loadout), keep_blank_values=True)
        self.assertEqual(parsed["stage"], ["160"])
        self.assertEqual(parsed["params"], ["1-2-3-10"])
        self.assertEqual(parsed["items"], ["0-0-0-0"])
        self.assertEqual(parsed["cards"], ["1-0-0-0-0-0_0-0-0-0-0-0"])

    def test_walk_files_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "real.txt").write_text("x", encoding="utf-8")
            try:
                os.symlink(root / "real.txt", root / "link.txt")
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows 当前用户没有创建符号链接权限")
                raise
            with self.assertRaises(DataUpdateError):
                du._walk_files(root)

    def test_verify_release_rejects_tampered_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            bundled = make_bundled_root(Path(temp))
            updater = StubUpdater(home, bundled, client=FakeClient(source_files()))
            updater.install(REV, MREV)
            release = home / "runtime" / "releases" / REV
            (release / "runtime" / "node" / "worker.mjs").write_bytes(b"tampered")
            with self.assertRaises(DataUpdateError):
                updater._verify_release(release, REV)


if __name__ == "__main__":
    unittest.main()
