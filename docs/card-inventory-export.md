# 本地角色卡与支援卡导出

插件 `1.0.8-inventory` 在游戏启动、账号数据加载后，自动读取账号的角色卡和支援卡集合。不需要打开对应的卡片页面。当前仅导出，不导入网页、旧卡库或模拟器，也不转换为 GKTools ID。

## 文件位置

以 `GKMemoryExport.ini` 的 `ExportDirectory` 为根目录：

```text
card-inventory/
  status.json
  snapshots/<本次进程与启动标识>/
    idol-cards.json
    support-cards.json
    manifest.json
```

先读取 `card-inventory/status.json`，只有 `completed: true` 时才读取其 `snapshot_dir` 指向的快照。`files` 中保存两个数组文件的 SHA256，`counts` 保存行数；快照目录中的 `manifest.json` 提供相同的校验信息。旧快照保留。原有 `memories.json` 格式不变。

## 字段

两个 JSON 文件均为数组，保留游戏原生 ID 和字段名。

| 文件 | 必需字段 | 可选字段 |
| --- | --- | --- |
| idol-cards.json | IdolCardId、LevelLimitRank、PotentialRank | IdolCardSkinId、Level、LevelLimit、PlanType、RarityInt |
| support-cards.json | SupportCardId、Level、LevelLimitRank、StockQuantity | LevelLimit、PlanType、RarityInt |

角色卡的 `LevelLimitRank` 为特训阶级，`PotentialRank` 为才能开花阶级。支援卡保留等级、突破阶级及库存数量。可选字段读取不到时为 `null`，原因记录在该行的 `field_issues`；必需字段读取失败则本次卡库导出失败，不用零填补。所有账号集合行都保留，不因阶级或库存为零而过滤。

## 失败处理与范围

角色卡集合必须已加载且非空，两个集合连续两次读取一致、间隔至少 500 毫秒后才发布。角色卡已加载时，空支援卡集合允许正常导出。整个导出共用原有 180 秒上限，失败或超时会写入状态，数量为 `null`，不会覆盖旧快照。回忆导出与卡库导出分别记录成功或失败。

只读取这两个账号集合，不导出密码、令牌、余额或玩家资料。加载时机、IL2CPP 线程附加和 version.dll / winhttp.dll 共存入口沿用原有实现。

## 本机验证

2026-10-18：实机成功导出角色卡 102 行、支援卡 165 行、回忆 418 份。两个卡库文件均通过数量、ID 唯一性、必需字段和 SHA256 校验。另通过 15 个原生隔离案例及两个 DLL 入口共 10 个集成案例。此功能暂为本地开发版本，尚未发布。
