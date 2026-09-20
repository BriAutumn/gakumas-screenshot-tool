"""Pinned original JavaScript engine in a cancellable Node process."""
import hashlib
import json
import math
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from gkshot.catalog import PROJECT_ROOT
from gkshot.models import Loadout


class SimulationError(RuntimeError):
    pass


class SimulationCancelled(SimulationError):
    pass


class LocalSimulator:
    def __init__(self, node=None, runtime=None, *, persistent=False):
        self.persistent = persistent
        self._worker = None
        self._identity = None
        self.node = node or shutil.which("node")
        self.runtime = Path(runtime or PROJECT_ROOT / "runtime/node").resolve()
        if not self.node:
            raise SimulationError("未找到 Node.js，请安装 Node.js 24 LTS。")

    def identity(self):
        try:
            manifest = json.loads((self.runtime / "manifest.json").read_text(encoding="utf-8"))
            hashes = {}
            for name, expected in manifest["files"].items():
                actual = hashlib.sha256((self.runtime / "upstream" / name).read_bytes()).hexdigest()
                if actual != expected:
                    raise SimulationError(f"引擎文件校验失败：{name}")
                hashes[name] = actual
            for name in ("worker.mjs", "manifest.json"):
                hashes[name] = hashlib.sha256((self.runtime / name).read_bytes()).hexdigest()
            version = subprocess.check_output([self.node, "--version"], text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
            return {"revision": manifest["revision"], "files": hashes, "node": version, "protocol": 1}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise SimulationError(f"本地引擎未安装或损坏：{error}") from error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._worker is None:
            return
        process, _, _, threads = self._worker
        if process.poll() is None:
            process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        self._worker = None
        self._identity = None

    def _start(self):
        if self._worker is not None:
            return self._worker
        self._identity = self.identity()
        command = [self.node, "--no-warnings", "--loader",
                   (self.runtime / "upstream/scripts/extensionless-loader.mjs").as_uri(),
                   str(self.runtime / "worker.mjs")]
        messages, errors = queue.Queue(), []
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read_stdout():
            try:
                for line in process.stdout:
                    messages.put(line)
            finally:
                messages.put(None)
        def read_stderr():
            for line in process.stderr:
                errors.append(line)
                if len(errors) > 30:
                    del errors[0]
        threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for thread in threads:
            thread.start()
        self._worker = process, messages, errors, threads
        return self._worker

    def simulate(self, loadout: Loadout, *, trials=100, seed=610397104,
                 timeout=300, cancel=None, progress=None):
        loadout = Loadout.from_dict(loadout.to_dict())
        if type(trials) is not int or not 1 <= trials <= 100000:
            raise ValueError("trials must be 1..100000")
        if type(seed) is not int or not 0 <= seed <= 0xffffffff:
            raise ValueError("seed must be uint32")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if cancel and cancel.is_set():
            raise SimulationCancelled("模拟已取消")
        process, messages, errors, threads = self._start()
        identity = self._identity
        request_id = uuid4().hex
        succeeded = False
        started = time.monotonic()
        try:
            process.stdin.write(json.dumps({"protocol_version": 1, "request_id": request_id,
                "loadout": loadout.to_dict(), "trials": trials, "seed": seed}) + "\n")
            process.stdin.flush()
            if not self.persistent:
                process.stdin.close()
            while True:
                if cancel and cancel.is_set():
                    raise SimulationCancelled("模拟已取消")
                if time.monotonic() - started > timeout:
                    raise SimulationError(f"模拟超过 {timeout} 秒，已终止")
                try:
                    line = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    raise SimulationError("Node worker 意外退出：" + "".join(errors)[-3000:])
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise SimulationError("Node worker 返回无效 JSON") from error
                if event.get("request_id") != request_id or event.get("protocol_version") != 1:
                    raise SimulationError("Node worker 协议不匹配")
                if event["type"] == "error":
                    raise SimulationError(event["error"]["message"])
                if event["type"] == "progress":
                    if progress:
                        progress(event["completed"], trials)
                elif event["type"] == "result":
                    scores = event.get("scores")
                    if (not isinstance(scores, list) or len(scores) != trials or
                        any(type(x) not in (int, float) or not math.isfinite(x) for x in scores) or
                        event.get("engine_revision") != identity["revision"] or
                        event.get("data_revision") != identity["revision"]):
                        raise SimulationError("引擎结果数量、数值或版本不匹配")
                    if not self.persistent:
                        process.wait(timeout=5)
                        if process.returncode != 0:
                            raise SimulationError("Node worker 非正常退出")
                    succeeded = True
                    return event
                else:
                    raise SimulationError("未知 worker 消息")
        finally:
            if not self.persistent or not succeeded:
                self.close()
