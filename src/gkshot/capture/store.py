"""Atomic session ledger and immutable, checksummed screenshots."""
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from gkshot.catalog import PROJECT_ROOT
from gkshot.paths import io_path

from gkshot.app_paths import USER_ROOT
DEFAULT_CAPTURES = USER_ROOT / "data/captures"


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path = io_path(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CaptureSession:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "session.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        if self.data.get("schema_version") != 1:
            raise ValueError("不支持的采集会话版本。")

    @classmethod
    def create(cls, profile, parent=DEFAULT_CAPTURES):
        session_id = uuid4().hex
        root = Path(parent).resolve() / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + session_id[:8])
        root.mkdir(parents=True)
        atomic_json(root / "session.json", {"schema_version": 1, "session_id": session_id,
            "created_at": now(), "updated_at": now(), "profile": profile, "status": "new",
            "pages": [], "entries": [], "events": [], "last_error": None})
        return cls(root)

    def save(self):
        self.data["updated_at"] = now()
        atomic_json(self.path, self.data)

    def log(self, message):
        with (self.root / 'capture.log').open('a',encoding='utf-8') as stream:
            stream.write(f'[{now()}] {message}\n')

    def event(self, state, message):
        self.data["status"] = state
        self.data["events"].append({"time": now(), "state": state, "message": message})
        self.save()

    def image(self, image, role, *, entry=None, slot=None, metadata=None):
        # UUID names permit recapture without modifying any previous raw screenshot.
        relative = Path("raw") / (entry["capture_id"] if entry else "navigation") / f"{role}-{uuid4().hex[:12]}.png"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            image.save(stream, format="PNG")
            stream.flush()
            os.fsync(stream.fileno())
        record = {"path": relative.as_posix(), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                  "role": role, "slot": slot, "size": list(image.size), "captured_at": now(), "metadata": metadata or {}}
        if entry is not None:
            entry.setdefault("images", []).append(record)
        else:
            # Navigation screenshots need their metadata too, especially the
            # anchor and overlap counts used to diagnose resume/pagination.
            self.data.setdefault("navigation_images", []).append(record)
        self.save()
        return record

    def source(self, record):
        path = (self.root / record["path"]).resolve()
        if not path.is_relative_to(self.root / "raw"):
            raise ValueError("截图路径不属于当前采集会话。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("截图校验失败：" + record["path"])
        return path

    def entry(self, page_index, cell_index):
        found = next((e for e in self.data["entries"] if e["page_index"] == page_index and e["cell_index"] == cell_index), None)
        if found:
            return found
        entry = {"capture_id": uuid4().hex, "ordinal": len(self.data["entries"]) + 1,
                 "page_index": page_index, "cell_index": cell_index, "status": "pending", "attempt": 0,
                 "images": [], "issues": [], "attempts": []}
        self.data["entries"].append(entry)
        self.save()
        return entry

    def export_manifest(self):
        groups = []
        for entry in self.data["entries"]:
            if entry["status"] not in ("captured", "needs-recapture"):
                continue
            images = [r for r in entry["images"] if r["metadata"].get("attempt") == entry["attempt"]]
            details = [r for r in images if r["role"] in ("overview-top", "overview-cards")]
            if not details:
                continue
            groups.append({"capture_id": f"{self.data['session_id']}:{entry['capture_id']}", "ordinal": entry["ordinal"],
                           "capture_status": entry["status"], "details": [r["path"] for r in details],
                           "card_details": [{"path": r["path"], "slot": r["slot"]} for r in images if r["role"] == "card-tooltip"],
                           "issues": entry["issues"]})
        atomic_json(self.root / "batch.json", {"schema_version": 1, "session_id": self.data["session_id"], "memories": groups})
        atomic_json(self.root / "skipped-captures.json", {
            "schema_version": 1, "session_id": self.data["session_id"],
            "memories": [{"ordinal": e["ordinal"], "capture_id": e["capture_id"],
                          "attempt": e["attempt"], "issues": e["issues"],
                          "images": [r["path"] for r in e["images"] if r["metadata"].get("attempt") == e["attempt"]]}
                         for e in self.data["entries"] if e["status"] == "skipped-incomplete"]})
        return self.root / "batch.json"


@contextmanager
def exclusive_collector_lock(path=None):
    """One local collector can inject input at a time, even across separate sessions."""
    import msvcrt
    path = Path(path or DEFAULT_CAPTURES / ".collector.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("已有一个采集器正在运行。") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
