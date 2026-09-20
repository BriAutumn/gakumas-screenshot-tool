import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_builder', ROOT / 'packaging/build_windows.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PortableTests(unittest.TestCase):
    def test_web_service_window_stops_its_listener(self):
        import tkinter as tk
        sys.path.insert(0, str(ROOT / 'src'))
        from gkshot.desktop import web_window
        real_tk = tk.Tk
        def hidden_window():
            window = real_tk()
            window.withdraw()
            window.after(200, lambda: window.tk.call(window.protocol('WM_DELETE_WINDOW')))
            return window
        with patch('tkinter.Tk', hidden_window), patch('webbrowser.open') as browser:
            self.assertEqual(web_window(port=0), 0)
            self.assertTrue(browser.call_args[0][0].startswith('http://127.0.0.1:'))

    def test_allowlist_excludes_personal_files_and_caches(self):
        for name in ('data/captures/a/raw.png', 'data/memory-library/library.sqlite3', '.env',
                     '.venv/Lib/site-packages/a.py', 'work/report.json', 'tests/fixtures/original-screenshot.png',
                     'runtime/node/node_modules/a.js', 'configs/.env', 'assets/ocr/__pycache__/x.pyc'):
            self.assertFalse(builder.approved_resource(name), name)
        for name in ('assets/ocr/model.onnx', 'configs/capture/pc-memory-v1.json', 'runtime/node/worker.mjs', 'docs/user-guide.html'):
            self.assertTrue(builder.approved_resource(name), name)

    def test_frozen_defaults_separate_resources_and_data(self):
        code = '''
import json,sys
from pathlib import Path
sys.frozen=True
sys._MEIPASS=sys.argv[1]
from gkshot.app_paths import RESOURCE_ROOT, USER_ROOT
from gkshot.catalog import PROJECT_ROOT
from gkshot.capture.store import DEFAULT_CAPTURES
from gkshot.memory_library import DEFAULT_LIBRARY
print(json.dumps([str(x) for x in (RESOURCE_ROOT,PROJECT_ROOT,USER_ROOT,DEFAULT_CAPTURES,DEFAULT_LIBRARY)]))
'''
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), LOCALAPPDATA=str(base / 'user'))
            env.pop('GKTOOL_USER_HOME', None)
            result = subprocess.check_output([sys.executable, '-c', code, str(base / 'app')], env=env, text=True)
            self.assertEqual(json.loads(result), [str(base/'app'), str(base/'app'), str(base/'user/GKTool'),
                str(base/'user/GKTool/data/captures'), str(base/'user/GKTool/data/memory-library')])

    def test_relative_override_rejected(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), GKTOOL_USER_HOME='relative')
        result = subprocess.run([sys.executable, '-c', 'import gkshot.app_paths'], env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_entire_pinned_engine_manifest_is_allowlisted(self):
        manifest = json.loads((ROOT / 'runtime/node/manifest.json').read_text(encoding='utf-8'))
        for name in manifest['files']:
            self.assertTrue(builder.approved_resource('runtime/node/upstream/' + name), name)


if __name__ == '__main__':
    unittest.main()
