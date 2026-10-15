# 来源与许可

模拟引擎、卡片/道具/偶像/场地/自定义目录和图标来自 [surisuririsu/gakumas-tools](https://github.com/surisuririsu/gakumas-tools)。本版固定引擎提交 `9ff4610011feb395e363d86d7c961e4da68b6deb`；逐文件哈希保存在 `runtime/node/manifest.json`，上游 BSD-3-Clause 许可原文保存在 `runtime/node/upstream/LICENSE` 和分发包 `licenses/`。

原生 ID 映射依据公开的 [vertesan/gakumasu-diff](https://github.com/vertesan/gakumasu-diff) 主数据与上述模拟目录生成，来源版本记录在 `assets/native/mapping.json` 的 provenance 中。映射覆盖整个固定目录，不限于开发者拥有的回忆。

游戏名称、角色与卡图等素材的权利归其相应权利人，上游代码许可不改变素材权利。分发包不包含账号回忆、游戏完整主数据包或 OCR 模型。

`assets/memory-covers` 包含本地已发布游戏资源中的 15 张内置回忆封面，按原始 AssetId 提取原纹理；资源 MD5 与 PNG SHA-256 记录在目录内的清单中。封面素材权利归游戏权利人。提取参考 [Gakuen-idolmaster-ab-decrypt](https://github.com/nijinekoyo/Gakuen-idolmaster-ab-decrypt) 的资源格式实现；运行时无需该解包器。玩家拍摄的照片仅从本机缓存只读显示，不随软件分发。

运行时包括 Python 3.13 / Tcl/Tk 8.6 和 Node.js 24.16.0，无需用户另外安装。相应许可及实际包含的原生依赖许可保存于 `licenses/`，版本记录在 `native-dependencies.json`。只读导出插件源码位于 `tools/native-memory-export/`，使用 Windows 系统 API，不捆绑游戏 DLL。

独立数据同步直接读取 [gakumas-tools/gakumas-tools](https://github.com/gakumas-tools/gakumas-tools) 与上述公开主数据源，并在本机生成对应关系。每次同步保留精确来源提交、文件哈希和校验结果；不向上游发送账号回忆。解析主数据使用 PyYAML 6.0.3（MIT），完整许可随包保存为 `licenses/PyYAML-MIT.txt`。
