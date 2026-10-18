from dataclasses import replace
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient

from app.domain.catalog.amazon_product import AmazonProductSnapshot
from app.infrastructure.vector.qdrant_amazon_catalog import AmazonCatalogIndexError, QdrantAmazonCatalogIndex


class Embedding:
    def __init__(self, vector=(1.0, 0.0, 0.0)):
        self.vector = list(vector)
        self.calls = []

    async def embed_batch(self, texts):
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]


class FailingEmbedding:
    async def embed_batch(self, texts):
        raise RuntimeError("embedding unavailable")


def settings(tmp_path):
    return SimpleNamespace(
        qdrant_url="",
        data_dir=tmp_path,
        amazon_product_collection="globex_amazon_test",
        rainforest_amazon_domain="amazon.com",
        embedding_base_url="https://embed.example/v1",
        embedding_model="test-embedding",
        embedding_version="v1",
        embedding_dim=3,
    )


def product(asin="B012345678", title="Lightweight Hiking Backpack", price=19.99):
    return AmazonProductSnapshot(
        amazon_domain="amazon.com",
        asin=asin,
        title=title,
        brand="Trail Maker",
        category="户外运动",
        highlights=["Lightweight design", "Water resistant"],
        price_major=price,
        currency="USD",
        rating_summary={"average": 4.6, "review_count": 123},
        image_url="https://m.media-amazon.com/images/sample.jpg",
        purchase_url=f"https://www.amazon.com/dp/{asin}",
        search_term="camping gear",
        synced_at="2026-10-11T02:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_snapshot_publishes_complete_cards_and_asin_lookup(tmp_path):
    client = AsyncQdrantClient(location=":memory:")
    index = QdrantAmazonCatalogIndex(settings(tmp_path), client=client)
    try:
        report = await index.replace_snapshot([product()], Embedding())
        assert report == {"total_products": 1, "reused_vectors": 0, "embedded_products": 1, "written_products": 1}
        assert await index.is_ready()
        payloads = await index.search([1.0, 0.0, 0.0], top_n=5, category="户外运动", price_max_usd=25)
        assert len(payloads) == 1
        payload = payloads[0][1]
        assert payload["asin"] == "B012345678"
        assert payload["amazon_domain"] == "amazon.com"
        assert payload["price_major"] == 19.99
        assert payload["synced_at"] == "2026-10-11T02:00:00+00:00"
        found = await index.get_by_asins(["B012345678", "B000000000"])
        assert len(found) == 1 and found[0]["asin"] == "B012345678"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_snapshot_reuses_vectors_and_swaps_alias_after_complete_write(tmp_path):
    client = AsyncQdrantClient(location=":memory:")
    index = QdrantAmazonCatalogIndex(settings(tmp_path), client=client)
    embedder = Embedding()
    try:
        await index.replace_snapshot([product()], embedder)
        first = await index.active_collection()
        report = await index.replace_snapshot([product()], FailingEmbedding())
        second = await index.active_collection()
        assert first != second
        assert report["reused_vectors"] == 1
        assert report["embedded_products"] == 0
        assert await client.collection_exists(second)
        assert not await client.collection_exists(first)
        rows = await index.get_by_asins(["B012345678"])
        assert rows[0]["title"] == "Lightweight Hiking Backpack"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_embedding_keeps_previous_active_snapshot(tmp_path):
    client = AsyncQdrantClient(location=":memory:")
    index = QdrantAmazonCatalogIndex(settings(tmp_path), client=client)
    try:
        await index.replace_snapshot([product()], Embedding())
        active = await index.active_collection()
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            await index.replace_snapshot([replace(product(), title="Changed title")], FailingEmbedding())
        assert await index.active_collection() == active
        rows = await index.get_by_asins(["B012345678"])
        assert rows[0]["title"] == "Lightweight Hiking Backpack"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_before_first_sync_has_clear_readiness_error(tmp_path):
    client = AsyncQdrantClient(location=":memory:")
    index = QdrantAmazonCatalogIndex(settings(tmp_path), client=client)
    try:
        assert not await index.is_ready()
        with pytest.raises(AmazonCatalogIndexError, match="先运行 python -m scripts.sync_product_catalog --once"):
            await index.search([1.0, 0.0, 0.0], top_n=5)
    finally:
        await client.close()
