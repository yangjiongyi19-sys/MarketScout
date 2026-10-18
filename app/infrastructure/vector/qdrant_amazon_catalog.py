# -*- coding: utf-8 -*-
"""Rainforest 商品快照的 Qdrant blue-green 索引。"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
    VectorParams,
)

from app.domain.catalog.amazon_product import AmazonProductSnapshot
from app.infrastructure.settings import Settings
from app.infrastructure.vector.embedding_identity import embedding_identity

logger = logging.getLogger(__name__)


def _point_id(domain: str, asin: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"globex/product/{domain.lower()}/{asin.upper()}"))


class AmazonCatalogIndexError(RuntimeError):
    pass


class QdrantAmazonCatalogIndex:
    def __init__(self, settings: Settings, *, client: AsyncQdrantClient | None = None) -> None:
        if client is not None:
            self._client = client
            self._owns_client = False
        elif settings.qdrant_url:
            self._client = AsyncQdrantClient(url=settings.qdrant_url)
            self._owns_client = True
        else:
            local_path = settings.data_dir / "qdrant"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=str(local_path))
            self._owns_client = True
        self.alias = settings.amazon_product_collection
        self.amazon_domain = settings.rainforest_amazon_domain
        self.embedding_key = embedding_identity(settings)
        self.vector_dim = settings.embedding_dim

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def _aliases(self) -> dict[str, str]:
        response = await self._client.get_aliases()
        return {item.alias_name: item.collection_name for item in response.aliases}

    async def active_collection(self) -> str | None:
        return (await self._aliases()).get(self.alias)

    async def is_ready(self) -> bool:
        return await self.active_collection() is not None

    async def _existing_points(self, collection: str | None) -> dict[str, tuple[dict[str, Any], list[float]]]:
        if collection is None:
            return {}
        existing: dict[str, tuple[dict[str, Any], list[float]]] = {}
        offset = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in points:
                payload = point.payload or {}
                vector = point.vector
                if isinstance(vector, dict):
                    vector = next(iter(vector.values()), None)
                if isinstance(payload.get("asin"), str) and isinstance(vector, list):
                    existing[payload["asin"].upper()] = (payload, vector)
            if offset is None:
                break
        return existing

    async def replace_snapshot(
        self,
        products: list[AmazonProductSnapshot],
        embedder,
        *,
        batch_size: int = 32,
    ) -> dict[str, int]:
        if not products:
            raise AmazonCatalogIndexError("本次同步未获得商品，保留现有索引")
        if batch_size < 1:
            raise ValueError("batch_size 必须为正整数")

        # 不允许重复 ASIN 进入物理索引；服务层已去重，这里再作边界校验。
        unique: dict[str, AmazonProductSnapshot] = {}
        for product in products:
            unique.setdefault(product.asin.upper(), product)
        products = list(unique.values())
        old_collection = await self.active_collection()
        existing = await self._existing_points(old_collection)
        snapshot_id = uuid.uuid4().hex[:12]
        physical_collection = f"{self.alias}__snapshot_{snapshot_id}"
        created = False
        try:
            await self._client.create_collection(
                collection_name=physical_collection,
                vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE),
            )
            created = True
            vectors: dict[str, list[float]] = {}
            pending: list[AmazonProductSnapshot] = []
            for product in products:
                prior = existing.get(product.asin.upper())
                payload = product.to_payload(self.embedding_key)
                if prior and prior[0].get("text_hash") == payload["text_hash"] and prior[0].get("embedding_key") == self.embedding_key:
                    vectors[product.asin] = prior[1]
                else:
                    pending.append(product)

            embedded_count = 0
            for start in range(0, len(pending), batch_size):
                batch = pending[start:start + batch_size]
                results = await embedder.embed_batch([item.searchable_text() for item in batch])
                if len(results) != len(batch):
                    raise AmazonCatalogIndexError("Embedding 返回数量与商品数量不匹配")
                for product, vector in zip(batch, results):
                    if len(vector) != self.vector_dim or not all(
                        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                        for value in vector
                    ):
                        raise AmazonCatalogIndexError("Embedding 维度错误或包含非有限数值")
                    vectors[product.asin] = [float(value) for value in vector]
                    embedded_count += 1

            points = [
                PointStruct(
                    id=_point_id(product.amazon_domain, product.asin),
                    vector=vectors[product.asin],
                    payload=product.to_payload(self.embedding_key),
                )
                for product in products
            ]
            for start in range(0, len(points), 256):
                await self._client.upsert(collection_name=physical_collection, points=points[start:start + 256], wait=True)

            count = await self._client.count(collection_name=physical_collection, exact=True)
            if count.count != len(products):
                raise AmazonCatalogIndexError(f"暂存索引校验失败：期望 {len(products)} 条，实际 {count.count} 条")

            # Qdrant 在单次 ChangeAliasesOperation 中原子切换活动别名。
            aliases = await self._aliases()
            current = aliases.get(self.alias)
            actions = []
            if current:
                actions.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=self.alias)))
            actions.append(CreateAliasOperation(create_alias=CreateAlias(
                collection_name=physical_collection, alias_name=self.alias,
            )))
            await self._client.update_collection_aliases(actions)

            if current and current.startswith(f"{self.alias}__snapshot_"):
                try:
                    await self._client.delete_collection(current)
                except Exception as err:  # 已切换成功，清理失败只留下孤立快照。
                    logger.warning("旧 Amazon 商品索引清理失败：%s", type(err).__name__)
            return {
                "total_products": len(products),
                "reused_vectors": len(products) - embedded_count,
                "embedded_products": embedded_count,
                "written_products": len(points),
            }
        except Exception:
            if created:
                try:
                    aliases = await self._aliases()
                    if aliases.get(self.alias) != physical_collection and await self._client.collection_exists(physical_collection):
                        await self._client.delete_collection(physical_collection)
                except Exception:
                    logger.exception("删除未发布的 Amazon 暂存索引失败")
            raise

    async def search(
        self,
        embedding: list[float],
        *,
        top_n: int,
        category: str | None = None,
        price_max_usd: float | None = None,
    ) -> list[tuple[float, dict[str, Any]]]:
        if not await self.is_ready():
            raise AmazonCatalogIndexError("Amazon 商品索引尚未就绪，请先运行 python -m scripts.sync_product_catalog --once")
        must = [FieldCondition(key="embedding_key", match=MatchValue(value=self.embedding_key))]
        if category:
            must.append(FieldCondition(key="category", match=MatchValue(value=category)))
        if price_max_usd is not None:
            must.append(FieldCondition(key="price_major", range=Range(lte=float(price_max_usd))))
        result = await self._client.query_points(
            collection_name=self.alias,
            query=embedding,
            limit=top_n,
            query_filter=Filter(must=must),
            with_payload=True,
        )
        return [
            (float(point.score or 0), point.payload)
            for point in result.points
            if point.payload and isinstance(point.payload.get("asin"), str)
        ]

    async def get_by_asins(self, asins: list[str]) -> list[dict[str, Any]]:
        collection = await self.active_collection()
        if collection is None:
            raise AmazonCatalogIndexError("Amazon 商品索引尚未就绪，请先运行 python -m scripts.sync_product_catalog --once")
        ids = [_point_id(self.amazon_domain, asin) for asin in dict.fromkeys(value.upper() for value in asins)]
        if not ids:
            return []
        points = await self._client.retrieve(
            collection_name=collection,
            ids=ids,
            with_payload=True,
            with_vectors=False,
        )
        by_asin = {str((point.payload or {}).get("asin", "")).upper(): point.payload or {} for point in points}
        return [by_asin[asin] for asin in dict.fromkeys(value.upper() for value in asins) if asin.upper() in by_asin]
