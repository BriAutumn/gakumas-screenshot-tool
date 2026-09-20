GKTool Windows x64 便携预览版

图文入门：双击同目录“使用说明.html”。图片已内嵌，可以离线阅读。

完整解压后双击 GKTool-preview.exe，不要在压缩包内直接运行。
内置 Python、OCR/ONNX 模型和 Node，无需安装 Python/Node、账号或 API 密钥。
功能运行离线；网页模拟使用本机默认浏览器与 127.0.0.1。

启动器：原图采集与离线处理 → 持有记忆库 → 本地模拟与比较。
首次采集需要设置游戏窗口与区域；游戏若以管理员运行，采集工具需同等权限。
各工具独立运行；采集/识别请从其窗口停止。网页关闭后请在服务窗口点击停止。

所有用户数据和日志默认位于 %LOCALAPPDATA%\GKTool。
备份/迁移请复制该目录，包含原图、采集台账和 SQLite 记忆库；升级不覆盖此目录。
高级用户可设置 GKTOOL_USER_HOME 为绝对路径以更改数据目录。
此包不包含任何现有个人截图、记忆库或模拟结果。

出现问题点击“运行诊断”，查看数据目录 logs\diagnostics.json。
启动异常查看 logs\startup-error.txt 和 logs\desktop.log。
诊断将实际初始化本地 OCR/ONNX、Tk、SQLite 和模拟引擎。
请勿只复制 exe；损坏时重新完整解压到新目录，用户数据仍保留。

这是包装集成预览，不是最终识图/模拟验收发行，不承诺 100% 自动识别。
第三方来源见 _internal\THIRD_PARTY.md；依赖清单见 _internal\dependencies.json。
许可证位于 _internal\licenses、assets、runtime 内相应 LICENSE 文件。
游戏名称、美术权利归相应权利人，代码许可不等于素材再分发授权。
