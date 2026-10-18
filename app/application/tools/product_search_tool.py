# -*- coding: utf-8 -*-
"""product_search_tool

商品检索工具：结构化检索入参 → CatalogSearchUseCase → 商品卡 JSON。
MainAgent 单干与 SearchAgent 派发两条路径共用同一工具实例。
工厂模式注入 UseCase 与 EventBus，模型看到的只是工具入参与返回值结构。

注意：本模块不能用 `from __future__ import annotations`——
AgentScope 用 pydantic 从函数签名动态生成 JSON schema，字符串化注解会解析失败。
"""
import json
import re
from typing import Optional

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.infrastructure.context import ShoppingContext
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.budget import remember_verified_result
from app.infrastructure.persistence.context_evidence import product_decision_view
from app.infrastructure.settings import EXTERNAL_PRODUCT_SEARCH_PROVIDERS


_KNOWN_CATEGORIES = (
    "旅行装备",
    "户外运动",
    "数码配件",
    "家居生活",
    "美妆个护",
    "厨房餐饮",
    "办公学习",
    "母婴宠物",
)

_CATEGORY_ALIASES = {
    "旅行装备": ("行李箱", "旅行收纳", "颈枕", "眼罩", "旅行包"),
    "户外运动": ("露营灯", "登山杖", "露营凳", "户外"),
    "数码配件": ("耳机", "耳塞", "充电器", "扩展坞", "三脚架", "数码"),
    "家居生活": ("家居", "收纳盒", "收纳架", "茶具", "香器"),
    "美妆个护": ("美妆", "个护", "护肤", "化妆"),
    "厨房餐饮": ("厨房", "餐具", "餐盒", "厨具"),
    "办公学习": ("办公", "学习", "文具", "台灯"),
    "母婴宠物": ("母婴", "宠物", "婴儿"),
}


def _normalize_category(category: Optional[str], normalized_query: str) -> Optional[str]:
    """把模型给出的叶子类目收敛到目录一级类目；无法识别时不施加错误硬过滤。"""
    for known in _KNOWN_CATEGORIES:
        if category == known or (category and known in category):
            return known
    haystacks = [value for value in (category, normalized_query) if value]
    for known, aliases in _CATEGORY_ALIASES.items():
        if any(alias in value for value in haystacks for alias in aliases):
            return known
    return next((known for known in _KNOWN_CATEGORIES if known in normalized_query), None)


def build_product_search_tool(usecase: CatalogSearchUseCase, bus: TradeEventBus, evidence_store=None, context_strategy="legacy",
                              external_provider=None, provider_mode="rainforest_index", product_provider=None):
    async def product_search_tool(
        normalized_query: str = "",
        category: Optional[str] = None,
        ship_to: Optional[str] = None,
        top_k: int | str = 5,
        price_max_major: float | str | None = None,
        target_currency: str = "CNY",
        excluded_material_tags: list[str] | None = None,
        required_material_tags: list[str] | None = None,
        product_id: str | None = None,
        sku_id: str | None = None,
    ) -> ToolChunk:
        """从 Amazon 同步索引检索并返回 Top-K 商品卡 JSON，不在查询时调用平台搜索 API。
        Amazon 商品只供展示和跳转，不支持 Globex 本地下单。价格是带同步时间的 USD 快照；
        ship_to 只用于说明配送尚未核实，不会计算运费、税费或 landed_price。已知 ASIN 时可传
        product_id="amazon:B012345678" 精确查本地索引；Amazon 商品没有本地 sku_id。

        Args:
            normalized_query (`str`):
                标准化检索词；Amazon 模式传简短英文关键词。精确 ASIN 查询时可以省略。
            category (`str | None`):
                品类槽位，可选，如"旅行装备"、"数码配件"。
            ship_to (`str | None`):
                收货国家二位码，可选，如 "CN"、"US"；Amazon 快照不含配送资格，结果会提示需在 Amazon 确认。
            top_k (`int`):
                返回候选数量，默认 5。
            price_max_major (`float | None`):
                价格上限（target_currency 主单位）；USD 预算按同步商品报价硬过滤，其它币种无法硬过滤。
            target_currency (`str`):
                价格口径币种，默认 "CNY"。
            excluded_material_tags (`list[str] | None`):
                材质黑名单，如买家明确不要塑料时传 ["合成聚合物"]。
            required_material_tags (`list[str] | None`):
                材质白名单，如必须是金属时传 ["金属"]。
            product_id (`str | None`):
                精确商品 ID；Amazon 模式使用 "amazon:<10 位 ASIN>"，例如 "amazon:B012345678"。
            sku_id (`str | None`):
                Amazon 同步索引不提供 Globex 本地 SKU/规格，该参数在 Amazon 模式不支持。
        """
        # 模型有时会把数字参数当字符串传（实测 qwen3-max 传 "300"），
        # schema 层放宽为接受数字字符串，这里统一强转后再进检索链路。
        if isinstance(top_k, str):
            try:
                top_k = int(top_k)
            except ValueError:
                return ToolChunk(
                    content=[TextBlock(type="text", text=f"[error] top_k 非法：{top_k}")],
                    state=ToolResultState.ERROR,
                )
        if isinstance(price_max_major, str):
            try:
                price_max_major = float(price_max_major)
            except ValueError:
                return ToolChunk(
                    content=[TextBlock(type="text", text=f"[error] price_max_major 非法：{price_max_major}")],
                    state=ToolResultState.ERROR,
                )
        snapshot_ctx = ShoppingContext.current()
        context_exclusions = list(snapshot_ctx.excluded_material_tags) if snapshot_ctx else []
        excluded_material_tags = list(
            dict.fromkeys([*context_exclusions, *(excluded_material_tags or [])]),
        )
        # ID 核验不能被查询文字中推测出的品类误过滤；显式品类约束仍保留。
        session_id = ShoppingContext.current_session_id()
        external_asin = None
        search_product_id = product_id
        external_error = None
        is_external_provider = provider_mode in EXTERNAL_PRODUCT_SEARCH_PROVIDERS
        is_amazon_provider = provider_mode in {"rainforest_index", "rainforest", "amazon_creators"}
        if is_external_provider:
            if is_amazon_provider and sku_id:
                external_error = "Amazon 外部商品没有 Globex 本地 SKU；请使用 ASIN 或关键词检索"
            if is_amazon_provider and product_id:
                candidate = product_id.removeprefix("amazon:").upper()
                if not re.fullmatch(r"[A-Z0-9]{10}", candidate):
                    external_error = "Amazon 精确商品查询需要 10 位 ASIN；本地商品 ID 不适用于当前商品源"
                external_asin = candidate
                search_product_id = None
            elif provider_mode == "mcp" and (product_id or sku_id):
                # MCP 来源采用统一关键词合同；把外部标识作为查询词交给来源端精确匹配。
                normalized_query = " ".join(value for value in (normalized_query, product_id, sku_id) if value).strip()
                search_product_id = None
        category = _normalize_category(category, "" if product_id or sku_id else normalized_query)
        args = {
            "normalized_query": normalized_query,
            "category": category,
            "ship_to": ship_to,
            "top_k": top_k,
            "price_max_major": price_max_major,
            "target_currency": target_currency,
            "excluded_material_tags": excluded_material_tags or [],
            "required_material_tags": required_material_tags or [],
            "product_id": product_id,
            "sku_id": sku_id,
        }
        bus.publish(session_id, "tool.invoke", {"tool": "product_search_tool", "args": args})
        if external_error:
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": external_error})
            return ToolChunk(content=[TextBlock(type="text", text=f"[error] {external_error}")], state=ToolResultState.ERROR)
        if not is_external_provider and ship_to and ship_to not in TariffSchedule(ExchangeRateTable()).supported_destinations():
            error = f"暂不支持的目的国：{ship_to}"
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": error})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] {error}")],
                state=ToolResultState.ERROR,
            )
        try:
            spec = ProductSearchSpec(
                normalized_query=normalized_query or (f"Amazon ASIN {external_asin}" if external_asin else normalized_query),
                category=category,
                ship_to=ship_to,
                top_k=top_k,
                price_max_major=price_max_major,
                target_currency=target_currency,
                excluded_material_tags=excluded_material_tags or [],
                required_material_tags=required_material_tags or [],
                product_id=search_product_id if is_amazon_provider or not is_external_provider else None,
                sku_id=sku_id if is_amazon_provider or not is_external_provider else None,
            )
            if is_external_provider:
                active_provider = product_provider or external_provider
                if active_provider is None:
                    raise RuntimeError("外部 Amazon 检索适配器未装配")
                if external_asin:
                    if provider_mode == "rainforest_index":
                        exact_price_cap = (
                            float(price_max_major)
                            if price_max_major is not None and str(target_currency).upper() == "USD"
                            else None
                        )
                        items = await active_provider.get_items(
                            [external_asin], category=category, price_max_usd=exact_price_cap,
                        )
                    else:
                        items = await active_provider.get_items([external_asin])
                    card = next((active_provider.to_external_card(item, category_hint=category) for item in items
                                 if str(item.get("asin", "")).upper() == external_asin), None)
                    exact_notes = ["Amazon 商品来自本地同步快照；价格不代表实时价格或库存。"]
                    if price_max_major is not None and str(target_currency).upper() != "USD":
                        exact_notes.append(f"预算币种为 {str(target_currency).upper()}，索引只提供 USD 报价，未按该预算硬过滤。")
                    if ship_to:
                        exact_notes.append(f"未核验是否配送至 {ship_to}，请在 Amazon 商品页确认。")
                    if required_material_tags or excluded_material_tags:
                        exact_notes.append("同步搜索结果没有结构化材质信息，材质条件未作硬过滤。")
                    result = {
                        "hits": [card] if card else [], "recall_strategy": f"{provider_mode}_asin_lookup",
                        "total_candidates": len(items), "rerank_applied": False, "filtered_out": [],
                        "notes": " ".join(exact_notes),
                        "price_filter_applied": price_max_major is not None and str(target_currency).upper() == "USD",
                    }
                else:
                    result = await active_provider.search(spec)
                result["query_conditions"] = args
                from datetime import datetime, timezone
                result["observed_at"] = datetime.now(timezone.utc).isoformat()
                # Amazon 商品只有当前界面展示，不进入本地订单证据或长期上下文证据库。
                external_view = {
                    key: value for key, value in result.items()
                    if key not in {"hits"}
                }
                external_view["hits"] = [
                    {key: value for key, value in hit.items()
                     if key in {"product_id", "canonical_product_id", "source_platform", "purchase_mode", "asin", "title", "brand",
                                "category", "price_major", "currency", "highlights", "purchase_url",
                                "source_price_major", "source_currency", "image_url", "rating_summary",
                                "rating_is_live", "synced_at", "updated_at", "score"}}
                    for hit in result.get("hits", [])
                ]
                view = external_view
            else:
                result = await product_provider.search(spec) if product_provider is not None else await usecase.execute(spec)
        except ValueError as err:
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": str(err)})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] {err}")],
                state=ToolResultState.ERROR,
            )
        except Exception as err:
            from app.infrastructure.amazon_creators import AmazonCreatorsError
            from app.infrastructure.rainforest_api import RainforestError
            if not isinstance(err, (AmazonCreatorsError, RainforestError, RuntimeError)):
                raise
            message = str(err)
            bus.publish(session_id, "tool.result", {"tool": "product_search_tool", "error": message})
            return ToolChunk(
                content=[TextBlock(type="text", text=f"[error] {message}")],
                state=ToolResultState.ERROR,
            )
        if not is_external_provider:
            result["query_conditions"] = args
            from datetime import datetime, timezone
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            remember_verified_result("products", result)
            if evidence_store is not None and snapshot_ctx is not None:
                result["result_ref"] = await evidence_store.save(snapshot_ctx.buyer_id, session_id, "products", result)
            from app.infrastructure.context_products import business_view, product_page, token_estimate
            view = product_decision_view(result) if context_strategy == "legacy" else business_view(result)
            if context_strategy != "legacy" and token_estimate(view) > 12000:
                if not result.get("result_ref"):
                    raise RuntimeError("大型商品结果未归档，不能安全分页")
                view = {**product_page(result, token_limit=12000), "query_conditions": args, "observed_at": result["observed_at"]}
        bus.publish(
            session_id,
            "tool.result",
            {
                "tool": "product_search_tool",
                "hit_count": len(result["hits"]),
                "recall_strategy": result["recall_strategy"],
                "total_candidates": result["total_candidates"],
                "rerank_applied": result["rerank_applied"],
                # 商品卡随事件下发，前端无需再调接口即可渲染（含 landed_price 到手价）
                "hits": result["hits"],
                **({"source_platform": "mcp" if provider_mode == "mcp" else
                    "amazon_creators" if provider_mode == "amazon_creators" else "amazon_rainforest",
                    "notes": result.get("notes", "")}
                   if is_external_provider else {}),
                **({"result_ref": result["result_ref"]} if "result_ref" in result else {}),
                **({"filtered_out": result["filtered_out"]} if "filtered_out" in result else {}),
            },
        )
        return ToolChunk(
            content=[TextBlock(type="text", text=json.dumps(view, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    if provider_mode in EXTERNAL_PRODUCT_SEARCH_PROVIDERS:
        product_search_tool.__doc__ = """从 Amazon.com 的本地同步索引检索商品，返回最多 10 件外部商品。
        中文需求可整理成简短英文关键词；检索不会实时调用 Amazon 或 Rainforest 搜索 API。
        商品只有 ASIN，没有 Globex SKU、目录库存或跨境到手价。价格是 USD 同步快照，结果包含同步时间。
        仅当预算币种为 USD 时按快照商品价过滤；其他币种不作硬过滤。配送与材质条件未经平台核验。
        通过商品卡 purchase_url 前往 Amazon 查看并购买。Amazon 商品不得传入本地创建订单工具。

        Args:
            normalized_query (str): 简短英文搜索词。
            category (str | None): 买家提出的品类提示。
            ship_to (str | None): 只用于提示配送国家，不能验证 Amazon 配送。
            top_k (int): 最多返回商品数，上限 10。
            price_max_major (float | None): USD 预算时按同步的美元商品价过滤。
            target_currency (str): 预算币种；Amazon 报价固定为 USD。
            excluded_material_tags (list[str] | None): 材质条件不能由搜索结果字段结构化验证。
            required_material_tags (list[str] | None): 材质条件不能由搜索结果字段结构化验证。
            product_id (str | None): 仅接受 Amazon 外部商品标识，不代表本地 SKU。
            sku_id (str | None): Amazon 外部商品没有 Globex SKU。
        """
    if provider_mode == "mcp":
        product_search_tool.__doc__ = """从已配置的只读 MCP 商品源检索候选商品。来源可能聚合多个平台；只使用工具返回的商品标识、平台、原币价格、更新时间和商品页链接。MCP 来源不验证订单、库存、配送或跨境到手价；未返回的信息保持未知。精确外部标识可放入 normalized_query 或 product_id。\n\nArgs:\n    normalized_query: 标准化商品关键词或来源商品标识。\n    category: 可选品类。\n    ship_to: 可选目的国，是否配送仍以商品来源为准。\n    top_k: 返回候选数。\n    price_max_major: 价格上限；仅当商品币种等于 target_currency 时本地硬过滤。\n    target_currency: 预算币种。\n    excluded_material_tags: 当前搜索的排除条件。\n    required_material_tags: 当前搜索的必需条件。\n    product_id: 外部商品标识，作为查询词交由 MCP 来源核验。\n    sku_id: 外部规格标识，作为查询词交由 MCP 来源核验。"""
    return product_search_tool
