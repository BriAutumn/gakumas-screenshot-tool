# 开发说明

本文件面向维护者，说明如何搭建开发环境、准备 Node 运行时、运行工具、测试与构建 Windows 便携包。产品使用说明见仓库根目录 [README.md](../README.md)。

## 环境要求

- Python 3.13（`pyproject.toml` 要求 `>=3.11`，开发以 3.13 为准）。
- 准备 Node 运行时时可访问 `nodejs.org`，或提前下载官方归档后离线使用。
- Windows x64 用于构建与最终验证。

## 安装与运行

在仓库根目录：

```text
pip install -e .                      # 安装项目与依赖
python tools/prepare_node.py          # 准备固定的 Node 运行时（node.exe 不纳入 Git）
python packaging/entry.py             # 运行工具
python -m unittest discover -s tests  # 运行测试
```

`packaging/entry.py` 在非打包运行时会先把 `src/` 加入 `sys.path`，可用 `--user-home` 指定数据目录、`--baseline` 绕过已激活的更新。

## Node 运行时

`tools/prepare_node.py` 只从固定官方地址获取 Node.js 24.16.0 win-x64 归档（或用 `--archive PATH` 指定已下载的同名官方压缩包），先校验整包 SHA-256，再只解出 `node-v24.16.0-win-x64/node.exe` 与 `node-v24.16.0-win-x64/LICENSE` 到 `runtime/node-bin`；已存在且校验一致时为空操作，不会联网，哈希不符时会拒绝覆盖。`runtime/node-bin/node.exe` 因体积原因不纳入 Git，`runtime/node-bin/LICENSE` 随仓库保留。

## 测试

```text
python -m unittest discover -s tests
```

测试使用仓库内的冻结样例，不需要任何私有素材、账号数据或真实网络；需要网络的路径通过注入假实现来校验更新、同步与导入逻辑。

## 目录约定

- `src/gkshot/` 为应用代码；`packaging/` 为入口与构建脚本；`runtime/node/` 为随包附带的固定上游引擎与数据。
- `assets/recognition` 仍是程序正在使用的卡片目录与图标目录（活动资源），不是旧版遗留，请勿删除或改名。
- `runtime/node/upstream/` 下的文件按 `runtime/node/manifest.json` 逐文件校验，手工替换会破坏校验，应通过同步流程更新；细节见 [runtime/node/README.md](../runtime/node/README.md)。
- 本机回忆与账号数据只在本机处理，不进入仓库、分发包或测试夹具。

## Windows 构建

```text
pip install pyinstaller==6.20.0
python packaging/build_windows.py     # Windows x64，PyInstaller 6.20
```

构建脚本会校验固定上游引擎、插件哈希与原生映射版本，收集资源与许可文件，打包为 onedir 便携目录并生成 `release.json` 供更新器使用；构建前请先运行 `python tools/prepare_node.py`。

## 原生插件

- 已编译的插件二进制随仓库附带（`assets/plugin/`），版本与 SHA-256 记录在 `assets/plugin/plugin.json`。
- 可选源码重建见 `tools/native-memory-export/build.cmd`，需要 Visual Studio 2022 Build Tools；重新编译后按需更新 `plugin.json` 中的版本与哈希，使其与新二进制一致。

## 数据来源

模拟引擎、卡片 / 道具 / 偶像 / 场地 / 自定义目录与图标来自 [surisuririsu/gakumas-tools](https://github.com/surisuririsu/gakumas-tools)，原生 ID 映射依据公开的 [vertesan/gakumasu-diff](https://github.com/vertesan/gakumasu-diff) 生成。完整来源与许可说明见 [THIRD_PARTY.md](../THIRD_PARTY.md)。
