"""仅离线还原本次演示的模型输入；不调用模型、工具或写入业务数据库。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent / "llm-input-inspection"
SID = "b93c2c24-354f-4b3c-8ffd-21fb3a54bd93"


def read_db(relative):
    return sqlite3.connect((ROOT / relative).resolve().as_uri() + "?mode=ro", uri=True)


def write_json(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def main():
    OUT.mkdir(exist_ok=True)
    with read_db("data/globex.db") as db:
        state = json.loads(db.execute("select state_json from agent_session_states where session_id=?", (SID,)).fetchone()[0])
    with read_db("data/prompts/registry.sqlite3") as db:
        row = db.execute("select a.version_id,v.payload from prompt_assignments a join prompt_versions v using(version_id) where a.session_id=?", (SID,)).fetchone()
    frozen = json.loads(row[1])
    with read_db("data/capabilities.db") as db:
        public_count = db.execute("select count(*) from capabilities").fetchone()[0]
    assert public_count == 0, "公共目录发生变化，不能直接还原历史目录"

    from agentscope.state import AgentState
    from agentscope.message import Msg
    from app.infrastructure.settings import load_settings
    from app.infrastructure.prompt_cache import PromptCacheFormatter
    from app.infrastructure.context_governance import LayeredContextMiddleware, update_working_state
    from app.infrastructure.context import ShoppingContext
    from app.application.agents.search_agent import SearchAgentFactory
    from app.application.agents.trade_agent import TradeAgentFactory
    from app.application.agents import main_agent as main_module

    settings = load_settings(require_llm_key=False)
    # 禁止创建数据库仓库；仅构造工具定义，依赖不会被执行。
    search = SearchAgentFactory.__new__(SearchAgentFactory)
    for key in ("_catalog_search", "_bus", "_knowledge_base", "evidence_store"):
        setattr(search, key, None)
    search._settings = settings
    search._resilience = lambda: []
    trade = TradeAgentFactory.__new__(TradeAgentFactory)
    for key in ("_place_order", "_query_order", "_cancel_order", "_bus", "evidence_store"):
        setattr(trade, key, None)
    trade._settings = settings
    trade._resilience = lambda: []
    public_registry = SimpleNamespace(metadata=lambda **kwargs: [])
    model = SimpleNamespace(model_id="qwen3.7-flash", context_size=128000)
    middleware = LayeredContextMiddleware(None, prompt_layout=settings.context_prompt_layout,
        state_mode=settings.context_state_mode, compact_result_rules=settings.context_compact_result_rules)
    with patch.object(main_module, "create_chat_model", return_value=model), \
         patch.object(main_module, "load_prompts", return_value=frozen["prompts"]), \
         patch.object(main_module, "build_agent_middlewares", return_value=[middleware]), \
         patch.object(main_module.MainAgentFactory, "_resilience", return_value=[]):
        factory = main_module.MainAgentFactory(settings, search, trade, None,
            SimpleNamespace(semantic_memory=False), None, None, capability_registry=public_registry,
            buyer_skill_store=SimpleNamespace(), shopping_form_store=SimpleNamespace())
        agent = factory.build()

    context = state["context"]
    buyer = [m for m in context if m["name"] == "pao-coder"]
    assistant = [m for m in context if m["role"] == "assistant"]
    hints = {m["name"]: m for m in context if m["name"] in ("memory_hint", "personal_skill_catalog", "candidate_state")}
    first_reply = assistant[0]
    runtime = next(b for b in first_reply["content"] if b["type"] == "hint")
    call = next(b for b in first_reply["content"] if b["type"] == "tool_call")
    result = next(b for b in first_reply["content"] if b["type"] == "tool_result")

    def partial_reply(contents):
        reply = copy.deepcopy(first_reply)
        reply["content"] = copy.deepcopy(contents)
        return reply

    first_prefix = [hints["memory_hint"], hints["personal_skill_catalog"], buyer[0]]
    contexts = [
        first_prefix + [partial_reply([runtime])],
        first_prefix + [partial_reply([runtime, call, result])],
        context[:context.index(buyer[1]) + 1],
    ]
    first_state = AgentState()
    dummy_agent = SimpleNamespace(state=first_state)
    with patch.object(ShoppingContext, "current", return_value=SimpleNamespace(buyer_id="pao-coder")):
        update_working_state(dummy_agent, [Msg.model_validate(buyer[0])])
    first_working = copy.deepcopy(first_state.middle_context["globex_context"]["working"])
    second_working = state["middle_context"]["globex_context"]["working"]
    formatter = PromptCacheFormatter(mode=settings.prompt_cache_mode, policy=settings.prompt_cache_policy)
    snapshots = []
    for index, saved_context in enumerate(contexts, 1):
        agent.state = AgentState()
        agent.state.context = [Msg.model_validate(m) for m in saved_context]
        agent.state.middle_context["globex_context"] = {"policy_version": "layered-v4", "working": copy.deepcopy(first_working if index < 3 else second_working)}
        prepared = await agent._prepare_model_input()
        formatted = await formatter.format(prepared["messages"])
        tools = prepared["tools"]
        # 此 JSON 的字段值由当前源码离线还原，不是 HTTP 原始请求证据。
        payload = {"model": "qwen3.7-flash", "messages": formatted, "tools": tools}
        write_json(f"{index:02d}-reconstructed-input.json", payload)
        snapshots.append(formatted)

    (OUT / "frozen-base-system-prompt.txt").write_text(frozen["prompts"]["main_agent"]["system_prompt"], encoding="utf-8")
    (OUT / "first-assistant-answer.txt").write_text("\n".join(b["text"] for b in first_reply["content"] if b["type"] == "text"), encoding="utf-8")
    (OUT / "saved-tool-result.json").write_text(json.dumps(json.loads(result["output"][0]["text"]), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json("working-state.json", {"first_turn_reconstructed": first_working, "second_turn_saved": second_working})

    notes = [
        "# 本次演示的 LLM 输入核查（离线还原）",
        "",
        "本目录不是历史 HTTP 请求抓包。正常业务运行未安装 evaluation_evidence_sink，未保存完整 model_request。",
        "三份 JSON 使用冻结的基础 Prompt、已保存的演示消息、当前工具定义及格式化器离线还原；不进行新模型调用。",
        "首轮 memory_hint 与个人 Skill 目录已被第二轮替换，使用第二轮空快照文本还原；动态公共目录当前为空。",
        "工作状态首轮从原文和当前解析规则重建，第二轮使用保存值。shopping_form 与公共能力工具按当前启动结构装配。",
        "工具 schema/配置使用当前源码和安全配置，不能证明等于当时线上进程的全部配置；请求参数、SDK隐式默认值、网关变换不在还原范围。",
        "ThinkingBlock 被当前 OpenAIChatFormatter 跳过；本目录没有输出其正文。",
        "本次已保存 summary 为空；没有把整个 AgentState、事件流、UI 图片或 permission_context 填进 messages。",
        "",
    ]
    titles = ["首轮首次模型调用：生成搜索工具调用", "首轮第二次模型调用：读取工具结果并生成推荐", "第二轮模型调用：读取历史并回答重量比较"]
    for i, (title, messages) in enumerate(zip(titles, snapshots), 1):
        notes += [f"## {i}. {title}", "", f"完整还原结构：[JSON]({i:02d}-reconstructed-input.json)。下列文本按 messages 顺序展开。", ""]
        for j, message in enumerate(messages, 1):
            notes += [f"### message {j}: role={message['role']}, name={message.get('name', '(无)')}", "", "```text"]
            content = message.get("content")
            if isinstance(content, str): notes.append(content)
            elif isinstance(content, list): notes += [b["text"] for b in content if b.get("type") == "text"]
            if message.get("tool_calls"): notes.append(json.dumps(message["tool_calls"], ensure_ascii=False, indent=2))
            notes += ["```", ""]
    (OUT / "实际输入核查.md").write_text("\n".join(notes), encoding="utf-8")
    frozen_checks = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
        for name, expected in frozen["toolset"]["source_files"].items() if (ROOT / name).is_file()}
    sources = ["app/infrastructure/context_governance.py", "app/infrastructure/prompt_cache.py",
        "app/infrastructure/llm.py", "app/infrastructure/context_usage.py", "app/application/agents/orchestrator.py",
        ".venv/Lib/site-packages/agentscope/agent/_agent.py", ".venv/Lib/site-packages/agentscope/formatter/_openai_formatter.py"]
    write_json("inspection-manifest.json", {
        "kind": "offline_reconstruction_not_raw_http_capture", "session_id": SID,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "prompt_version": row[0],
        "base_prompt_source": "session-bound frozen prompt registry", "frozen_tool_source_matches_current": frozen_checks,
        "configuration": {k: getattr(settings, k) for k in ("context_strategy", "context_prompt_layout", "context_state_mode", "context_compact_result_rules", "prompt_cache_mode", "prompt_cache_policy", "skill_catalog_mode")},
        "model_calls_executed_during_inspection": 0, "business_tools_executed_during_inspection": 0,
        "sources_sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in sources},
        "artifacts_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name != "inspection-manifest.json"},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    })
    print(json.dumps({"output": str(OUT), "snapshots": [
        [{"role": m["role"], "name": m.get("name"), "tool_calls": len(m.get("tool_calls", [])), "text_chars": len(str(m.get("content", "")))} for m in messages] for messages in snapshots],
        "tools": [t["function"]["name"] for t in tools], "frozen_source_matches": all(frozen_checks.values())}, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
