# MainAgent 子 Agent 消息观察

- 日期：2026-10-10（Asia/Shanghai）。
- 目标：真实启动本机项目并观察 MainAgent 派发 SearchAgent 时的输入与返回原文。
- query：请并行分头处理两个互不依赖的选购需求：A. 轻便通勤双肩包，预算不超过300元；B. 主动降噪耳机，预算不超过1000元。两项都寄到中国。各找最多3个候选，给出 product_id、SKU、价格、库存以及简短匹配理由，最后按品类汇总。
- 运行：`POST /commerce/ag-ui/run`，HTTP 200，AG-UI outcome success；thread `c20ef2a8-659d-4452-9938-4d1f39b0c552`，run `df328cd8-5dc7-4200-9b30-1146673653fd`。
- 模型：`qwen3.7-flash`。生效 Prompt 版本：`p-2144037979a337fd0e1553d9f634da2b663b4fd12d375c62ac1c6acedf9bb4fd`。
- 隔离：使用新的合成 buyer `codex-subagent-trace-20261010-01` 和新会话；未调用下单、取消订单或记忆写入工具。

## 子 Agent 实际输入

MainAgent 同轮派发了两个 `search_agent`。`task_dispatch_tool.py` 为每个新实例构造 `UserMsg(name="commerce_concierge", content=demands)`；子 Agent 不继承 MainAgent 的完整会话历史。SearchAgent 自己仍使用 `globex.yml` 中的系统提示词和自己的工具集。

本次 synthetic buyer 的只读数据库核对显示没有偏好事实或偏好审计记录（见 [memory-check.json](memory-check.json)），未生成额外 `memory_hint`，所以每个子 Agent 收到的 MainAgent 消息只有下列对应 demands：

1. `检索轻便通勤双肩包，预算不超过300元（CNY），收货国家CN。返回最多3个候选商品卡，包含product_id、SKU、价格（landed_price到手价）、库存、简短匹配理由。需轻便、适合日常通勤，不要超出预算的选项。`
2. `检索主动降噪耳机，预算不超过1000元（CNY），收货国家CN。返回最多3个候选商品卡，包含product_id、SKU、价格（landed_price到手价）、库存、简短匹配理由。需具备主动降噪功能，不要超出预算的选项。`

原始参数 JSON、完整返回工具文本及结构化结果见 [dispatch-messages.json](dispatch-messages.json)；逐事件参数/结果增量见 [dispatch-events.jsonl](dispatch-events.jsonl)。

## 子 Agent 返回给 MainAgent

`task_dispatch` 不会裸传子 Agent 消息，而是把它包装为 JSON：`agent`、`status`、`summary`、`evidence_refs`、`verified_product_ids`、`unverified_product_ids`。MainAgent 实际看到的是 `dispatch-messages.json` 中的 `result_returned_to_main_raw`。

- 背包任务：`status=unverified`。工具观察到的商品标识为 P1003、P1017、P1049 及其 SKU，但子 Agent 回复中还出现未被本轮工具证据核实的 P2192、P2200。派发工具将 `summary` 替换为“子任务回复包含未能由本轮检索证据核实的商品标识，请使用业务工具复核。”因此这次运行的事件流**没有保留被替换前的子 Agent 完整原始回复**；不能将替换前内容猜写为原文。
- 耳机任务：`status=completed`，`summary` 原文被放入返回 JSON。候选为 P1022 / P1022-S1，商品价 299 CNY、到手价 324 CNY、库存 80；其亮点只写“通话降噪”，子 Agent 同时注明未明确标注全频段主动降噪（ANC）。标识通过本轮证据核验。

子 Agent 业务检索中 reranker 未能运行，日志显示降级；本次不能据此声称精排成功。模型和 embedding HTTP 请求成功；无代码、Prompt、Harness 或运行配置变更，也未运行 Harness 评测。这是单次真实链路观察，不是效果或发布验收。

## 失败与数据边界

- 初次捕获脚本因项目导入路径未设置，在发送 HTTP 请求前失败；没有模型调用。原始记录保存在 [attempt-01-failure.txt](attempt-01-failure.txt)，修复采集路径后重新运行。
- 后续重复启动后端时，第二个进程因本地 Qdrant 已被第一个服务持有而启动失败；未改动或清除向量数据。异常记录见 [attempt-02-duplicate-backend.txt](attempt-02-duplicate-backend.txt)。第一个进程健康检查通过并完成 query。
- 运行只新增这次合成 buyer 的会话/运行日志和本目录证据。无订单、库存或长期偏好写入。
- query 通过网页同用的 AG-UI 接口提交，以便保存工具消息原文；没有在前端文本框再次提交，避免重复模型请求。前端首页已通过 HTTP 200 和应用内浏览器加载确认；只验证加载，没有读取或保存默认买家历史内容。
- 前端第一次从受限命令进程访问超时，后续以本机后台进程重新启动后访问成功；失败和复测记录见 [frontend-check-attempt-01.txt](frontend-check-attempt-01.txt)、[frontend-check-attempt-02.txt](frontend-check-attempt-02.txt)、[frontend-http-check.json](frontend-http-check.json) 与 [frontend-page-check.json](frontend-page-check.json)。后端和前端保持运行，地址分别为 `http://127.0.0.1:8000`、`http://127.0.0.1:5173`。
