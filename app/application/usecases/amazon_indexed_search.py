# -*- coding: utf-8 -*-
"""Amazon 商品搜索只访问已同步的 Qdrant 快照，不调用平台搜索 API。"""
from __future__ import annotations

import logging
from typing import Any

from app.domain.catalog.amazon_product import AmazonProductSnapshot
from app.infrastructure.vector.qdrant_amazon_catalog import QdrantAmazonCatalogIndex

logger = logging.getLogger(__name__)


class AmazonIndexedSearchProvider:
    def __init__(self, index: QdrantAmazonCatalogIndex, embedder, reranker=None) -> None:
        self._index = index
        self._embedder = embedder
        self._reranker = reranker

    @staticmethod
    def to_external_card(item: dict[str, Any], *, category_hint: str | None = None) -> dict[str, Any] | None:
        if not isinstance(item, dict) or not isinstance(item.get("asin"), str):
            return None
        if not item.get("title"):
            return None
        return AmazonProductSnapshot.from_payload(item).to_card()

    async def get_items(
        self,
        asins: list[str],
        *,
        category: str | None = None,
        price_max_usd: float | None = None,
    ) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(value.strip().upper() for value in asins if isinstance(value, str)))
        if any(len(asin) != 10 or not asin.isalnum() for asin in normalized):
            raise ValueError("Amazon 商品标识必须是 10 位 ASIN")
        payloads = await self._index.get_by_asins(normalized)
        matching = []
        for payload in payloads:
            if category is not None and payload.get("category") != category:
                continue
            if price_max_usd is not None:
                try:
                    price = float(payload.get("price_major"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if payload.get("currency") != "USD" or price > price_max_usd:
                    continue
            matching.append(payload)
        return matching

    async def search(self, spec) -> dict[str, Any]:
        if not await self._index.is_ready():
            raise RuntimeError("Amazon 商品索引尚未就绪，请先运行 python -m scripts.sync_product_catalog --once")
        query = (spec.normalized_query or "").strip()
        if not query:
            raise ValueError("请输入商品关键词，或提供 ASIN 精确查询")
        currency = str(spec.target_currency or "USD").upper()
        apply_price_filter = spec.price_max_major is not None and currency == "USD"
        price_cap = float(spec.price_max_major) if apply_price_filter else None
        limit = min(256, max(32, int(spec.top_k) * 8))
        embedding = await self._embedder.embed(query)
        hits = await self._index.search(
            embedding,
            top_n=limit,
            category=spec.category,
            price_max_usd=price_cap,
        )
        ranked = [(score, payload) for score, payload in hits]
        rerank_applied = False
        if ranked and self._reranker is not None:
            try:
                scores = await self._reranker.rerank(
                    query,
                    [str(payload.get("searchable_text") or "") for _, payload in ranked],
                )
                if len(scores) != len(ranked):
                    raise ValueError("Reranker 返回数量不匹配")
                ranked = sorted(
                    [(float(scores[index]), payload) for index, (_, payload) in enumerate(ranked)],
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                rerank_applied = True
            except Exception as error:
                logger.warning("Amazon 商品重排失败，保留向量顺序：%s", type(error).__name__)

        cards = []
        for score, payload in ranked:
            card = self.to_external_card(payload)
            if card is not None:
                card["score"] = round(float(score), 4)
                cards.append(card)
            if len(cards) >= min(10, max(1, int(spec.top_k))):
                break

        notes = ["商品信息来自本地 Rainforest 同步快照；价格为 USD 商品报价，不代表实时价格或库存。"]
        if cards:
            newest = max((card.get("synced_at", "") for card in cards), default="")
            if newest:
                notes.append(f"当前结果索引更新时间：{newest}。")
        if spec.price_max_major is not None and not apply_price_filter:
            notes.append(f"预算币种为 {currency}，Amazon 快照仅提供 USD 报价，未按该预算硬过滤。")
        if spec.ship_to:
            notes.append(f"未核验是否配送至 {spec.ship_to}，请在 Amazon 商品页确认。")
        if spec.required_material_tags or spec.excluded_material_tags:
            notes.append("同步搜索结果没有结构化材质信息，材质条件未作硬过滤。")
        return {
            "hits": cards,
            "recall_strategy": "rainforest_qdrant_vector_rerank" if rerank_applied else "rainforest_qdrant_vector",
            "total_candidates": len(hits),
            "rerank_applied": rerank_applied,
            "filtered_out": [],
            "notes": " ".join(notes),
            "price_filter_applied": apply_price_filter,
        }
