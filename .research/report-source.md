# Claude Code 上下文管理调研：证据底稿

> 调研日期：2026-09-05。此文件是内部证据与推理底稿，正式结论见 `docs/research/Claude-Code上下文管理与Globex结合方案.md`。

## 研究问题

1. Claude Code 如何控制长会话中的消息、工具结果和持久知识？
2. `/compact`、自动压缩、Prompt Cache、会话持久化、auto memory、subagent 分别解决什么问题？
3. 哪些机制有公开、可验证的产品文档，哪些只能推断？
4. Globex 当前实现已经具备什么，缺什么，怎样以最小风险演进？

## 证据矩阵

| 主题 | 可验证结论 | 证据等级 | 主要来源 |
|---|---|---:|---|
| 上下文组成 | system、项目指令、auto memory、技能描述、对话、文件、工具输出都占上下文 | 官方产品文档 | https://code.claude.com/docs/en/context-window |
| 自动治理顺序 | 接近上限时先清理旧工具输出，仍不够再摘要会话 | 官方产品文档 | https://code.claude.com/docs/en/how-claude-code-works |
| 压缩语义 | `/compact` 用结构化摘要替换活跃消息历史；system 不属于被替换的消息历史 | 官方产品文档 | https://code.claude.com/docs/en/context-window |
| 压缩后恢复 | 根 CLAUDE.md、auto memory、plan 等重新注入；最近修改文件有限量重读 | 官方产品文档 | https://code.claude.com/docs/en/context-window |
| 压缩请求 | Claude Code 用同一 system、tools、history 加末尾摘要指令发独立请求；热缓存可复用旧前缀 | 官方产品文档 | https://code.claude.com/docs/en/prompt-caching |
| 缓存失效 | 压缩会改变 conversation 层，必然使该层缓存失效；system 层仍可复用 | 官方产品文档 | https://code.claude.com/docs/en/prompt-caching |
| 缓存本质 | 前缀精确匹配；Prompt Cache 降低重复计算/计费，但不减少上下文 token | 官方产品/API 文档 | https://code.claude.com/docs/en/prompt-caching；https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context |
| 工具定义治理 | MCP 工具定义默认延迟加载；只先放名称和服务说明 | 官方产品文档 | https://code.claude.com/docs/en/how-claude-code-works |
| 子 Agent | 独立上下文执行，高体积工具输出不进入主会话，只回传摘要；独立 transcript 可恢复和独立压缩 | 官方产品文档 | https://code.claude.com/docs/en/sub-agents |
| 长期记忆 | CLAUDE.md 存规则；auto memory 存学习与偏好，MEMORY.md 启动只加载前 200 行或 25KB，详情按需读 | 官方产品文档 | https://code.claude.com/docs/en/memory |
| 完整历史 | transcript 用 JSONL 持久化；压缩改变活跃上下文，不等于删除原始记录 | 官方产品文档 | https://code.claude.com/docs/en/sessions；https://code.claude.com/docs/en/checkpointing |
| 压缩扩展点 | PreCompact/PostCompact hook 可观测触发原因和生成摘要 | 官方产品文档 | https://code.claude.com/docs/en/hooks |
| API 工具清理 | Anthropic API 可按阈值从旧到新清理 tool_result，客户端仍保留完整历史 | 官方 API 文档 | https://platform.claude.com/docs/en/build-with-claude/context-editing |
| API 服务端压缩 | `compact_20260112` 达阈值生成 compaction block，后续忽略该 block 之前内容 | 官方 API 文档 | https://platform.claude.com/docs/en/build-with-claude/compaction |
| Claude Code 与 API 内部关系 | 公开文档未证明 Claude Code 一定直接使用 `clear_tool_uses_20250919` 或 `compact_20260112` | 未知，不应断言 | 产品文档只描述行为；API 文档只描述可用原语 |
| 具体摘要 prompt/清理算法 | 默认摘要提示会随模型/版本变化；工具输出清理的内部优先级与阈值未完整公开 | 部分公开/部分未知 | 官方文档只给行为与部分示例 |
| Qwen 隐式缓存 | 无需配置、不可关闭；公共前缀自动识别，命中不确定 | 百炼官方文档 | https://help.aliyun.com/zh/model-studio/context-cache |
| Qwen 显式缓存 | messages content 上用 `cache_control`；最多 4 点、至少 1024 token、5 分钟 TTL、最多回看 20 个 content 块 | 百炼官方文档 | https://help.aliyun.com/zh/model-studio/context-cache |

## 本地代码事实

- `context_policy.py`：75% 触发、15% 原文保留、摘要强调偏好/商品 ID/订单/待确认动作。
- AgentScope `_agent.py`：压缩前按 model token counter 计数；旧上下文进入结构化摘要，最近后缀保留；摘要写入 `AgentState.summary`；如果有 Offloader，旧消息可卸载并保留路径。
- AgentScope 工具结果上限的单位是 token，而 Globex 注释写成了“字符”，需要修正文档和配置认知。
- `main_agent.py`：每个购物会话恢复/持久化完整 `AgentState`。
- `orchestrator.py`：通过摘要前后比较发 `context.compressed`，但只有摘要字符数和消息数，没有压缩前后 token、事实保真率、缓存读写量。
- `preference_selector.py`：长期偏好按需注入，dislike 全量保留，like Top-K；这个安全策略合理。
- `task_dispatch_tool.py`：子 Agent 每次新建独立 AgentState，只回最终文本；与 Claude Code 的“高体积操作隔离”方向一致。
- `product_search_tool.py`：同一完整结果既进入 LLM tool_result，也通过事件流下发；可以改成“原始结果落冷存储/事件，LLM 只接收决策投影”。
- `llm.py`：默认 Qwen3-Max + OpenAI Chat 兼容接口，没有显式 `cache_control` 注入。
- AgentScope `OpenAIChatFormatter` 会重建标准 content block，不会保留任意 `cache_control`；显式缓存需要自定义 Formatter/Model 边界。
- AgentScope OpenAI Chat 模型已读取 `prompt_tokens_details.cached_tokens` 到 `ChatUsage.cache_input_tokens`，但当前业务记账与事件未呈现该指标；也没有读取百炼的 `cache_creation_input_tokens`。

## 推导原则

1. 事实型交易状态不能只存在 LLM 摘要中，必须由工具/领域层确定性维护。
2. 完整 transcript 与活跃 prompt 分离：前者可审计，后者为当前决策服务。
3. 先在源头投影工具输出，再清理旧工具结果，最后才调用 LLM 摘要。
4. Cache Breakpoint 按“未来多久会重复且完全不变”放置，而不是按语义重要程度或固定 K 轮放置。
5. 压缩是低频的离散重写；不要每轮对旧历史做不稳定的微摘要。
6. 评估必须同时看有效上下文大小、缓存命中、成本、延迟和任务事实保真率。

## 尚未验证/不应写成事实

- Claude Code 原生二进制的完整内部源代码、精确默认摘要 prompt 和所有清理启发式没有公开，不做逆向结论。
- 不把 Anthropic API 的 context editing/compaction beta 等同于 Claude Code 当前所有路径的内部实现。
- 不采用社区 issue 中历史版本的固定百分比，当前官方文档明确表示阈值随模型和配置变化。
- 不复用教程中没有原始实验记录的“85%→15%”“综合成本降低 35%”等数字作为项目事实。
