# 来源与版本

识别资产来自 [surisuririsu/gakumas-tools](https://github.com/surisuririsu/gakumas-tools)，固定提交：
`7153b0775c654d58c7b8a17b3a598322a35d0b2a`。

- 卡片模型、分类标签：`gakumas-tools/public/skill_card_*`。
- 道具模型与标签也已保存，但当前道具识别使用模板，不使用这套模型推断。
- 名称 / 场地 / 自定义目录：`packages/gakumas-data/json/`，仅用于识别与核对，不执行效果 DSL。
- 0.5.31 起，`tools/catalog/upstream/` 保留同一固定版本的效果解析器、转换器和 BSD-3-Clause 许可；仅为 Node ESM 补齐 `.js` 导入后缀，原文和本地哈希见 `provenance.json`。开发工具生成 `catalog-effects.json`，用于强化候选的追加、锚点替换和有限数值成长解析；不运行对局模拟。上面“仅目录”是此前状态。
- 道具图标：`packages/gakumas-images/images/pItems/icons/`，477 张原始图标，运行时仅加载舞台道具。
- 强化图形参考：`skillCards/icons/592_1.png` 与 `593_1.png`。
- 独立记忆识别另使用 864 个卡片 ID 的规范图标（每个 ID 一个，普通路径或 `_1` 变体），保留在 `card-icons/`；用于辅助核对 ONNX 低分候选，不假定所有美术变体都一致。
- 参考的预处理与 URL 行为：[memory.js](https://github.com/surisuririsu/gakumas-tools/blob/7153b0775c654d58c7b8a17b3a598322a35d0b2a/gakumas-tools/utils/imageProcessing/memory.js)、[simulatorLoadout.js](https://github.com/surisuririsu/gakumas-tools/blob/7153b0775c654d58c7b8a17b3a598322a35d0b2a/gakumas-tools/utils/imageProcessing/simulatorLoadout.js)、[simulator.js](https://github.com/surisuririsu/gakumas-tools/blob/7153b0775c654d58c7b8a17b3a598322a35d0b2a/gakumas-tools/utils/simulator.js)。

完整路径和 SHA-256 在 `assets/recognition/manifest.json`。上游仓库 BSD-3-Clause 许可原文保留于 `assets/recognition/LICENSE`；游戏名称、美术和用户截图的权利仍归相应权利人，不因仓库代码许可而改变。

OCR 使用 [RapidOCR](https://github.com/RapidAI/RapidOCR) 的 `rapidocr-onnxruntime` 包及其随包模型；ONNX Runtime、NumPy、Pillow、OpenCV 等依赖的版本记录于 `requirements-recognition.txt`，各自许可由安装包保留。

卡片详情另用 RapidAI/RapidOCR 发布的 `japan_PP-OCRv4_rec_mobile.onnx`（PaddleOCR 日文识别模型，v3.9.2 模型集合）。固定下载地址、SHA-256 在 `assets/ocr/manifest.json`，约 9.75 MB。哈希与 [RapidOCR 默认模型清单](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml) 核对；Apache-2.0 许可原文保存为 `assets/ocr/LICENSE-RapidOCR`。模型已经下载到项目，运行时不下载或访问模型站点。
