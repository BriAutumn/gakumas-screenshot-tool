# 学マス截图助手 · GKTool

Windows 本地工具：采集游戏原图、离线识别回忆、管理回忆库，并进行单编成模拟与候选比较。

**[下载 Windows x64 先行版](https://github.com/BriAutumn/gakumas-screenshot-tool/releases/tag/2026.09.20-preview.1)** · [图文使用说明](docs/user-guide.html)

下载 ZIP 后完整解压，双击包内 exe。图文使用说明也在发行包根目录，浏览器打开即可离线阅读。普通使用不需要安装 Python、Node.js，也不需要 ChatGPT、DeepSeek 或任何 API 密钥。

## 当前版本

`2026.09.20-preview.1` 包含四步工作台、采集与离线识别页签、回忆库查找/筛选、单编成模拟与候选比较，以及 40 张卡的详情识别规则。

识别尚未覆盖所有情况：当前已处理的 403 份开发样本中，279 份完整正确（约 69.2%），124 份仍有未确定项；另有 44 份未完成本轮验证。该比例不是未知截图准确率，也不是整批最终结果，**未达到或验收 90% 自动识别目标**。不确定结果会保留供核对。

发行版已检查解压运行、OCR/ONNX/Tk/SQLite、空库、网页三舞台接口及模拟调用。尚未签名，也未完成干净 Windows 虚拟机和真游戏全流程验证。本版本标为预发布。

## 从源码运行

Windows x64，已验证 Python 3.13。运行 `setup.cmd` 安装本地依赖，然后运行 `launch-desktop.cmd`。引擎安装与维护说明见 [runtime/node/README.md](runtime/node/README.md)，打包说明见 [docs/windows-distribution.md](docs/windows-distribution.md)。首次安装依赖需要联网，正常识别使用仓库中固定版本的本地模型和素材。

## 公开源码与发行版本

本仓库从发行构建提交 `4f793d3ef15d1b51bf867d168fef8e8af0d01be3` 导出。为了保护私人采集数据，使用新的公开 Git 历史，没有上传原始截图回归夹具、回忆数据库、识别报告和本地日志。

`src/`、`configs/`、`assets/`、`runtime/`、`packaging/` 与发行构建提交逐文件一致。公开整理仅涉及文档及私有数据排除；[PUBLIC-SOURCE.json](PUBLIC-SOURCE.json) 记录来源与文件校验值。Release 附件里的 `release-manifest.json` 保留实际构建版本，未把后续未验证改动算入发行版。

## 测试

不依赖私人图像夹具的检查可运行：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_short_text.py
.venv\Scripts\python.exe -m unittest discover -s tests -p test_text_order.py
.venv\Scripts\python.exe -m unittest discover -s tests -p test_rule_evaluation.py
```

测试源码保留完整，但部分图像回归测试需要未公开的私人截图，不能在公开仓库中直接运行全套测试并宣称通过。回归夹具说明见 [tests/fixtures/README.md](tests/fixtures/README.md)。

## 来源

上游代码、模型、目录及游戏素材来源与第三方许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。
