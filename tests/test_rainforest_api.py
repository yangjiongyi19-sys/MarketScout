import json

import httpx
import pytest
from agentscope.tool import FunctionTool

from app.application.tools.order_tools import _verified_order_items
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.amazon_indexed_search import AmazonIndexedSearchProvider
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.rainforest_catalog_sync import ProductSyncScope, RainforestProductCatalogSync, SyncQuery
from app.domain.catalog.amazon_product import AmazonProductSnapshot
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.buyer_favorites import BuyerFavoriteStore
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.rainforest_api import (
    RainforestApiProvider,
    RainforestConfigurationError,
    RainforestError,
    RainforestRateLimitError,
)
from app.infrastructure.settings import load_settings
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository


ASIN = "B012345678"


def search_result(*, price=19.99, asin=ASIN):
    return {
        "asin": asin.lower(),
        "title": "Lightweight Hiking Backpack",
        "link": f"https://www.amazon.com/dp/{asin}?tag=sample-20&ref=sr_1_1",
        "image": "https://m.media-amazon.com/images/sample.jpg",
        "brand": "Trail Maker",
        "price": {"value": price, "currency": "USD"} if price is not None else None,
        "rating": 4.6,
        "ratings_total": 123,
        "feature_bullets": ["Lightweight design", "Water resistant"],
    }


def provider(handler, *, sleep=None, clock=None, api_key="test-key"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs = {"api_key": api_key, "client": client}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if clock is not None:
        kwargs["clock"] = clock
    return RainforestApiProvider(**kwargs), client


@pytest.mark.asyncio
async def test_rainforest_page_for_sync_maps_result_without_detail_fanout():
    calls = []

    async def handler(request):
        calls.append(request)
        params = dict(request.url.params)
        assert request.url.path == "/request"
        assert params["api_key"] == "test-key"
        assert params["type"] == "search"
        assert params["amazon_domain"] == "amazon.com"
        assert params["search_term"] == "camping gear"
        assert params["page"] == "1"
        assert params["category_id"] == "12345"
        assert params["exclude_sponsored"] == "true"
        assert params["associate_id"] == "sample-20"
        assert "include_products_count" not in params
        return httpx.Response(200, json={"request_info": {"success": True}, "search_results": [search_result()]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = RainforestApiProvider(api_key="test-key", associate_id="sample-20", client=client)
    try:
        items = await api.search_page("camping gear", page=1, category_id="12345")
        card = api.to_external_card(items[0], category_hint="户外运动")
        assert len(calls) == 1
        assert card["product_id"] == f"amazon:{ASIN}"
        assert card["source_platform"] == "amazon_rainforest"
        assert card["purchase_mode"] == "external"
        assert card["asin"] == ASIN
        assert card["price_major"] == 19.99 and card["currency"] == "USD"
        assert card["rating_summary"] == {"average": 4.6, "review_count": 123}
        assert card["skus"] == []
        assert "tag=sample-20" in card["purchase_url"]
        assert not hasattr(api, "search"), "Rainforest adapter must not be a query-time search provider"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_catalog_sync_deduplicates_asins_and_publishes_one_snapshot():
    class Api:
        def __init__(self):
            self.calls = []

        async def search_page(self, term, *, page, category_id=None):
            self.calls.append((term, page, category_id))
            if term == "camping gear":
                return [search_result(), search_result(asin="B012345679")]
            return [search_result(asin=ASIN), search_result(asin="B012345680")]

        to_external_card = staticmethod(RainforestApiProvider.to_external_card)

    class Index:
        def __init__(self):
            self.products = None

        async def replace_snapshot(self, products, embedder):
            self.products = products
            return {"reused_vectors": 0, "embedded_products": len(products), "written_products": len(products)}

    api, index = Api(), Index()
    scope = ProductSyncScope("amazon.com", 1, (
        SyncQuery("户外运动", "camping gear"),
        SyncQuery("旅行装备", "travel accessories"),
    ))
    report = await RainforestProductCatalogSync(api, index, object(), scope).sync_once()
    assert api.calls == [("camping gear", 1, None), ("travel accessories", 1, None)]
    assert report["unique_products"] == 3
    assert report["returned_pages"] == 2
    duplicate = next(product for product in index.products if product.asin == ASIN)
    assert duplicate.category == "户外运动"
    assert duplicate.search_term == "camping gear, travel accessories"
    assert duplicate.price_major == 19.99
    assert duplicate.synced_at


@pytest.mark.asyncio
async def test_catalog_sync_walks_configured_pages_for_each_keyword():
    class Api:
        def __init__(self):
            self.calls = []

        async def search_page(self, term, *, page, category_id=None):
            self.calls.append((term, page))
            return [search_result(asin=f"B01234567{page}")]

        to_external_card = staticmethod(RainforestApiProvider.to_external_card)

    class Index:
        products = []

        async def replace_snapshot(self, products, embedder):
            self.products = products
            return {"reused_vectors": 0, "embedded_products": len(products), "written_products": len(products)}

    api, index = Api(), Index()
    syncer = RainforestProductCatalogSync(
        api, index, object(), ProductSyncScope("amazon.com", 2, (SyncQuery("户外运动", "camping gear"),)),
    )
    report = await syncer.sync_once()

    assert api.calls == [("camping gear", 1), ("camping gear", 2)]
    assert report["returned_pages"] == 2
    assert {product.asin for product in index.products} == {"B012345671", "B012345672"}


@pytest.mark.asyncio
async def test_failed_category_fetch_does_not_publish_partial_catalog():
    class Api:
        calls = 0
        to_external_card = staticmethod(RainforestApiProvider.to_external_card)

        async def search_page(self, term, *, page, category_id=None):
            self.calls += 1
            if self.calls == 2:
                raise RainforestError("temporary failure")
            return [search_result()]

    class Index:
        called = False

        async def replace_snapshot(self, products, embedder):
            self.called = True
            return {}

    index = Index()
    syncer = RainforestProductCatalogSync(
        Api(), index, object(), ProductSyncScope("amazon.com", 1,
            (SyncQuery("户外运动", "camping gear"), SyncQuery("家居生活", "home organization"))),
    )
    with pytest.raises(RainforestError, match="temporary failure"):
        await syncer.sync_once()
    assert not index.called


@pytest.mark.asyncio
async def test_product_lookup_refreshes_favorite_and_uses_short_in_memory_cache():
    now = [0.0]
    calls = []

    async def handler(request):
        params = dict(request.url.params)
        calls.append(params)
        assert params["type"] == "product"
        assert params["asin"] == ASIN
        return httpx.Response(200, json={
            "request_info": {"success": True},
            "product": {
                "asin": ASIN,
                "title": "Fresh backpack title",
                "link": f"https://www.amazon.com/dp/{ASIN}?tag=sample-20",
                "main_image": {"link": "https://m.media-amazon.com/images/fresh.jpg"},
                "buybox_winner": {"price": {"value": 24.5, "currency": "USD"}},
            },
        })

    api, client = provider(handler, clock=lambda: now[0])
    try:
        first = await api.get_items([ASIN])
        now[0] = 3599
        second = await api.get_items([ASIN])
        assert first == second
        assert len(calls) == 1
        card = api.to_external_card(first[0])
        assert card["title"] == "Fresh backpack title"
        assert card["price_major"] == 24.5
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_retries_short_retry_after_and_reports_long_delay():
    calls = 0
    waits = []

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"request_info": {"success": True}, "search_results": []})

    api, client = provider(handler, sleep=lambda seconds: _record_wait(waits, seconds))
    try:
        assert await api.search_page("camera") == []
        assert calls == 2
        assert waits == [2.0]
    finally:
        await client.aclose()

    async def long_limit(request):
        return httpx.Response(429, headers={"Retry-After": "30"})

    api, client = provider(long_limit)
    try:
        with pytest.raises(RainforestRateLimitError, match="30 秒"):
            await api.search_page("camera")
    finally:
        await client.aclose()


async def _record_wait(waits, seconds):
    waits.append(seconds)


@pytest.mark.asyncio
async def test_auth_failure_and_request_info_errors_are_sanitized():
    async def unauthorized(request):
        return httpx.Response(401)

    api, client = provider(unauthorized, api_key="sensitive-test-key")
    try:
        with pytest.raises(RainforestConfigurationError) as error:
            await api.search_page("camera")
        assert "sensitive-test-key" not in str(error.value)
    finally:
        await client.aclose()

    async def exhausted(request):
        return httpx.Response(200, json={"request_info": {"success": False, "message": "insufficient credits"}})

    api, client = provider(exhausted)
    try:
        with pytest.raises(RainforestConfigurationError, match="额度不足"):
            await api.search_page("camera")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_api_key_fails_before_network_and_empty_pages_are_empty():
    async def forbidden_network(request):
        raise AssertionError("未配置凭据时不应发起请求")

    api, client = provider(forbidden_network, api_key="")
    try:
        with pytest.raises(RainforestConfigurationError, match="RAINFOREST_API_KEY"):
            await api.search_page("camera")
    finally:
        await client.aclose()

    api, client = provider(lambda request: httpx.Response(200, json={"request_info": {"success": True}}))
    try:
        assert await api.search_page("no results") == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_indexed_search_filters_snapshot_prices_without_calling_rainforest():
    now = "2026-10-11T02:00:00+00:00"
    snapshots = [
        AmazonProductSnapshot("amazon.com", ASIN, "Lightweight Hiking Backpack", "Trail Maker", "户外运动",
            ["Lightweight design"], 19.99, "USD", {"average": 4.6, "review_count": 123}, None,
            f"https://www.amazon.com/dp/{ASIN}", "camping gear", now),
        AmazonProductSnapshot("amazon.com", "B012345679", "Large Hiking Backpack", "Trail Maker", "户外运动",
            [], 49.99, "USD", None, None, "https://www.amazon.com/dp/B012345679", "camping gear", now),
    ]

    class Index:
        async def is_ready(self):
            return True

        async def search(self, embedding, *, top_n, category=None, price_max_usd=None):
            assert embedding == [1.0, 0.0]
            assert category == "户外运动"
            assert price_max_usd == 25.0
            return [(0.9, snapshots[0].to_payload("identity"))]

        async def get_by_asins(self, asins):
            return []

    class Embedding:
        async def embed(self, query):
            assert query == "lightweight hiking backpack"
            return [1.0, 0.0]


    spec = ProductSearchSpec(normalized_query="lightweight hiking backpack", category="户外运动",
        top_k=5, price_max_major=25, target_currency="USD")
    result = await AmazonIndexedSearchProvider(Index(), Embedding()).search(spec)
    assert result["hits"][0]["asin"] == ASIN
    assert result["hits"][0]["synced_at"] == now
    assert result["price_filter_applied"] is True
    assert result["recall_strategy"] == "rainforest_qdrant_vector"


@pytest.mark.asyncio
async def test_indexed_asin_lookup_applies_category_and_usd_price_filters():
    payloads = [
        AmazonProductSnapshot("amazon.com", ASIN, "Backpack", "Trail Maker", "户外运动", [], 19.99,
            "USD", None, None, f"https://www.amazon.com/dp/{ASIN}", "camping gear", "2026-10-11T02:00:00+00:00")
            .to_payload("identity"),
        AmazonProductSnapshot("amazon.com", "B012345679", "Expensive Backpack", "Trail Maker", "户外运动", [], 49.99,
            "USD", None, None, "https://www.amazon.com/dp/B012345679", "camping gear", "2026-10-11T02:00:00+00:00")
            .to_payload("identity"),
        AmazonProductSnapshot("amazon.com", "B012345680", "Home Bag", "Maker", "家居生活", [], 9.99,
            "USD", None, None, "https://www.amazon.com/dp/B012345680", "home organization", "2026-10-11T02:00:00+00:00")
            .to_payload("identity"),
    ]

    class Index:
        async def get_by_asins(self, asins):
            assert asins == [ASIN]
            return payloads

    provider = AmazonIndexedSearchProvider(Index(), object())
    matching = await provider.get_items([ASIN.lower()], category="户外运动", price_max_usd=25)
    assert [item["asin"] for item in matching] == [ASIN]


@pytest.mark.asyncio
async def test_exact_asin_search_reads_index_and_does_not_call_rainforest():
    snapshot = AmazonProductSnapshot("amazon.com", ASIN, "Backpack", "Trail Maker", "户外运动", [], 19.99,
        "USD", None, None, f"https://www.amazon.com/dp/{ASIN}", "camping gear", "2026-10-11T02:00:00+00:00")

    class Index:
        async def get_by_asins(self, asins):
            assert asins == [ASIN]
            return [snapshot.to_payload("identity")]

    class Rainforest:
        async def get_items(self, asins):
            raise AssertionError("商品搜索不得调用 Rainforest 商品详情 API")

    provider = AmazonIndexedSearchProvider(Index(), object())
    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus(),
        external_provider=Rainforest(), provider_mode="rainforest_index", product_provider=provider,
    )
    result = await tool(product_id=f"amazon:{ASIN}", category="户外运动", price_max_major=25, target_currency="USD")
    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["asin"] == ASIN
    assert payload["price_filter_applied"] is True


@pytest.mark.asyncio
async def test_external_search_tool_uses_index_provider_and_never_saves_local_evidence():
    class Api:
        async def search(self, spec):
            raise AssertionError("query-time Rainforest search must not be called")

    snapshot = AmazonProductSnapshot("amazon.com", ASIN, "Lightweight Hiking Backpack", "Trail Maker", "户外运动",
        ["Lightweight design"], 19.99, "USD", None, None, f"https://www.amazon.com/dp/{ASIN}",
        "camping gear", "2026-10-11T02:00:00+00:00")

    class IndexProvider:
        async def search(self, spec):
            return {"hits": [snapshot.to_card(0.9)], "recall_strategy": "rainforest_qdrant_vector",
                "total_candidates": 1, "rerank_applied": False, "filtered_out": [], "notes": "同步快照。"}

        @staticmethod
        def to_external_card(item, *, category_hint=None):
            return AmazonProductSnapshot.from_payload(item).to_card()

    class Evidence:
        async def save(self, *args):
            raise AssertionError("Amazon 商品不能进入本地长期上下文证据")

    bus = TradeEventBus()
    queue = bus.subscribe("rainforest-session")
    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), bus, Evidence(),
        external_provider=Api(), provider_mode="rainforest_index", product_provider=IndexProvider(),
    )
    token = ShoppingContext.set(ShoppingContextSnapshot(
        shopping_session_id="rainforest-session", buyer_id="buyer-1", locale="zh-CN", currency="USD",
    ))
    try:
        result = await tool(normalized_query="lightweight hiking backpack")
    finally:
        ShoppingContext.reset(token)
    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["source_platform"] == "amazon_rainforest"
    assert payload["hits"][0]["synced_at"] == "2026-10-11T02:00:00+00:00"
    assert "result_ref" not in payload
    assert queue.qsize() == 2
    queue.get_nowait()
    event = queue.get_nowait()
    assert event.payload["source_platform"] == "amazon_rainforest"
    with pytest.raises(ValueError, match="只能前往 Amazon"):
        await _verified_order_items(None, "buyer-1", "rainforest-session", [
            {"product_id": f"amazon:{ASIN}", "quantity": 1},
        ])


def test_rainforest_tool_docstring_builds_agentscope_schema():
    function = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus(),
        external_provider=object(), provider_mode="rainforest_index",
    )
    tool = FunctionTool(function, is_read_only=True)
    assert "normalized_query" in tool.input_schema["properties"]
    assert "同步索引" in function.__doc__


@pytest.mark.asyncio
async def test_favorite_store_keeps_only_rainforest_source_and_asin(tmp_path):
    store = BuyerFavoriteStore(tmp_path / "favorites.sqlite")
    card = RainforestApiProvider.to_external_card(search_result())
    await store.save("buyer-1", {**card, "description": "transient product details"})
    saved = (await store.list("buyer-1"))[0]
    assert saved == {
        "product_id": f"amazon:{ASIN}", "source_platform": "amazon_rainforest",
        "purchase_mode": "external", "asin": ASIN,
    }


def test_rainforest_settings_are_server_side_and_secret_is_not_in_repr(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCT_SEARCH_PROVIDER", "rainforest_index")
    monkeypatch.setenv("RAINFOREST_API_KEY", "sensitive-test-key")
    monkeypatch.setenv("RAINFOREST_AMAZON_DOMAIN", "amazon.com")
    monkeypatch.setenv("RAINFOREST_ASSOCIATE_ID", "sample-20")
    settings = load_settings(require_llm_key=False)
    assert settings.product_search_provider == "rainforest_index"
    assert settings.amazon_product_collection == "globex_amazon_products"
    assert settings.rainforest_api_key == "sensitive-test-key"
    assert "sensitive-test-key" not in repr(settings)


def test_local_fixture_provider_is_rejected_at_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCT_SEARCH_PROVIDER", "local_fixture")
    with pytest.raises(ValueError, match="仅支持 rainforest_index"):
        load_settings(require_llm_key=False)


def test_non_us_amazon_domain_is_rejected_for_this_index(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCT_SEARCH_PROVIDER", "rainforest_index")
    monkeypatch.setenv("RAINFOREST_AMAZON_DOMAIN", "amazon.co.uk")
    with pytest.raises(ValueError, match="仅支持 Amazon.com"):
        load_settings(require_llm_key=False)


def test_sync_scope_config_loads_four_bounded_queries():
    from app.application.usecases.rainforest_catalog_sync import load_sync_scope
    from app.infrastructure.settings import PROJECT_ROOT
    scope = load_sync_scope(PROJECT_ROOT / "configs" / "product_sync.yaml")
    assert scope.amazon_domain == "amazon.com"
    assert scope.max_pages_per_query == 1
    assert [query.search_term for query in scope.queries] == [
        "camping gear", "home organization", "travel accessories", "phone accessories",
    ]
