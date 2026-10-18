# -*- coding: utf-8 -*-
"""按受控关键词从 Rainforest 获取 Amazon 搜索快照并发布至 Qdrant。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.domain.catalog.amazon_product import AmazonProductSnapshot
from app.infrastructure.rainforest_api import RainforestApiProvider, RainforestError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncQuery:
    category: str
    search_term: str
    category_id: str = ""


@dataclass(frozen=True)
class ProductSyncScope:
    amazon_domain: str
    max_pages_per_query: int
    queries: tuple[SyncQuery, ...]


def load_sync_scope(path: str | Path) -> ProductSyncScope:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        raise ValueError(f"无法读取商品同步范围配置：{config_path}") from error
    if not isinstance(raw, dict):
        raise ValueError("商品同步范围配置必须是 YAML 对象")
    domain = str(raw.get("amazon_domain", "amazon.com")).strip().lower()
    if domain != "amazon.com":
        raise ValueError("当前商品同步仅支持 amazon.com")
    pages = raw.get("max_pages_per_query", 1)
    if type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError("max_pages_per_query 必须是 1 到 5 的整数")
    entries = raw.get("queries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("商品同步范围至少需要一个关键词")
    queries: list[SyncQuery] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("每个商品同步关键词必须是对象")
        category = entry.get("category")
        term = entry.get("search_term")
        category_id = entry.get("category_id", "")
        if not isinstance(category, str) or not category.strip() or not isinstance(term, str) or not term.strip():
            raise ValueError("每个关键词都需要非空 category 和 search_term")
        if not isinstance(category_id, str):
            raise ValueError("category_id 必须是字符串")
        queries.append(SyncQuery(category.strip(), term.strip(), category_id.strip()))
    return ProductSyncScope(domain, pages, tuple(queries))


class RainforestProductCatalogSync:
    def __init__(self, provider: RainforestApiProvider, index, embedder, scope: ProductSyncScope) -> None:
        self._provider = provider
        self._index = index
        self._embedder = embedder
        self._scope = scope

    async def sync_once(self) -> dict[str, Any]:
        synced_at = datetime.now(timezone.utc).isoformat()
        products: dict[str, AmazonProductSnapshot] = {}
        page_count = 0
        for query in self._scope.queries:
            for page in range(1, self._scope.max_pages_per_query + 1):
                items = await self._provider.search_page(
                    query.search_term,
                    page=page,
                    category_id=query.category_id or None,
                )
                page_count += 1
                if not items:
                    break
                for item in items:
                    card = self._provider.to_external_card(item, category_hint=query.category)
                    if card is None:
                        continue
                    asin = card["asin"].upper()
                    snapshot = AmazonProductSnapshot.from_external_card(
                        card,
                        amazon_domain=self._scope.amazon_domain,
                        category=query.category,
                        search_term=query.search_term,
                        synced_at=synced_at,
                    )
                    previous = products.get(asin)
                    if previous is None:
                        products[asin] = snapshot
                    else:
                        # 同一 ASIN 命中多个词时保留首个类目，合并检索词和不重复的卖点。
                        merged_terms = ", ".join(dict.fromkeys((previous.search_term, snapshot.search_term)))
                        merged_highlights = list(dict.fromkeys([*previous.highlights, *snapshot.highlights]))[:5]
                        products[asin] = replace(
                            previous,
                            search_term=merged_terms,
                            highlights=merged_highlights,
                            price_major=previous.price_major if previous.price_major is not None else snapshot.price_major,
                            image_url=previous.image_url or snapshot.image_url,
                            purchase_url=previous.purchase_url or snapshot.purchase_url,
                            rating_summary=previous.rating_summary or snapshot.rating_summary,
                        )
        if not products:
            raise RainforestError("Rainforest 同步范围没有返回有效商品；保留当前活动索引")
        index_report = await self._index.replace_snapshot(list(products.values()), self._embedder)
        report = {
            "complete": True,
            "amazon_domain": self._scope.amazon_domain,
            "query_count": len(self._scope.queries),
            "returned_pages": page_count,
            "unique_products": len(products),
            "synced_at": synced_at,
            **index_report,
        }
        logger.info(
            "Amazon 商品索引刷新完成：关键词 %d，页 %d，去重后 %d，复用向量 %d，新建向量 %d",
            report["query_count"], report["returned_pages"], report["unique_products"],
            report.get("reused_vectors", 0), report.get("embedded_products", 0),
        )
        return report
