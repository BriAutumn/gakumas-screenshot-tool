"""Portable entrypoint; resolve user home before importing application modules."""
import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--user-home', type=Path)
    parser.add_argument('--active-child', action='store_true')
    parser.add_argument('--baseline-exe')
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--plugin-action', choices=['install','uninstall'])
    parser.add_argument('--game-dir', type=Path)
    parser.add_argument('--self-test', type=Path)
    parser.add_argument('--serve-only', action='store_true')
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args()
    if args.user_home: os.environ['GKTOOL_USER_HOME'] = str(args.user_home.resolve())
    from gkshot.app_paths import USER_ROOT, RESOURCE_ROOT
    from gkshot.simulation import atomic_json
    USER_ROOT.mkdir(parents=True, exist_ok=True)
    if args.plugin_action:
        from gkshot.plugin_manager import PluginManager
        try:
            if not args.game_dir: raise ValueError('缺少游戏目录')
            result = getattr(PluginManager(RESOURCE_ROOT / 'assets/plugin', USER_ROOT), args.plugin_action)(args.game_dir)
            result['ok'] = True
        except Exception as error: result = {'ok':False, 'error':str(error)}
        atomic_json(USER_ROOT / 'plugin-action-result.json', result)
        return 0 if result['ok'] else 1
    if args.self_test:
        import tkinter
        from gkshot.catalog import Catalog
        from gkshot.models import Loadout
        from gkshot.adapters.local_engine import LocalSimulator
        from gkshot.plugin_manager import PluginManager
        from gkshot.native_service import NativeService
        from gkshot.data_updates import DataUpdater
        import yaml
        catalog = Catalog()
        PluginManager(RESOURCE_ROOT / 'assets/plugin', USER_ROOT)._load_bundle()
        mapping = json.loads(NativeService(catalog=catalog).mapping.read_text(encoding='utf8'))
        if mapping['provenance']['catalog_revision'] != catalog.manifest['revision']:
            raise ValueError('映射和引擎目录版本不一致')
        tk = tkinter.Tk(); tk.withdraw(); tk.update(); tk.destroy()
        loadout = Loadout.load(RESOURCE_ROOT / 'examples/simulation/original.loadout.json')
        with LocalSimulator() as engine:
            identity = engine.identity()
            result = engine.simulate(loadout, trials=3)
        atomic_json(args.self_test, {'passed':True,'version':json.loads((RESOURCE_ROOT/'configs/product.json').read_text(encoding='utf8'))['version'],
            'engine_revision':identity['revision'],'node':identity['node'],'scores':result['scores'],
            'user_home':str(USER_ROOT),'resources':str(RESOURCE_ROOT),
            'data_update':DataUpdater().status(),'yaml_version':yaml.__version__})
        return 0
    if args.serve_only:
        from gkshot.simulation_web import SimulationServer
        with SimulationServer(('127.0.0.1', args.port)) as server:
            atomic_json(USER_ROOT / 'service.json', {'origin':server.origin,'token':server.token,'pid':os.getpid()})
            server.serve_forever()
        return 0
    if getattr(sys, 'frozen', False) and not args.active_child and not args.baseline:
        from gkshot.program_updates import _read_pointer, _verify_staged
        pointer = _read_pointer(USER_ROOT / 'program-active.json')
        active = pointer and pointer.get('active')
        if active:
            directory = Path(active['directory']).resolve()
            updates = (USER_ROOT / 'program-updates').resolve()
            if directory == updates or not directory.is_relative_to(updates): raise ValueError('更新目录不合法')
            verified = _verify_staged(directory)
            if verified['manifest_sha256'] != active['manifest_sha256']: raise ValueError('活动版本校验失败')
            executable = directory / verified['manifest']['entrypoint']
            if executable.resolve() != Path(sys.executable).resolve():
                subprocess.Popen([str(executable),'--active-child','--user-home',str(USER_ROOT),
                    '--baseline-exe', str(Path(sys.executable).resolve())], creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                return 0
    from gkshot.launcher import launch
    launch(args.baseline_exe)
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as error:
        if '--self-test' in sys.argv:
            try:
                report=Path(sys.argv[sys.argv.index('--self-test')+1]); report.parent.mkdir(parents=True,exist_ok=True)
                report.write_text(json.dumps({'passed':False,'error':str(error)},ensure_ascii=False),encoding='utf8')
            except Exception: pass
            sys.exit(1)
        try:
            from gkshot.app_paths import USER_ROOT
            USER_ROOT.mkdir(parents=True, exist_ok=True)
            (USER_ROOT/'last-error.txt').write_text(str(error), encoding='utf8')
        except Exception: pass
        ctypes.windll.user32.MessageBoxW(None, '启动失败：'+str(error)+'\n\n可使用 GKMemoryTool.exe --baseline 打开原版恢复。', '回忆工具', 0x10)
        sys.exit(1)
