"""Exercise an unpacked distribution with no developer runtime on PATH."""
import argparse
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    home = args.scratch.resolve()
    home.mkdir(parents=True, exist_ok=False)
    exe, = folder.glob("*.exe")
    manifest = json.loads((folder / "FILES-SHA256.json").read_text())
    def verify_integrity():
        actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        assert actual == set(manifest) | {'FILES-SHA256.json'}, 'Unexpected or missing distribution files'
        for name, expected in manifest.items():
            with (folder / name).open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, name
    verify_integrity()
    guide = (folder / '使用说明.html').read_text(encoding='utf-8')
    assert guide.count('data:image/png;base64,') >= 3, 'Illustrated guide is incomplete'
    assert (folder / '版本说明.md').is_file()
    assert not list(folder.rglob("*.sqlite3"))
    assert not any(part in (".venv", "captures", "__pycache__", ".git") for p in folder.rglob("*") for part in p.relative_to(folder).parts)
    env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME", "NODE_PATH", "NODE_OPTIONS", "CONDA_PREFIX", "TCL_LIBRARY", "TK_LIBRARY")}
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    env["GKTOOL_USER_HOME"] = str(home)
    env["GKTOOL_NO_DIALOG"] = "1"
    begin = time.monotonic()
    subprocess.run([str(exe), "diagnose"], env=env, cwd=home, check=True, timeout=120)
    report = json.loads((home / "logs/diagnostics.json").read_text(encoding="utf-8"))
    assert report["ok"], report
    assert report["frozen"] is True
    assert report["checks"]["local_simulation"]["detail"]["result"]["scores"] == [94042]
    diagnostic_seconds = time.monotonic() - begin
    subprocess.run([str(exe), "memory-list"], env=env, cwd=home, check=True, timeout=20)
    assert (home / "data/memory-library/library.sqlite3").is_file()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    process = subprocess.Popen([str(exe), "simulation", "web", "--no-browser", "--port", str(port)], env=env, cwd=home)
    def request(path, body=None, token=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("POST" if body is not None else "GET", path,
                body=json.dumps(body) if body is not None else None, headers={"X-GK-Token": token} if token else {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                status, page = request("/")
                assert status == 200
                break
            except ConnectionError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise
                time.sleep(.2)
        token = re.search(rb'name="gk-token" content="([^"]+)"', page)[1].decode()
        status, raw = request('/api/season-progress', token=token)
        assert status == 200, raw
        assert len(json.loads(raw)['stages']) == 3, raw
        loadout = json.loads((folder / "_internal/examples/simulation/original.loadout.json").read_text())
        status, raw = request('/api/run', {"candidates": [{"name": "portable-smoke", "loadout": loadout}], "trials": 3, "seed": 610397104}, token)
        assert status == 202, raw
        job_id = json.loads(raw)['job_id']
        deadline = time.monotonic() + 30
        while True:
            status, raw = request('/api/jobs/' + job_id, token=token)
            assert status == 200, raw
            job = json.loads(raw)
            if job['state'] != 'running':
                break
            assert time.monotonic() < deadline, job
            time.sleep(.1)
        assert job['state'] == 'complete', job
        assert job['report']['results'][0]['simulation']['scores'] == [94042, 24119, 85058], job
    finally:
        process.terminate()
        process.wait(timeout=10)
    verify_integrity()
    summary = {"ok": True, "diagnostic_seconds": round(diagnostic_seconds, 3), "web_scores": [94042, 24119, 85058],
               "folder": str(folder), "home": str(home), "limitation": "PATH isolation on developer Windows; not a clean VM or firewall test"}
    (home / 'verification.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
