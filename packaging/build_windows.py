"""Build an allowlisted Windows x64 onedir. Never copy a checkout wholesale."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "24.16.0"
NODE_SHA256 = "edaca9bd58ec8e92037dac4e877d52f6b8f430b81c18b57e264b4e2fb111cd56"
RESOURCE_TREES = ("assets/recognition/", "assets/ocr/", "configs/", "runtime/node/", "src/gkshot/web/")
RESOURCE_FILES = ("examples/simulation/original.loadout.json", "THIRD_PARTY.md", "docs/user-guide.html")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def approved_resource(name):
    parts = Path(name).parts
    forbidden = {"node_modules", "__pycache__", ".venv", ".git", "cache"}
    if any(part in forbidden or part.startswith(".env") for part in parts):
        return False
    return name in RESOURCE_FILES or name.startswith(RESOURCE_TREES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-zip", type=Path, required=True, help="Official node-v24.16.0-win-x64.zip")
    parser.add_argument("--name", default="GKTool-preview")
    parser.add_argument("--release-label", default="integration-preview")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("Build on Windows x64 with Python 3.13 x64")
    if not args.name.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("Invalid distribution name")
    if digest(args.node_zip) != NODE_SHA256:
        raise SystemExit("Node archive SHA256 mismatch")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stage = ROOT / "build" / f"portable-{stamp}"
    stage.mkdir(parents=True, exist_ok=False)
    resources = stage / "resources"
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in tracked:
        if approved_resource(name):
            target = resources / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
    manifest = json.loads((resources / "runtime/node/manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest['files'].items():
        path = resources / 'runtime/node/upstream' / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError(f"Incomplete engine resources: {name}")
    with zipfile.ZipFile(args.node_zip) as archive:
        for name in ("node.exe", "LICENSE"):
            target = resources / "runtime/node-bin" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(f"node-v{NODE_VERSION}-win-x64/{name}"))
    licenses = resources / "licenses"
    licenses.mkdir()
    inventory = []
    for dist in sorted(metadata.distributions(), key=lambda item: item.metadata['Name'].lower()):
        name = dist.metadata["Name"]
        copied = []
        for file in dist.files or []:
            if "__pycache__" in file.parts or file.suffix.lower() in (".py", ".pyc", ".pyd", ".dll"):
                continue
            if any("license" in part.lower() or "copying" in part.lower() or "notice" in part.lower() for part in file.parts):
                source = Path(dist.locate_file(file))
                if not source.is_file():
                    continue
                target = licenses / name / Path(*[p for p in file.parts if p not in ("..", ".")])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(target.relative_to(resources)).replace("\\", "/"))
        inventory.append({"name": name, "version": dist.version,
            "license_expression": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
            "license_files": copied, "scope": "build environment; subset bundled at runtime"})
    python_license = Path(sys.base_prefix) / "LICENSE_PYTHON.txt"
    if not python_license.exists():
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.exists():
        raise SystemExit("Python license missing")
    shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    inventory += [{"name": "Python", "version": platform.python_version(), "provider": sys.version},
                  {"name": "Node.js", "version": NODE_VERSION, "archive_sha256": NODE_SHA256,
                   "url": f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"}]
    (resources / "dependencies.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    changes = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    (resources / "build-info.json").write_text(json.dumps({"revision": revision, "dirty": bool(changes),
        "built_at": stamp, "release_label": args.release_label,
        "status": "prerelease; recognition completeness is not guaranteed"}, indent=2), encoding="utf-8")
    output = ROOT / "dist" / stamp
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed",
        "--noupx", "--name", args.name, "--paths", str(ROOT / "src"),
        "--distpath", str(output), "--workpath", str(stage / "pyinstaller"), "--specpath", str(stage),
        "--collect-all", "rapidocr_onnxruntime", "--collect-all", "onnxruntime",
        "--collect-submodules", "gkshot", "--add-data", f"{resources};.", str(ROOT / "packaging/entry.py")]
    subprocess.run(command, check=True, cwd=ROOT)
    print("Collecting native dependency notices...", flush=True)
    folder = output / args.name
    # Conda-based Python brings native dependencies beyond pip's metadata.
    # Preserve their notices too; no package caches are copied wholesale.
    native_names = {p.name.lower() for p in (folder / "_internal").rglob("*") if p.suffix.lower() in (".dll", ".pyd")}
    native_inventory = []
    for record in (Path(sys.base_prefix) / "conda-meta").glob("*.json"):
        package = json.loads(record.read_text(encoding="utf-8"))
        matches = sorted({Path(name).name for name in package.get("files", []) if Path(name).name.lower() in native_names})
        if not matches:
            continue
        source = Path(package.get("link", {}).get("source", Path(sys.base_prefix) / "pkgs" / record.stem)) / "info/licenses"
        if not source.is_dir():
            raise RuntimeError(f"Native dependency license missing: {package['name']}")
        shutil.copytree(source, folder / "_internal/licenses/native" / record.stem)
        native_inventory.append({"name": package['name'], "version": package['version'], "license": package.get('license'), "binaries": matches})
    (folder / "_internal/native-dependencies.json").write_text(json.dumps(native_inventory, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "packaging/PORTABLE-README.txt", folder / "使用说明.txt")
    shutil.copy2(resources / "docs/user-guide.html", folder / "使用说明.html")
    shutil.copy2(ROOT / "packaging/RELEASE-NOTES.md", folder / "版本说明.md")
    print("Hashing distribution files...", flush=True)
    files = {str(p.relative_to(folder)).replace("\\", "/"): digest(p) for p in sorted(folder.rglob("*")) if p.is_file()}
    (folder / "FILES-SHA256.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    print("Compressing distribution...", flush=True)
    archive_path = shutil.make_archive(str(output / args.name), "zip", output, args.name)
    summary = {"folder": str(folder), "zip": archive_path, "sha256": digest(Path(archive_path)),
        "zip_bytes": Path(archive_path).stat().st_size, "unpacked_bytes": sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())}
    (output / "build-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
