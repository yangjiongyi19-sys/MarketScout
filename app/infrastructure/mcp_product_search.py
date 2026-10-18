# -*- coding: utf-8 -*-
"""把受信任的只读 MCP 商品搜索工具接入商品来源端口。"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from urllib.parse import urlsplit

from agentscope.mcp import HttpMCPConfig, MCPClient

logger = logging.getLogger(__name__)


class MCPProductSearchError(RuntimeError):
    """MCP 商品服务不可用或违反约定时返回的脱敏错误。"""


class MCPProductSearchProvider:
    """调用一个经过部署方配置的 MCP 商品搜索工具。"""

    MAX_RESULT_CHARS = 256_000

    def __init__(self, settings, *, client=None) -> None:
        self._settings = settings
        self._tool_name = settings.mcp_product_search_tool
        self._allowed_domains = tuple(settings.mcp_product_allowed_domains)
        self._client = client or MCPClient(
            name="globex_product_source",
            is_stateful=False,
            mcp_config=HttpMCPConfig(
                url=settings.mcp_product_search_url,
                headers=(
                    {"Authorization": f"Bearer {settings.mcp_product_search_token}"}
                    if settings.mcp_product_search_token else None
                ),
                timeout=settings.mcp_product_search_timeout_seconds,
            ),
            enable_tools=[self._tool_name],
            execution_timeout=settings.mcp_product_search_timeout_seconds,
        )
        self._tool = None

    async def startup(self) -> None:
        """预取工具描述；远端不可用时由本次检索给出明确错误，服务仍可启动。"""
        await self._resolve_tool()

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception as error:  # noqa: BLE001
            logger.warning("MCP 商品客户端关闭失败（%s）", type(error).__name__)

    async def _resolve_tool(self):
        if self._tool is not None:
            return self._tool
        try:
            definitions = await self._client.list_raw_tools()
            definition = next((item for item in definitions
                               if getattr(item, "name", None) == self._tool_name), None)
            if definition is None:
                raise MCPProductSearchError("MCP 商品服务未提供配置的只读搜索工具")
            annotations = getattr(definition, "annotations", None)
            read_only = getattr(annotations, "readOnlyHint", None)
            destructive = getattr(annotations, "destructiveHint", None)
            if read_only is not True or destructive is True:
                raise MCPProductSearchError("MCP 商品工具必须声明只读且非破坏性")
            self._tool = await self._client.get_tool(self._tool_name)
            return self._tool
        except MCPProductSearchError:
            raise
        except Exception as error:  # noqa: BLE001 —— SDK/远端异常可能含 URL 或响应正文
            logger.warning("MCP 商品工具发现失败（%s）", type(error).__name__)
            raise MCPProductSearchError("MCP 商品服务暂时不可用，请稍后重试") from None

    async def search(self, spec) -> dict:
        tool = await self._resolve_tool()
        arguments = {
            "normalized_query": str(spec.normalized_query or "")[:500],
            "category": spec.category,
            "ship_to": spec.ship_to,
            "top_k": min(50, max(1, int(spec.top_k))),
            "price_max_major": spec.price_max_major,
            "target_currency": str(spec.target_currency or "USD").upper(),
            "excluded_material_tags": list(spec.excluded_material_tags or ()),
            "required_material_tags": list(spec.required_material_tags or ()),
        }
        try:
            async with asyncio.timeout(self._settings.mcp_product_search_timeout_seconds):
                chunk = await tool.call(**arguments)
        except TimeoutError:
            raise MCPProductSearchError("MCP 商品搜索超时，请稍后重试") from None
        except Exception as error:  # noqa: BLE001 —— 不向日志或买家暴露远端异常正文
            logger.warning("MCP 商品搜索调用失败（%s）", type(error).__name__)
            raise MCPProductSearchError("MCP 商品搜索暂时失败，请稍后重试") from None

        if getattr(getattr(chunk, "state", None), "value", "") == "error":
            raise MCPProductSearchError("MCP 商品服务返回检索失败")
        text = "\n".join(
            value for block in getattr(chunk, "content", ())
            if isinstance((value := getattr(block, "text", None)), str)
        )
        if not text or len(text) > self.MAX_RESULT_CHARS:
            raise MCPProductSearchError("MCP 商品服务返回为空或超过大小限制")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise MCPProductSearchError("MCP 商品结果必须是 JSON 文本") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise MCPProductSearchError("MCP 商品结果必须包含 hits 数组")

        hits = []
        price_filter_applied = arguments["price_max_major"] is not None
        skipped_currency_filter = False
        for item in payload["hits"][: min(256, arguments["top_k"] * 8)]:
            card = self._card(item)
            if card is None:
                continue
            cap = arguments["price_max_major"]
            if cap is not None:
                if card["currency"] == arguments["target_currency"]:
                    if card["price_major"] > cap:
                        continue
                else:
                    price_filter_applied = False
                    skipped_currency_filter = True
            hits.append(card)
            if len(hits) >= arguments["top_k"]:
                break

        notes = ["商品来自已配置的 MCP 服务；价格、库存和配送以来源页面为准。"]
        if skipped_currency_filter:
            notes.append("部分商品币种与预算币种不同，未按该预算硬过滤。")
        return {
            "hits": hits,
            "total_candidates": len(payload["hits"]),
            "recall_strategy": "mcp_product_search",
            "rerank_applied": False,
            "filtered_out": [],
            "notes": " ".join(notes),
            "price_filter_applied": price_filter_applied,
        }

    def _card(self, item) -> dict | None:
        if not isinstance(item, dict):
            return None
        product_id = item.get("product_id")
        title = item.get("title")
        source = item.get("source_platform")
        currency = item.get("currency")
        price = item.get("price_major")
        purchase_url = self._safe_url(item.get("purchase_url"))
        if (not isinstance(product_id, str) or not product_id.strip() or len(product_id) > 128
                or not re.fullmatch(r"[A-Za-z0-9._:/-]+", product_id)
                or not isinstance(title, str) or not title.strip() or len(title) > 500
                or not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", source)
                or not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency)
                or isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or price < 0 or purchase_url is None):
            return None
        amount = float(price)
        if amount > 1_000_000_000:
            return None
        card = {
            "product_id": f"mcp:{source.lower()}:{product_id}",
            "canonical_product_id": f"mcp:{source.lower()}:{product_id}",
            "source_platform": source.lower(),
            "purchase_mode": "external",
            "title": title.strip(),
            "price_major": amount,
            "currency": currency.upper(),
            "source_price_major": amount,
            "source_currency": currency.upper(),
            "purchase_url": purchase_url,
        }
        updated_at = item.get("updated_at") or item.get("synced_at")
        if isinstance(updated_at, str) and len(updated_at) <= 80:
            card["updated_at"] = updated_at
        highlights = item.get("highlights")
        if isinstance(highlights, list):
            card["highlights"] = [value.strip()[:200] for value in highlights[:8]
                                  if isinstance(value, str) and value.strip()]
        image_url = self._safe_url(item.get("image_url"))
        if image_url:
            card["image_url"] = image_url
        return card

    def _safe_url(self, value) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        try:
            parsed = urlsplit(value)
            host = parsed.hostname.lower().rstrip(".") if parsed.hostname else ""
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme != "https" or not host or port not in {None, 443}
                or parsed.username or parsed.password
                or not any(host == domain or host.endswith("." + domain)
                           for domain in self._allowed_domains)):
            return None
        return value
