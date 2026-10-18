"""通过项目 AG-UI 入口运行合成 query，并保留派发消息证据。"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT.parents[2]))

from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.settings import load_settings


QUERY = (
    "请并行分头处理两个互不依赖的选购需求：A. 轻便通勤双肩包，预算不超过300元；"
    "B. 主动降噪耳机，预算不超过1000元。两项都寄到中国。各找最多3个候选，"
    "给出 product_id、SKU、价格、库存以及简短匹配理由，最后按品类汇总。"
)
BUYER_ID = "codex-subagent-trace-20261010-01"
THREAD_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
PAYLOAD = {
    "threadId": THREAD_ID,
    "runId": RUN_ID,
    "messages": [{"id": f"{RUN_ID}:user", "role": "user", "content": QUERY}],
    "tools": [],
    "context": [],
    "state": {},
    "forwardedProps": {"buyerId": BUYER_ID, "locale": "zh-CN", "currency": "CNY"},
}

(OUT / "request.json").write_text(
    json.dumps(PAYLOAD, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
policy = IdentityPolicy.from_settings(load_settings())
if policy.mode == "hmac":
    headers["Authorization"] = f"Bearer {policy.issue(BUYER_ID)}"
request = Request(
    "http://127.0.0.1:8000/commerce/ag-ui/run",
    data=json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8"),
    headers=headers,
    method="POST",
)

try:
    with urlopen(request, timeout=240) as response:
        status = response.status
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
except HTTPError as error:
    status = error.code
    content_type = error.headers.get("Content-Type", "")
    raw = error.read()
except (URLError, TimeoutError, OSError) as error:
    (OUT / "failure.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
    raise

frames = []
for frame in raw.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n\n"):
    data_lines = [line[6:] for line in frame.splitlines() if line.startswith("data: ")]
    if not data_lines:
        continue
    data = "\n".join(data_lines)
    try:
        frames.append((data, json.loads(data)))
    except json.JSONDecodeError:
        continue

relevant_types = {
    "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR", "RUN_FAILED",
    "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
    "TOOL_RESULT_START", "TOOL_RESULT_TEXT_DELTA", "TOOL_RESULT_END",
    "TOOL_CALL_RESULT",
}
starts: dict[str, str] = {}
args: dict[str, list[str]] = {}
result_chunks: dict[str, list[str]] = {}
direct_results: dict[str, list[str]] = {}
relevant = []

def field(event: dict, camel: str, snake: str):
    return event.get(camel, event.get(snake))

for data, event in frames:
    event_type = event.get("type", "")
    call_id = field(event, "toolCallId", "tool_call_id")
    if event_type == "TOOL_CALL_START":
        name = field(event, "toolCallName", "tool_call_name") or ""
        if call_id and name == "task_dispatch":
            starts[call_id] = name
    elif event_type == "TOOL_RESULT_START":
        name = field(event, "toolCallName", "tool_call_name") or ""
        if call_id and name == "task_dispatch":
            starts[call_id] = name
    if call_id in starts and event_type in relevant_types:
        relevant.append(data)
    if call_id in starts and event_type == "TOOL_CALL_ARGS":
        args.setdefault(call_id, []).append(event.get("delta", ""))
    if call_id in starts and event_type == "TOOL_RESULT_TEXT_DELTA":
        result_chunks.setdefault(call_id, []).append(event.get("delta", ""))
    if call_id in starts and event_type == "TOOL_CALL_RESULT":
        direct_results.setdefault(call_id, []).append(event.get("content", ""))

dispatches = []
for call_id, name in starts.items():
    raw_args = "".join(args.get(call_id, []))
    try:
        parsed_args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        parsed_args = None
    raw_result = "".join(direct_results.get(call_id, [])) or "".join(result_chunks.get(call_id, []))
    try:
        parsed_result = json.loads(raw_result) if raw_result else None
    except json.JSONDecodeError:
        parsed_result = None
    dispatches.append({
        "tool_call_id": call_id,
        "subagent_type": parsed_args.get("subagent_type") if isinstance(parsed_args, dict) else None,
        "demands": parsed_args.get("demands") if isinstance(parsed_args, dict) else None,
        "arguments_raw": raw_args,
        "result_returned_to_main_raw": raw_result,
        "result_parsed": parsed_result,
        "child_reply_raw_if_forwarded_verbatim": (
            parsed_result.get("summary") if isinstance(parsed_result, dict) else None
        ),
    })

(OUT / "dispatch-events.jsonl").write_text("\n".join(relevant) + ("\n" if relevant else ""), encoding="utf-8")
(OUT / "dispatch-messages.json").write_text(
    json.dumps({
        "http_status": status,
        "content_type": content_type,
        "thread_id": THREAD_ID,
        "run_id": RUN_ID,
        "buyer_id": BUYER_ID,
        "query": QUERY,
        "dispatch_count": len(dispatches),
        "dispatches": dispatches,
        "final_events": [event for _, event in frames if event.get("type", "").startswith("RUN_")],
    }, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps({
    "http_status": status,
    "thread_id": THREAD_ID,
    "run_id": RUN_ID,
    "dispatch_count": len(dispatches),
    "dispatch_types": [item["subagent_type"] for item in dispatches],
    "event_count": len(frames),
    "capture_dir": str(OUT),
}, ensure_ascii=False))
