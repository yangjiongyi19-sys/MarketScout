# Rainforest API 接入验证记录

日期：2026-10-11（Asia/Shanghai）

## 验证范围

- 模拟 Rainforest 搜索、ASIN 商品详情、美元价格映射、联盟链接、收藏快照、限流、错误脱敏和缺少凭据行为。
- Amazon Creators API 既有回归。
- 前端用例与生产构建。
- 完整后端回归和 Harness `smoke` 档。
- Rainforest API key 未从聊天复制到文件或命令，也没有对 Rainforest 发起真实请求。

## 结果

- [Rainforest + Amazon Creators 定向回归](external-provider-tests.log)：30 passed。
- [前端测试](frontend-tests.log)：204 passed；[生产构建](frontend-build.log)：通过。
- [完整后端回归（UTF-8 复跑）](backend-tests-utf8.log)：1,818 passed、44 skipped、8 failed。6 项失败涉及 Windows 上 POSIX `0600` 权限位和符号链接创建权限；另 2 项本地检索断言期待 `P1008`，实际返回 `P1039`，单独复跑仍失败，当前没有同版本基线可判定其来源。完整失败堆栈保留在原始日志。
- [首次完整后端收集失败](backend-tests.log)：Windows 默认 GBK 编码读取评测 JSON 时发生解码错误；未覆盖该日志，后续启用 Python UTF-8 复跑。
- [Harness smoke 报告](harness-smoke/report.html)：838 项安全合同中 832 通过、6 失败。失败为上述 Windows 文件权限与符号链接契约。Harness 因安全合同失败停止，`current`/`candidate` 都是 0 次模型调用；`attempts.jsonl` 为空，不能称为完整冒烟通过。
- [Harness 同源码验证结果](harness-verify.log)：未通过，原因为运行不完整、场景缺失、安全合同未通过。

## 原始证据

目录内保留前后端测试日志、Harness 输入集、合同 XML 和日志、运行摘要、源码归档、HTML 报告、Verify 输出及单独复跑日志。运行集合 SHA-256：`eeb3f981d6b049579db21fbaafa272a4c6c61d4c0a3703ad677f80552d5b0a10`。当前副本无 Git 元数据，提交 SHA 未知。

## 外部链路状态

Rainforest API 实际账户链路未验证。凭据由用户在聊天中提交后，没有被写入 `.env`、源码、日志或评测材料；也没有消耗 Rainforest 请求额度。请在 Dashboard 轮换该 key，然后只在项目根目录本机 `.env` 或部署 Secret 中配置，再单独运行真实搜索联调。
