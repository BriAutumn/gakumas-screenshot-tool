# 本地原生 GKTools 引擎（runtime/node）

`runtime/node/` 保存随工具附带的固定上游引擎与目录，供 Python 通过行 JSON 协议以子进程方式调用。

## 来源与版本

- 固定上游提交 `9ff4610011feb395e363d86d7c961e4da68b6deb`，仓库 [surisuririsu/gakumas-tools](https://github.com/surisuririsu/gakumas-tools)（现由 [gakumas-tools/gakumas-tools](https://github.com/gakumas-tools/gakumas-tools) 维护）。
- `manifest.json` 的 `revision` 记录该提交，`files` 逐文件记录 SHA-256；`upstream/` 下的上游文件由 `manifest.json` 列出并逐字节保留。
- 上游 BSD-3-Clause 许可原文保存在 `upstream/LICENSE`。

## 目录内容

- `manifest.json`、`package.json`、`worker.mjs`、`reference.mjs` 为本地文件。
- `upstream/` 为上游引擎与数据（`packages/gakumas-engine`、`packages/gakumas-data`、`gakumas-tools/` 片段、`scripts/extensionless-loader.mjs`、`tests/`）。
- 引擎是 JavaScript ESM，只依赖同仓库的 `gakumas-data`；没有 npm 包下载、TS 编译、网页构建或联网运行依赖。省略扩展名与 JSON 导入由上游 `scripts/extensionless-loader.mjs` 处理。

## Node 运行时

环境为 Node.js 24（固定 24.16.0）。为避免提交约 92 MB 的二进制，`runtime/node-bin/node.exe` 不纳入 Git；`runtime/node-bin/LICENSE` 是随仓库保留的 Node 许可文件。

在仓库根目录运行：

```text
python tools/prepare_node.py
```

它会从固定官方地址 `https://nodejs.org/dist/v24.16.0/node-v24.16.0-win-x64.zip` 获取归档（或用 `--archive PATH` 指定已下载的官方压缩包），先校验整包 SHA-256，再只解出成员 `node-v24.16.0-win-x64/node.exe` 与 `node-v24.16.0-win-x64/LICENSE` 到 `runtime/node-bin`。已存在且校验一致时为空操作，不会联网；已存在但哈希不符时会拒绝覆盖。

## 入口与协议

- Python 侧入口是 `packaging/entry.py`（打包后为 `GKMemoryTool.exe`），它调用 `gkshot.adapters.local_engine.LocalSimulator` 启动本引擎。
- `worker.mjs` 是实际工作进程：stdin/stdout 逐行 JSON，日志只写 stderr。请求包含 `protocol_version / request_id / loadout / trials / seed`；响应依次为 `type=progress`，最后是 `type=result` 或 `type=error`。`worker.mjs` 启动时会按 `manifest.json` 校验全部上游文件哈希。
- `reference.mjs` 使用上游 URL 解析器与引擎入口直接调用引擎，用作独立对照（worker / reference 冒烟对比），不导入 worker 的映射逻辑。
- Python 每候选使用独立或持久化的进程，检查版本与文件完整性；取消或超时会终止 worker，不缓存半成品。

## 数据同步

引擎、目录、图标与原生映射由 `gkshot/data_updates.py` 直接从公开上游提交同步：拉取显式修订、校验 Git blob 校验和与大小、构建原生映射，并通过上游回归测试与 worker/reference 冒烟对比后才原子激活到 `runtime/releases/<revision>`；运行进程在生命周期内固定使用自己启动时的版本。同步不需要重新打包 EXE，也不再使用旧版 `install.py`。

## 校验

```text
node --no-warnings --loader ./runtime/node/upstream/scripts/extensionless-loader.mjs ./runtime/node/upstream/tests/run.mjs
```

上游 `tests/` 为冻结样例，不需要私有素材即可运行。
