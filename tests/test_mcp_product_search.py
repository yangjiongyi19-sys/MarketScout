import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from app.application.tools.product_search_tool import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.mcp_product_search import MCPProductSearchError, MCPProductSearchProvider
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.settings import load_settings
from app.infrastructure.prompt_registry import toolset_contract
from app import composition


def settings(**overrides):
    defaults = {
        "mcp_product_search_tool": "search_products",
        "mcp_product_search_url": "https://mcp.example.com/mcp",
        "mcp_product_search_token": "",
        "mcp_product_search_timeout_seconds": 1.0,
        "mcp_product_allowed_domains": ("shop.example",),
    }
    return SimpleNamespace(**(defaults | overrides))


def test_provider_selection_and_adapter_are_bound_to_prompt_tool_contract():
    project_root = Path(__file__).resolve().parents[1]
    default_contract = toolset_contract(project_root, product_search_provider="rainforest_index")
    mcp_contract = toolset_contract(project_root, product_search_provider="mcp")
    assert default_contract != mcp_contract
    assert mcp_contract["product_search_provider"] == "mcp"
    source_files = {path.replace("\\", "/") for path in mcp_contract["source_files"]}
    assert "app/infrastructure/mcp_product_search.py" in source_files


@pytest.mark.asyncio
async def test_mcp_startup_does_not_require_amazon_vector_index(monkeypatch):
    calls = []

    class MCPProvider:
        async def startup(self):
            calls.append("mcp")

    class VectorIndex:
        async def is_ready(self):
            raise AssertionError("MCP mode must not probe the Amazon index")

    async def bootstrap_knowledge(_knowledge_base):
        calls.append("knowledge")

    monkeypatch.setattr(composition, "bootstrap_category_knowledge", bootstrap_knowledge)
    container = composition.Container(
        settings=SimpleNamespace(product_search_provider="mcp"),
        bus=None, orchestrator=None, cache=None, semantic_cache=None,
        task_queue=None, backplane=None, query_order=None, cancel_order=None,
        product_repo=None, embedder=None, vector_index=VectorIndex(),
        knowledge_base=object(), db_engine=None, mcp_product_provider=MCPProvider(),
    )

    await container.startup()

    assert calls == ["mcp", "knowledge"]


def mcp_payload():
    return {
        "hits": [
            {
                "product_id": "sku-1",
                "source_platform": "shop",
                "title": "USB-C Travel Adapter",
                "price_major": 80,
                "currency": "USD",
                "purchase_url": "https://www.shop.example/products/sku-1",
                "updated_at": "2026-10-12T10:00:00Z",
                "highlights": ["Compact"],
            },
            {
                "product_id": "sku-2",
                "source_platform": "shop",
                "title": "International Travel Plug",
                "price_major": 90,
                "currency": "EUR",
                "purchase_url": "https://shop.example/products/sku-2",
            },
            {
                "product_id": "sku-3",
                "source_platform": "shop",
                "title": "Untrusted link",
                "price_major": 5,
                "currency": "USD",
                "purchase_url": "https://attacker.example/item",
            },
        ],
    }


class FakeTool:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else mcp_payload()
        self.arguments = None

    async def call(self, **kwargs):
        self.arguments = kwargs
        return ToolChunk(
            content=[TextBlock(text=json.dumps(self.payload))],
            state=ToolResultState.RUNNING,
        )


class FakeClient:
    def __init__(self, tool=None, definitions=None):
        self.tool = tool or FakeTool()
        self.definitions = definitions if definitions is not None else [SimpleNamespace(
            name="search_products",
            annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False),
        )]

    async def list_raw_tools(self):
        return self.definitions

    async def get_tool(self, name):
        assert name == "search_products"
        return self.tool

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_provider_filters_same_currency_budget_and_unapproved_urls():
    client = FakeClient()
    provider = MCPProductSearchProvider(settings(), client=client)
    await provider.startup()

    result = await provider.search(SimpleNamespace(
        normalized_query="travel adapter", category="数码配件", ship_to="US", top_k=5,
        price_max_major=75, target_currency="USD", excluded_material_tags=(), required_material_tags=(),
    ))

    assert [item["product_id"] for item in result["hits"]] == ["mcp:shop:sku-2"]
    assert result["hits"][0]["purchase_url"].startswith("https://shop.example/")
    assert result["price_filter_applied"] is False
    assert result["notes"]
    assert client.tool.arguments["normalized_query"] == "travel adapter"
    assert "buyer_id" not in client.tool.arguments


@pytest.mark.asyncio
async def test_provider_requires_named_tool_and_json_hits_contract():
    provider = MCPProductSearchProvider(settings(), client=FakeClient(definitions=[]))
    with pytest.raises(MCPProductSearchError, match="未提供配置"):
        await provider.startup()

    unmarked = FakeClient(definitions=[SimpleNamespace(
        name="search_products",
        annotations=SimpleNamespace(readOnlyHint=None, destructiveHint=False),
    )])
    with pytest.raises(MCPProductSearchError, match="只读"):
        await MCPProductSearchProvider(settings(), client=unmarked).startup()

    malformed = MCPProductSearchProvider(settings(), client=FakeClient(FakeTool({"items": []})))
    with pytest.raises(MCPProductSearchError, match="hits 数组"):
        await malformed.search(SimpleNamespace(
            normalized_query="plug", category=None, ship_to=None, top_k=3,
            price_max_major=None, target_currency="USD", excluded_material_tags=(), required_material_tags=(),
        ))


@pytest.mark.asyncio
async def test_product_tool_routes_mcp_ids_as_query_and_keeps_external_projection():
    class Provider:
        async def search(self, spec):
            assert spec.normalized_query == "mcp:shop:sku-2"
            return {
                "hits": [{
                    "product_id": "mcp:shop:sku-2", "source_platform": "shop",
                    "title": "International Travel Plug", "price_major": 90,
                    "currency": "EUR", "purchase_url": "https://shop.example/products/sku-2",
                }],
                "notes": "source note",
                "recall_strategy": "mcp_product_search", "total_candidates": 1,
                "rerank_applied": False, "filtered_out": [],
            }

    tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), TradeEventBus(),
        provider_mode="mcp", product_provider=Provider(),
    )
    token = ShoppingContext.set(ShoppingContextSnapshot("mcp-session", "buyer-1", "zh-CN", "USD"))
    try:
        result = await tool(product_id="mcp:shop:sku-2")
    finally:
        ShoppingContext.reset(token)

    payload = json.loads(result.content[0].text)
    assert payload["hits"][0]["product_id"] == "mcp:shop:sku-2"
    assert payload["hits"][0]["source_platform"] == "shop"


def test_settings_require_secure_mcp_endpoint_and_purchase_domain_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRODUCT_SEARCH_PROVIDER", "mcp")
    monkeypatch.setenv("MCP_PRODUCT_SEARCH_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("MCP_PRODUCT_ALLOWED_DOMAINS", "shop.example,store.example")
    settings = load_settings(require_llm_key=False)
    assert settings.mcp_product_allowed_domains == ("shop.example", "store.example")

    monkeypatch.setenv("MCP_PRODUCT_SEARCH_URL", "http://remote.example/mcp")
    with pytest.raises(ValueError):
        load_settings(require_llm_key=False)

