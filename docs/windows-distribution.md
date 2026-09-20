# Windows 便携发行（包装集成预览）

本任务基于 e7a71d3 的独立 worktree，不修改 C:/gktool，也不携带其生产数据。最终发行必须合入识图与模拟任务的最新提交后重建、验收。本包不代表 ≥90% 自动识别验收，也不能用含模型补录的 447 份可用库宣传 100% 自动识别。

## 方案与入口

采用 PyInstaller **onedir + ZIP**，Windows x64 原生构建。用户完整解压、双击 `GKTool-preview.exe`；无需安装 Python/Node，不调用云 OCR 或任何 API。内置 Python 3.13、Tk/Tcl、NumPy、Pillow、OpenCV、ONNX Runtime、RapidOCR 默认模型、本项目日文模型、固定识别资产与配置、Node 24.16.0、原 JS 引擎及网页资源。默认浏览器为 Windows 已安装浏览器。

目录模式启动不必每次释放数百 MB 模型，故暂不采用单文件 exe；符合 [PyInstaller 官方说明](https://pyinstaller.org/en/stable/operating-mode.html)。首轮原型约 178 MiB ZIP / 381 MiB 解压，精确大小以构建产物 `build-summary.json` 为准。未做签名和安装器，因此仍为预览包。

统一菜单复用原有 `gkshot.cli` / `simulation_cli`，不重写识别与模拟。网页服务有独立停止窗口，绑定 127.0.0.1:8765；关闭浏览器不会停止服务。端口占用会清楚报错，不自动连接未知服务。各工具窗口独立运行。开发 CLI 仍可用：

```powershell
.venv/Scripts/python.exe packaging/entry.py
.venv/Scripts/python.exe packaging/entry.py diagnose
```

发行 exe 支持同样参数，如 `GKTool-preview.exe capture-process <会话目录>`、`GKTool-preview.exe simulation compare <loadout.json>`。windowed exe 的输出写入用户日志，自动化调用需等待退出并读取输出文件；用绝对输入输出路径。

## 路径接口与合并点

`src/gkshot/app_paths.py` 新增 `RESOURCE_ROOT` / `USER_ROOT`。源码默认仍在项目目录读写；冻结发行从 `sys._MEIPASS` 读资源，用户数据默认 `%LOCALAPPDATA%/GKTool`。可用绝对路径环境变量 `GKTOOL_USER_HOME` 覆盖，便于迁移、测试与独立配置。不自动搬迁旧数据、不修改已有库。

必须保留的小改动：

- `catalog.py` 的 `PROJECT_ROOT` 由 `RESOURCE_ROOT` 提供。
- `capture/store.py`、`capture/gui.py`、`memory_library.py` 的可写路径使用 `USER_ROOT`。
- `simulation_web.py` 的 data/work 路径及 relative_to 使用 `USER_ROOT`；静态页面/模型/目录仍用 `PROJECT_ROOT`。
- `simulation_gui.py` 的缓存使用 `USER_ROOT`。
- `gui.py`、`memory_gui.py` 仅在示例截图实际存在时显示私人测试示例按钮，发行不带这些截图。

新增 `desktop.py` 在启动发行时切换工作目录到用户目录，并将私有 Node 放在 PATH 第一项。识别算法、原引擎及网页文件未改。网页服务控制窗口依赖 `SimulationServer.guard/jobs/searches/busy()`；模拟任务调整接口时需一起验收。

用户备份应包含整个用户目录（原图、台账、处理结果、数据库）。更新只替换程序目录；不删除用户目录。不把旧个人库放进 ZIP。

## 可重复构建

在 Windows x64 上创建专用 Python 3.13 venv。构建需要联网获取依赖；产品运行离线。不能把已有 .venv 直接作为发行目录。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r packaging/requirements-build.txt
New-Item -ItemType Directory -Force build/downloads
Invoke-WebRequest https://nodejs.org/dist/v24.16.0/node-v24.16.0-win-x64.zip -OutFile build/downloads/node-v24.16.0-win-x64.zip
.venv/Scripts/python.exe packaging/build_windows.py --node-zip build/downloads/node-v24.16.0-win-x64.zip
```

Node 官方 ZIP 的 SHA-256 固定在脚本，下载后必须匹配。构建输出 `dist/<时间戳>/`，保留目录、ZIP、ZIP 哈希和大小。资源只从 Git tracked allowlist 复制，不复制整个仓库；禁止 captures/data/work/.venv/cache/node_modules。`build-info.json` 记录提交、dirty 标志与预览状态；正式版本要从审查后的干净提交构建。

`FILES-SHA256.json` 覆盖发行文件（清单自身除外）。它用于损坏排查，不是签名或防篡改信任机制。

## 依赖与许可证

`requirements-recognition.txt` 固定运行依赖，`packaging/requirements-build.txt` 固定打包工具。产物中的 `dependencies.json` 保存所有构建环境发行包版本及许可证文本路径；包括部分仅构建使用的包，不能把它当精确的运行时 SBOM。`native-dependencies.json` 记录从 Conda Python 收集的原生 DLL 对应包、版本及许可，`licenses/native/` 收录缓存中的许可文件，缺失时构建失败。

上游原引擎 BSD、识别目录 BSD、RapidOCR 与日文模型许可、Node 的完整第三方许可，以及 Python 许可一并保留。许可证来源另见 `THIRD_PARTY.md`。正式向公众分发前仍需核对游戏图标/模型的再分发授权、Python 来源和所有 DLL 的许可适用性；本仓库代码许可不自动覆盖游戏素材。

## 验证与最终验收门槛

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_portable.py
.venv/Scripts/python.exe packaging/verify_portable.py dist/<时间戳>/GKTool-preview --scratch 'work/验证数据 新目录'
```

验证脚本校验发行文件哈希、无生产库、仅保留 Windows System32 的 PATH、移除 Python/Node/Tcl 开发环境变量。实际运行冻结 exe 的诊断：加载日文 OCR + ONNX CPU 模型并推断空白图、Tk、SQLite、模拟一次固定种子得到 94042。再创建空记忆库，启动 HTTP 服务，经令牌 API 比较三次固定成绩 94042/24119/85058。原样记录结果和耗时；不是 OCR 准确率测试。

还需要在 Windows 10/11 x64 干净 VM 逐项确认：

1. 未安装 Python/Node，禁用外网，普通用户、中文用户名、中文空格解压路径，运行统一菜单和诊断。
2. 程序目录只读，数据和缓存只写 LocalAppData；移动/升级程序目录后仍找到用户数据。浏览器历史依赖其 localStorage，文件库独立备份。
3. 真游戏采集权限不一致时有可理解提示；普通/管理员场景、原图落盘、暂停/续采、关游戏离线识别到新库、库选择与主副模拟串联。不能拿当前开发机测试代替这一条。
4. 将主识图任务最新规则及模拟任务最新界面合并后，重跑回归与真实全流程；自动识别率单独按纯离线证据计量。
5. 损坏/缺失模型、Node、端口占用、不可写数据目录、磁盘不足，均有可定位错误；停止模拟服务时无孤立 Node，取消搜索保留检查点。
6. 重新扫描发行内容无个人截图/数据库/缓存/凭证，复核完整许可证与素材分发权，再考虑签名和安装包。

当前 PATH 隔离验证仍在开发机进行，不能证明干净 VM 无额外系统依赖，也没有断网/游戏操作验收。
