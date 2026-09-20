"""Vendor an archived, pinned upstream snapshot without npm or source rewriting."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

REVISION = "7153b0775c654d58c7b8a17b3a598322a35d0b2a"
ARCHIVE_SHA256 = "014086133ab45452998ef91f824ddb1024e00b55f886125a04455261518787b7"
ROOT = Path(__file__).resolve().parents[2]
DEST = Path(__file__).resolve().parent / "upstream"

def install(archive):
    archive_hash = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    if archive_hash != ARCHIVE_SHA256:
        raise ValueError('Archive hash differs from the verified upstream snapshot')
    files = {}
    with zipfile.ZipFile(archive) as bundle:
        prefix = f"gakumas-tools-{REVISION}/"
        for member in bundle.namelist():
            if not member.startswith(prefix) or member.endswith("/"):
                continue
            relative = member[len(prefix):]
            if not (relative.startswith(("packages/gakumas-engine/", "packages/gakumas-data/"))
                    or relative in ("LICENSE", "scripts/extensionless-loader.mjs", "tests/lib.mjs",
                                    "tests/run.mjs", "tests/suite.jsonl", "gakumas-tools/utils/stamina.js",
                                    "gakumas-tools/contexts/LoadoutContext.js")):
                continue
            target = DEST / relative
            if not target.resolve().is_relative_to(DEST.resolve()):
                raise ValueError("Unsafe archive path")
            data = bundle.read(member)
            if target.exists() and target.read_bytes() != data:
                raise ValueError(f"Refusing to overwrite modified upstream file: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[relative] = hashlib.sha256(data).hexdigest()
    if not files:
        raise ValueError("Archive does not contain the pinned revision")
    manifest = {"repository": "https://github.com/surisuririsu/gakumas-tools",
                "revision": REVISION, "source_archive_sha256": archive_hash,
                "changes": "None; upstream extensionless loader used unchanged", "files": files}
    (DEST.parent / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Installed {len(files)} original files")

if __name__ == "__main__":
    install(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "work/catalog-candidates/upstream.zip")
