import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from agentscope.tool import FunctionTool
from agentscope.tool import FunctionTool

from app.application.tools.order_tools import _verified_order_items
from app.application.tools.product_search_tool import build_product_search_tool
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.amazon_creators import (
    AmazonCreatorsConfigurationError,
    AmazonCreatorsError,
    AmazonCreatorsProvider,
    AmazonCreatorsRateLimitError,
)
from app.infrastructure.buyer_favorites import BuyerFavoriteStore
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.presentation.favorites import register_favorite_routes
from app.application.usecases.catalog_search import CatalogSearchUseCase


ASIN = "B012345678"


def item(*, price=19.99, asin=ASIN):
    return {
        "asin": asin,
        "detailPageURL": f"https://www.amazon.com/dp/{asin}?tag=sample-20&linkCode=ogi&th=1",
        "images": {"primary": {"large": {"url": "https://m.media-amazon.com/images/sample.jpg"}}},
        "itemInfo": {
            "title": {"displayValue": "Lightweight Hiking Backpack"},
            "byLineInfo": {"brand": {"displayValue": "Trail Maker"}},
            "features": {"displayValues": ["Lightweight design", "Water resistant"]},
        },
        "offersV2": {"listings": [{"price": {"money": {"amount": price, "currency": "USD"}}}]}
        if price is not None else None,
    }


def provider(handler, *, sleep=None, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if sleep is not None:
        kwargs["sleep"] = sleep
    return AmazonCreatorsProvider(
        credential_id="credential-id",
        credential_secret="credential-secret",
        partner_tag="sample-20",
        client=client,
        **kwargs,
    ), client


def oauth_response(token="token-1", status=200):
    return httpx.Response(status, json={"access_token": token, "expires_in": 3600})


@pytest.mark.asyncio
async def test_search_items_authenticates_maps_card_and_caches_response():
    calls = []

    async def handler(request):
        calls.append(request)
        if request.url.host == "api.amazon.com":
            body = json.loads(request.content)
            assert body["grant_type"] == "client_credentials"
            assert body["scope"] == "creatorsapi::default"
            return oauth_response()
        assert request.url.path == "/catalog/v1/searchItems"
        assert request.headers["authorization"] == "Bearer token-1"
        assert request.headers["x-marketplace"] == "www.amazon.com"
        body = json.loads(request.content)
        assert body["keywords"] == "lightweight hiking backpack"
        assert body["maxPrice"] == 5000
        assert body["itemCount"] == 5
        assert "offersV2.listings.price" in body["resources"]
        return httpx.Response(200, json={"searchResult": {"items": [item()]}})

    api, client = provider(handler)
    try:
        first = await api.search_items("lightweight hiking backpack", item_count=5, max_price_cents=5000)
        second = await api.search_items("lightweight hiking backpack", item_count=5, max_price_cents=5000)
        assert first == second
        assert len(calls) == 2  # OAuth 一次、商品检索一次
        card = api.to_external_card(first[0])
        assert card["product_id"] == f"amazon:{ASIN}"
        assert card["source_platform"] == "amazon_creators"
        assert card["purchase_mode"] == "external"
        assert card["asin"] == ASIN
        assert card["price_major"] == 19.99 and card["currency"] == "USD"
        assert card["skus"] == []
        assert "tag=sample-20" in card["purchase_url"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_offer_maps_without_fabricating_price_and_keeps_day_cache():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.host == "api.amazon.com":
            return oauth_response()
        return httpx.Response(200, json={"searchResult": {"items": [item(price=None)]}})

    api, client = provider(handler)
    try:
        raw = await api.search_items("lightweight bag")
        card = api.to_external_card(raw[0])
        assert card["price_major"] is None
        assert card["purchase_url"].endswith("th=1")
        assert len(await api.search_items("lightweight bag")) == 1
        assert calls.count("/catalog/v1/searchItems") == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_offer_cache_expires_after_one_hour_and_non_offer_resource_after_one_day():
    now = [0.0]
    calls = {"offer": 0, "metadata": 0}

    async def handler(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        query = json.loads(request.content)["keywords"]
        calls[query] += 1
        data = item(price=12.0) if query == "offer" else item(price=None)
        return httpx.Response(200, json={"searchResult": {"items": [data]}})

    api, client = provider(handler, clock=lambda: now[0])
    try:
        await api.search_items("offer")
        await api.search_items("metadata")
        now[0] = 3599
        await api.search_items("offer")
        await api.search_items("metadata")
        assert calls == {"offer": 1, "metadata": 1}
        now[0] = 3601
        await api.search_items("offer")
        await api.search_items("metadata")
        assert calls == {"offer": 2, "metadata": 1}
        now[0] = 86401
        await api.search_items("metadata")
        assert calls == {"offer": 2, "metadata": 2}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unauthorized_item_call_refreshes_expired_token_once():
    oauth_count = 0
    item_count = 0

    async def handler(request):
        nonlocal oauth_count, item_count
        if request.url.host == "api.amazon.com":
            oauth_count += 1
            return oauth_response(f"token-{oauth_count}")
        item_count += 1
        if item_count == 1:
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"searchResult": {"items": [item()]}})

    api, client = provider(handler)
    try:
        assert len(await api.search_items("backpack")) == 1
        assert (oauth_count, item_count) == (2, 2)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_honors_short_retry_after_and_reports_long_delay():
    sleeps = []
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.url.host == "api.amazon.com":
            return oauth_response()
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"searchResult": {"items": []}})

    api, client = provider(handler, sleep=lambda seconds: _record_sleep(sleeps, seconds))
    try:
        assert await api.search_items("backpack") == []
        assert sleeps == [2.0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_oauth_rate_limit_honors_short_retry_after_and_reports_long_delay():
    sleeps = []
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.url.host != "api.amazon.com":
            raise AssertionError("OAuth 限流测试不应请求商品接口")
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return oauth_response()

    api, client = provider(handler, sleep=lambda seconds: _record_sleep(sleeps, seconds))
    try:
        assert await api._access_token() == "token-1"
        assert sleeps == [2.0]
        assert calls == 2
    finally:
        await client.aclose()

    async def long_retry(request):
        return httpx.Response(429, headers={"Retry-After": "30"})

    api, client = provider(long_retry, sleep=lambda seconds: _record_sleep(sleeps, seconds))
    try:
        with pytest.raises(AmazonCreatorsRateLimitError, match="30 秒后重试"):
            await api._access_token()
    finally:
        await client.aclose()

    async def long_retry(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        return httpx.Response(429, headers={"Retry-After": "30"})

    api, client = provider(long_retry, sleep=lambda seconds: _record_sleep(sleeps, seconds))
    try:
        with pytest.raises(AmazonCreatorsRateLimitError, match="30 秒后重试"):
            await api.search_items("boots")
    finally:
        await client.aclose()


async def _record_sleep(calls, seconds):
    calls.append(seconds)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(403, "准入"), (500, "暂时异常")])
async def test_api_errors_are_classified(status, expected):
    async def handler(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        return httpx.Response(status)

    api, client = provider(handler)
    try:
        with pytest.raises(AmazonCreatorsError, match=expected):
            await api.search_items("backpack")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_oauth_rejects_bad_credentials_without_leaking_response_body():
    async def handler(request):
        if request.url.host == "api.amazon.com":
            return httpx.Response(401, json={"error": "invalid_client", "secret": "do-not-copy"})
        raise AssertionError("invalid OAuth response must stop the request")

    api, client = provider(handler)
    try:
        with pytest.raises(AmazonCreatorsConfigurationError, match="凭据无效") as error:
            await api.search_items("headphones")
        assert "do-not-copy" not in str(error.value)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_product_request_timeout_is_a_readable_service_error():
    async def handler(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        raise httpx.ReadTimeout("private timeout details", request=request)

    api, client = provider(handler)
    try:
        with pytest.raises(AmazonCreatorsError, match="请求超时"):
            await api.search_items("wireless headphones")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_usd_budget_does_not_apply_static_exchange_rate_filter():
    async def handler(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        body = json.loads(request.content)
        assert "maxPrice" not in body
        return httpx.Response(200, json={"searchResult": {"items": [item(price=799.0)]}})

    api, client = provider(handler)
    try:
        result = await api.search(SimpleNamespace(
            normalized_query="camera", category=None, top_k=5, price_max_major=3000,
            target_currency="CNY", ship_to="CN", required_material_tags=[], excluded_material_tags=[],
        ))
        assert result["hits"][0]["price_major"] == 799.0
        assert result["price_filter_applied"] is False
        assert "未作硬过滤" in result["notes"]
        assert "未核验是否配送至 CN" in result["notes"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_credentials_fail_without_network_or_fixture_fallback():
    async def handler(request):
        raise AssertionError("未配置凭据时不应发网络请求")

    api, client = provider(handler)
    api._credential_secret = ""
    try:
        with pytest.raises(AmazonCreatorsConfigurationError, match="尚未配置"):
            await api.search_items("backpack")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_search_tool_uses_external_provider_without_local_evidence_or_fixture():
    class External:
        async def search(self, spec):
            assert spec.normalized_query == "lightweight hiking backpack"
            assert spec.price_max_major == 75
            return {
                "hits": [api_card()], "recall_strategy": "amazon_creators_search_items",
                "total_candidates": 1, "rerank_applied": False, "filtered_out": [],
                "notes": "USD 报价；配送情况待 Amazon 确认。", "price_filter_applied": True,
            }

    class Evidence:
        async def save(self, *args):
            raise AssertionError("Amazon 商品不应进入长期上下文证据")

    bus = TradeEventBus()
    queue = bus.subscribe("amazon-session")
    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), bus, Evidence(),
        external_provider=External(), provider_mode="amazon_creators",
    )
    token = ShoppingContext.set(ShoppingContextSnapshot(
        shopping_session_id="amazon-session", buyer_id="buyer-1", locale="zh-CN", currency="USD",
    ))
    try:
        result = await tool(normalized_query="lightweight hiking backpack", price_max_major=75, target_currency="USD")
    finally:
        ShoppingContext.reset(token)
    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["purchase_mode"] == "external"
    assert payload["hits"][0]["asin"] == ASIN
    assert "result_ref" not in payload
    assert queue.qsize() == 2
    event = queue.get_nowait()
    event = queue.get_nowait()
    assert event.payload["hits"][0]["source_platform"] == "amazon_creators"


@pytest.mark.asyncio
async def test_search_tool_supports_exact_asin_without_local_sku():
    class External:
        async def get_items(self, asins):
            assert asins == [ASIN]
            return [item()]

        @staticmethod
        def to_external_card(value, *, category_hint=None):
            return AmazonCreatorsProvider.to_external_card(value, category_hint=category_hint)

    bus = TradeEventBus()
    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), bus,
        external_provider=External(), provider_mode="amazon_creators",
    )
    token = ShoppingContext.set(ShoppingContextSnapshot(
        shopping_session_id="amazon-asin-session", buyer_id="buyer-1", locale="zh-CN", currency="USD",
    ))
    try:
        result = await tool(product_id=ASIN)
    finally:
        ShoppingContext.reset(token)
    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["asin"] == ASIN
    assert payload["recall_strategy"] == "amazon_creators_asin_lookup"


def test_amazon_tool_docstring_still_builds_agentscope_schema():
    function = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus(),
        external_provider=object(), provider_mode="amazon_creators",
    )
    tool = FunctionTool(function, is_read_only=True)
    assert "normalized_query" in tool.input_schema["properties"]


@pytest.mark.asyncio
async def test_search_tool_supports_exact_asin_without_local_sku():
    class External:
        async def get_items(self, asins):
            assert asins == [ASIN]
            return [item()]

        @staticmethod
        def to_external_card(value, *, category_hint=None):
            return AmazonCreatorsProvider.to_external_card(value, category_hint=category_hint)

    bus = TradeEventBus()
    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), bus,
        external_provider=External(), provider_mode="amazon_creators",
    )
    token = ShoppingContext.set(ShoppingContextSnapshot(
        shopping_session_id="amazon-asin-session", buyer_id="buyer-1", locale="zh-CN", currency="USD",
    ))
    try:
        result = await tool(product_id=ASIN)
    finally:
        ShoppingContext.reset(token)
    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["asin"] == ASIN
    assert payload["recall_strategy"] == "amazon_creators_asin_lookup"


def test_amazon_tool_docstring_still_builds_agentscope_schema():
    function = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus(),
        external_provider=object(), provider_mode="amazon_creators",
    )
    tool = FunctionTool(function, is_read_only=True)
    assert "normalized_query" in tool.input_schema["properties"]


def api_card():
    return {
        "product_id": f"amazon:{ASIN}", "source_platform": "amazon_creators", "purchase_mode": "external",
        "asin": ASIN, "title": "Lightweight Hiking Backpack", "brand": "Trail Maker",
        "price_major": 19.99, "currency": "USD", "highlights": [], "skus": [],
        "purchase_url": f"https://www.amazon.com/dp/{ASIN}?tag=sample-20",
    }


@pytest.mark.asyncio
async def test_external_product_cannot_be_added_to_local_order():
    with pytest.raises(ValueError, match="只能前往 Amazon"):
        await _verified_order_items(None, "buyer-1", "session-1", [
            {"product_id": f"amazon:{ASIN}", "quantity": 1},
        ])


@pytest.mark.asyncio
async def test_favorite_store_keeps_only_external_platform_and_asin(tmp_path):
    store = BuyerFavoriteStore(tmp_path / "favorites.sqlite")
    await store.save("buyer-1", {
        **api_card(), "description": "transient product details", "image_url": "https://m.media-amazon.com/image.jpg",
    })
    saved = (await store.list("buyer-1"))[0]
    assert saved == {
        "product_id": f"amazon:{ASIN}", "source_platform": "amazon_creators",
        "purchase_mode": "external", "asin": ASIN,
    }


@pytest.mark.asyncio
async def test_favorite_refresh_fetches_asin_details_and_returns_fresh_offer():
    async def handler(request):
        if request.url.host == "api.amazon.com":
            return oauth_response()
        assert request.url.path == "/catalog/v1/getItems"
        body = json.loads(request.content)
        assert body["itemIds"] == [ASIN]
        assert body["itemIdType"] == "ASIN"
        return httpx.Response(200, json={"itemsResult": {"items": [item()]}})

    api, client = provider(handler)
    try:
        refreshed = await api.refresh_favorites([{"asin": ASIN}])
        assert refreshed[0]["asin"] == ASIN
        assert refreshed[0]["price_major"] == 19.99
        assert refreshed[0]["purchase_url"].endswith("tag=sample-20&linkCode=ogi&th=1")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_favorite_api_persists_only_asin_and_refreshes_before_display(tmp_path):
    api = FastAPI()
    store = BuyerFavoriteStore(tmp_path / "favorites.sqlite")

    class Refresher:
        async def refresh_favorites(self, products):
            assert products == [{
                "product_id": f"amazon:{ASIN}", "source_platform": "amazon_creators",
                "purchase_mode": "external", "asin": ASIN,
            }]
            return [{**api_card(), "title": "Fresh Amazon title"}]

    register_favorite_routes(api, lambda: store, lambda: Refresher())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        response = await client.put(
            f"/commerce/favorites/amazon:{ASIN}?buyer_id=buyer-1",
            json={"product": {**api_card(), "description": "must not persist"}},
        )
        assert response.status_code == 200
        assert response.json()["products"][0]["title"] == "Fresh Amazon title"
    saved = (await store.list("buyer-1"))[0]
    assert set(saved) == {"product_id", "source_platform", "purchase_mode", "asin"}


@pytest.mark.asyncio
async def test_invalid_purchase_link_is_not_returned():
    external = item()
    external["detailPageURL"] = "https://attacker.example/product"
    assert AmazonCreatorsProvider.to_external_card(external)["purchase_url"] is None
