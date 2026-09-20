# 本地原生 GKTools 引擎

已接通。引擎与数据固定为 `7153b0775c654d58c7b8a17b3a598322a35d0b2a`，74 个上游文件逐字节保留，BSD-3-Clause 许可在 `upstream/LICENSE`。`manifest.json` 记录全部 SHA-256；归档内容另已与该提交的 Git blob ID 核对。

环境：Node.js 24（实测 24.16.0）和项目 Python。引擎本身是 JavaScript ESM，只依赖同仓库 gakumas-data；没有 npm 包下载、TS 编译、网页构建或联网运行依赖。使用原上游 `scripts/extensionless-loader.mjs` 处理省略扩展名和 JSON 导入。识图不受影响。

仓库已包含运行文件，通常不需要安装。如果恢复文件，可运行 `python runtime/node/install.py <已验证的upstream.zip>`；安装器固定归档 SHA-256，拒绝未知归档及覆盖修改过的文件。它不是自动更新器。

运行：根目录 `launch-simulation.cmd`，或 `python simulate.py compare examples/simulation/original.loadout.json --trials 1000`。详细说明在 `docs/simulation.md`。

协议 v1：stdin/stdout 逐行 JSON；日志只写 stderr。请求包含 `protocol_version/request_id/loadout/trials/seed`。响应依次为 `type=progress`，最后 `type=result` 或 `type=error`。成功结果包含每次真实分数、次数、种子、RNG 调用数、引擎/数据版本、Node 版本及实际生效配置。Python 每候选使用独立进程，检查版本与文件完整性；取消或超时会终止 worker，不缓存半成品。

随机数：原引擎生产默认使用 Math.random；显式 `resetRand(uint32)` 才切换为原生 mulberry32。每个候选开始时设种子，此后连续运行 N 次，不能每次重设成同一个种子。同版本、同输入、同 Node 和同种子可复现；不同候选消耗随机数的数量可能不同，不声称逐次抽牌配对。

验收：`node --no-warnings --loader ./runtime/node/upstream/scripts/extensionless-loader.mjs ./runtime/node/upstream/tests/run.mjs`（524 个上游冻结样例）；`python -m unittest discover -s tests -p test_simulation.py -v`（Python桥接、独立参考、统计、缓存、取消等）。`reference.mjs` 使用原上游 URL 解析器直接调用引擎，不导入 worker 的映射逻辑。
