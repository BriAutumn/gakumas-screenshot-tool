"""Build a curated portable onedir, compatible with the unified updater."""
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64','x86_64'):
        raise RuntimeError('请在 Windows x64 构建')
    config = json.loads((ROOT/'configs/product.json').read_text(encoding='utf8'))
    version = config['version']
    plugin = ROOT/'assets/plugin'
    expected = json.loads((plugin/'plugin.json').read_text(encoding='utf8'))
    if digest(plugin/'version.dll') != expected['sha256']: raise ValueError('插件哈希不符')
    for name, expected_hash in expected.get('files', {}).items():
        if name not in ('version.dll', 'winhttp.dll') or digest(plugin/name) != expected_hash:
            raise ValueError('插件共存入口校验失败：'+name)
    manifest = json.loads((ROOT/'runtime/node/manifest.json').read_text(encoding='utf8'))
    for name, expected in manifest['files'].items():
        if digest(ROOT/'runtime/node/upstream'/name) != expected: raise ValueError('引擎校验失败：'+name)
    mapping = json.loads((ROOT/'assets/native/mapping.json').read_text(encoding='utf8'))
    if mapping['provenance']['catalog_revision'] != manifest['revision']: raise ValueError('原生映射版本不符')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    build = ROOT/'build'/stamp; output=ROOT/'dist'/stamp
    build.mkdir(parents=True); output.mkdir(parents=True)
    resources = build/'resources'; resources.mkdir()
    for name in ('assets','configs','runtime','src/gkshot/web','examples'):
        source = ROOT/name
        for file in source.rglob('*'):
            if file.is_symlink(): raise ValueError('构建资源包含符号链接')
            if not file.is_file(): continue
            relative = file.relative_to(ROOT)
            if any(p in ('.git','node_modules','__pycache__','cache') for p in relative.parts):
                raise ValueError('资源含不允许的路径：'+str(relative))
            target=resources/relative; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(file,target)
    licenses = resources/'licenses'; licenses.mkdir()
    python_license=Path(sys.base_prefix)/'LICENSE_PYTHON.txt'
    if not python_license.is_file(): python_license=Path(sys.base_prefix)/'LICENSE.txt'
    shutil.copy2(python_license, licenses/'Python.txt')
    shutil.copy2(ROOT/'runtime/node/upstream/LICENSE', licenses/'gakumas-tools-BSD-3-Clause.txt')
    shutil.copy2(ROOT/'runtime/node-bin/LICENSE', licenses/'Node.txt')
    shutil.copy2(ROOT/'THIRD_PARTY.md', resources/'THIRD_PARTY.md')
    yaml_package = distribution('PyYAML')
    yaml_license = next((yaml_package.locate_file(p) for p in yaml_package.files
                         if p.as_posix().endswith('/licenses/LICENSE')), None)
    if yaml_license is None or not yaml_license.is_file():
        raise RuntimeError('PyYAML 许可文件缺失')
    shutil.copy2(yaml_license, licenses/'PyYAML-MIT.txt')
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--onedir','--windowed','--noupx',
        '--name','GKMemoryTool','--icon',str(ROOT/'assets/app-icon.ico'),
        '--paths',str(ROOT/'src'),'--distpath',str(output),
        '--workpath',str(build/'pyinstaller'),'--specpath',str(build),'--add-data',str(resources)+';.',
        str(ROOT/'packaging/entry.py')]
    subprocess.run(command,check=True,cwd=ROOT)
    folder=output/'GKMemoryTool'
    native_names={p.name.lower() for p in (folder/'_internal').rglob('*') if p.suffix.lower() in ('.dll','.pyd')}
    inventory=[]
    for record in (Path(sys.base_prefix)/'conda-meta').glob('*.json'):
        package=json.loads(record.read_text(encoding='utf8'))
        matches=sorted({Path(n).name for n in package.get('files',[]) if Path(n).name.lower() in native_names})
        if not matches: continue
        source=Path(package.get('link',{}).get('source',Path(sys.base_prefix)/'pkgs'/record.stem))/'info/licenses'
        if not source.is_dir(): raise RuntimeError('本地依赖许可缺失：'+package['name'])
        shutil.copytree(source,folder/'_internal/licenses/native'/record.stem)
        inventory.append({'name':package['name'],'version':package['version'],'license':package.get('license'),'binaries':matches})
    (folder/'_internal/native-dependencies.json').write_text(json.dumps(inventory,indent=2),encoding='utf8')
    shutil.copy2(ROOT/'README.md',folder/'使用说明.md')
    shutil.copy2(ROOT/'tools/diagnostics/GKMemoryExport-diagnostic-v3.cmd',
                 folder/'GKMemoryExport-diagnostic-v3.cmd')
    shutil.copy2(ROOT/'THIRD_PARTY.md',folder/'THIRD_PARTY.md')
    (folder/'恢复原版.cmd').write_text('@echo off\r\n"%~dp0GKMemoryTool.exe" --baseline\r\n',encoding='ascii')
    files={p.relative_to(folder).as_posix():digest(p) for p in sorted(folder.rglob('*')) if p.is_file()}
    release={'schema_version':1,'product':'gk-memory-tool','version':version,'entrypoint':'GKMemoryTool.exe',
        'components':{'plugin':json.loads((plugin/'plugin.json').read_text(encoding='utf8'))['version'],
            'engine_revision':manifest['revision'],'mapping_sha256':digest(ROOT/'assets/native/mapping.json')},'files':files}
    (folder/'release.json').write_text(json.dumps(release,ensure_ascii=False,indent=2),encoding='utf8')
    archive=output/f'GKMemoryTool-{version}-windows-x64.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(folder.rglob('*')):
            if p.is_file(): z.write(p,p.relative_to(folder).as_posix())
    (output/(archive.name+'.sha256')).write_text(digest(archive)+'  '+archive.name+'\n',encoding='ascii')
    result={'folder':str(folder),'zip':str(archive),'sha256':digest(archive),'bytes':archive.stat().st_size}
    (output/'build-summary.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__': main()
