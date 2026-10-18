# -*- coding: utf-8 -*-
"""Amazon.com Creators API 客户端与外部商品映射。"""
from __future__ import annotations

import asyncio
import copy
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from app.domain.catalog.ports.product_search_provider import ProductSearchProvider


API_BASE = "https://creatorsapi.amazon"
TOKEN_ENDPOINTS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
RESOURCE_FIELDS = [
    "images.primary.large",
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "offersV2.listings.price",
]


class AmazonCreatorsError(RuntimeError):
    """可安全展示给用户的 Amazon 检索错误。"""


class AmazonCreatorsConfigurationError(AmazonCreatorsError):
    pass


class AmazonCreatorsRateLimitError(AmazonCreatorsError):
    pass


class AmazonCreatorsProvider(ProductSearchProvider):
    """使用官方 Creators API；不抓取网页、不将商品写入向量库。"""

    def __init__(
        self,
        *,
        credential_id: str,
        credential_secret: str,
        credential_version: str = "3.1",
        partner_tag: str,
        marketplace: str = "www.amazon.com",
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self._credential_id = credential_id.strip()
        self._credential_secret = credential_secret.strip()
        self._credential_version = credential_version.strip()
        self._partner_tag = partner_tag.strip()
        self.marketplace = marketplace.strip()
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))
        self._owns_client = client is None
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._search_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._item_cache: dict[str, tuple[float, dict]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._credential_id and self._credential_secret and self._partner_tag)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _require_configuration(self) -> str:
        if not self._credential_id or not self._credential_secret:
            raise AmazonCreatorsConfigurationError(
                "Amazon Creators API 尚未配置 Credential ID/Secret，请在后端环境变量中设置后重启服务"
            )
        if not self._partner_tag:
            raise AmazonCreatorsConfigurationError(
                "Amazon Creators API 尚未配置美国站 Partner Tag，请设置 AMAZON_US_PARTNER_TAG"
            )
        if self.marketplace != "www.amazon.com":
            raise AmazonCreatorsConfigurationError("当前接入仅支持 Amazon.com（www.amazon.com）")
        endpoint = TOKEN_ENDPOINTS.get(self._credential_version)
        if endpoint is None:
            raise AmazonCreatorsConfigurationError("AMAZON_CREATORS_CREDENTIAL_VERSION 仅支持 3.1、3.2 或 3.3")
        return endpoint

    async def _oauth_response(self, endpoint: str) -> httpx.Response:
        """获取 OAuth 响应，并按短 Retry-After 重试一次。"""
        for attempt in range(2):
            try:
                response = await self._client.post(
                    endpoint,
                    headers={"Content-Type": "application/json"},
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self._credential_id,
                        "client_secret": self._credential_secret,
                        "scope": "creatorsapi::default",
                    },
                )
            except httpx.TimeoutException as error:
                raise AmazonCreatorsError("Amazon OAuth 请求超时，请稍后重试") from error
            except httpx.HTTPError as error:
                raise AmazonCreatorsError("Amazon OAuth 服务暂时不可用，请稍后重试") from error
            if response.status_code != 429:
                return response
            wait_seconds = self._retry_after(response)
            if attempt == 0 and wait_seconds <= 3:
                await self._sleep(wait_seconds)
                continue
            raise AmazonCreatorsRateLimitError(
                f"Amazon OAuth 触发限流，请在 {max(1, int(wait_seconds + 0.999))} 秒后重试"
            )
        raise AmazonCreatorsRateLimitError("Amazon OAuth 触发限流，请稍后重试")

    async def _access_token(self, *, force: bool = False) -> str:
        endpoint = self._require_configuration()
        async with self._token_lock:
            if not force and self._token and self._clock() < self._token_expires_at - 60:
                return self._token
            response = await self._oauth_response(endpoint)
            if response.status_code in (400, 401, 403):
                self._token = None
                raise AmazonCreatorsConfigurationError("Amazon Creators API 凭据无效或尚未获准访问，请检查账户准入与凭据版本")
            if response.status_code >= 500:
                raise AmazonCreatorsError("Amazon OAuth 服务异常，请稍后重试")
            if response.status_code >= 400:
                raise AmazonCreatorsError(f"Amazon OAuth 请求失败（HTTP {response.status_code}）")
            try:
                body = response.json()
                token = body["access_token"]
                expires_in = max(60, int(body.get("expires_in", 3600)))
            except (ValueError, KeyError, TypeError) as error:
                raise AmazonCreatorsError("Amazon OAuth 响应格式无效") from error
            if not isinstance(token, str) or not token:
                raise AmazonCreatorsError("Amazon OAuth 响应缺少访问令牌")
            self._token = token
            self._token_expires_at = self._clock() + expires_in
            return token

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After", "").strip()
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                try:
                    target = parsedate_to_datetime(raw)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return 1.0

    async def _post(self, path: str, body: dict) -> dict:
        token = await self._access_token()
        for attempt in range(2):
            try:
                response = await self._client.post(
                    f"{API_BASE}/catalog/v1/{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "x-marketplace": self.marketplace,
                    },
                    json=body,
                )
            except httpx.TimeoutException as error:
                raise AmazonCreatorsError("Amazon 商品服务请求超时，请稍后重试") from error
            except httpx.HTTPError as error:
                raise AmazonCreatorsError("Amazon 商品服务暂时不可用，请稍后重试") from error
            if response.status_code == 401 and attempt == 0:
                self._token = None
                token = await self._access_token(force=True)
                continue
            if response.status_code == 403:
                raise AmazonCreatorsConfigurationError("Amazon 拒绝 Creators API 访问，请核对准入状态和美国站 Partner Tag")
            if response.status_code == 401:
                self._token = None
                raise AmazonCreatorsConfigurationError("Amazon Creators API 凭据或访问授权无效，请检查账户准入与凭据")
            if response.status_code == 429:
                wait_seconds = self._retry_after(response)
                if attempt == 0 and wait_seconds <= 3:
                    await self._sleep(wait_seconds)
                    continue
                raise AmazonCreatorsRateLimitError(
                    f"Amazon Creators API 触发限流，请在 {max(1, int(wait_seconds + 0.999))} 秒后重试"
                )
            if response.status_code >= 500:
                raise AmazonCreatorsError("Amazon 商品服务暂时异常，请稍后重试")
            if response.status_code >= 400:
                raise AmazonCreatorsError(f"Amazon 商品检索失败（HTTP {response.status_code}）")
            try:
                result = response.json()
            except ValueError as error:
                raise AmazonCreatorsError("Amazon 商品服务返回了无效数据") from error
            if not isinstance(result, dict):
                raise AmazonCreatorsError("Amazon 商品服务返回了无效数据")
            return result
        raise AmazonCreatorsError("Amazon 商品服务暂时不可用，请稍后重试")

    def _request_body(self, **values) -> dict:
        return {
            "marketplace": self.marketplace,
            "partnerTag": self._partner_tag,
            **values,
        }

    async def search_items(self, keywords: str, *, item_count: int = 10, max_price_cents: int | None = None) -> list[dict]:
        query = keywords.strip()
        if not query:
            raise AmazonCreatorsError("Amazon 检索词不能为空；请先将买家需求整理成简短英文关键词")
        item_count = min(10, max(1, int(item_count)))
        key = (query.casefold(), item_count, max_price_cents, self.marketplace)
        cached = self._search_cache.get(key)
        if cached and cached[0] > self._clock():
            return copy.deepcopy(cached[1])
        body = self._request_body(
            keywords=query,
            searchIndex="All",
            itemCount=item_count,
            resources=RESOURCE_FIELDS,
            currencyOfPreference="USD",
        )
        if max_price_cents is not None:
            body["maxPrice"] = max(1, max_price_cents)
        response = await self._post("searchItems", body)
        items = response.get("searchResult", {}).get("items", [])
        if not isinstance(items, list):
            raise AmazonCreatorsError("Amazon 商品服务返回了无效的搜索结果")
        items = [item for item in items if isinstance(item, dict)]
        ttl = 3600 if any(self._has_offers_resource(item) for item in items) else 86400
        self._search_cache[key] = (self._clock() + ttl, copy.deepcopy(items))
        for item in items:
            asin = item.get("asin")
            if isinstance(asin, str) and ASIN_PATTERN.fullmatch(asin.upper()):
                item_ttl = 3600 if self._has_offers_resource(item) else 86400
                self._item_cache[asin.upper()] = (self._clock() + item_ttl, copy.deepcopy(item))
        return items

    async def get_items(self, asins: list[str]) -> list[dict]:
        normalized = list(dict.fromkeys(asin.strip().upper() for asin in asins if isinstance(asin, str)))
        if any(not ASIN_PATTERN.fullmatch(asin) for asin in normalized):
            raise AmazonCreatorsError("Amazon 商品标识必须是 10 位 ASIN")
        output: dict[str, dict] = {}
        missing = []
        now = self._clock()
        for asin in normalized:
            cached = self._item_cache.get(asin)
            if cached and cached[0] > now:
                output[asin] = copy.deepcopy(cached[1])
            else:
                missing.append(asin)
        for start in range(0, len(missing), 10):
            chunk = missing[start:start + 10]
            if not chunk:
                continue
            response = await self._post(
                "getItems",
                self._request_body(
                    itemIds=chunk,
                    itemIdType="ASIN",
                    resources=RESOURCE_FIELDS,
                    currencyOfPreference="USD",
                ),
            )
            items = response.get("itemsResult", {}).get("items", [])
            if not isinstance(items, list):
                raise AmazonCreatorsError("Amazon 商品服务返回了无效的商品详情")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("asin"), str):
                    continue
                asin = item["asin"].upper()
                if asin not in chunk:
                    continue
                ttl = 3600 if self._has_offers_resource(item) else 86400
                self._item_cache[asin] = (self._clock() + ttl, copy.deepcopy(item))
                output[asin] = copy.deepcopy(item)
        return [output[asin] for asin in normalized if asin in output]

    @staticmethod
    def _display_value(node: Any) -> str | None:
        if not isinstance(node, dict):
            return None
        value = node.get("displayValue")
        return value if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _has_offers_resource(item: dict) -> bool:
        return item.get("offersV2") is not None

    @staticmethod
    def _item_price(item: dict) -> tuple[float, str] | None:
        listings = (item.get("offersV2") or {}).get("listings", [])
        if not isinstance(listings, list):
            return None
        for listing in listings:
            money = ((listing or {}).get("price") or {}).get("money") if isinstance(listing, dict) else None
            amount = money.get("amount") if isinstance(money, dict) else None
            currency = money.get("currency") if isinstance(money, dict) else None
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0 and isinstance(currency, str):
                return float(amount), currency
        return None

    @classmethod
    def to_external_card(cls, item: dict, *, category_hint: str | None = None) -> dict | None:
        asin = item.get("asin")
        title = cls._display_value((item.get("itemInfo") or {}).get("title"))
        if not isinstance(asin, str) or not ASIN_PATTERN.fullmatch(asin.upper()) or not title:
            return None
        item_info = item.get("itemInfo") or {}
        byline = item_info.get("byLineInfo") or {}
        brand = cls._display_value(byline.get("brand"))
        features = (item_info.get("features") or {}).get("displayValues", [])
        highlights = [value for value in features if isinstance(value, str) and value.strip()][:5] if isinstance(features, list) else []
        image = ((item.get("images") or {}).get("primary") or {})
        image_url = next((image.get(size, {}).get("url") for size in ("large", "medium", "small")
                          if isinstance(image.get(size), dict) and isinstance(image[size].get("url"), str)), None)
        price = cls._item_price(item)
        purchase_url = item.get("detailPageURL")
        if isinstance(purchase_url, str):
            from urllib.parse import urlsplit
            parsed = urlsplit(purchase_url)
            if parsed.scheme != "https" or parsed.hostname not in {"www.amazon.com", "amazon.com"}:
                purchase_url = None
        else:
            purchase_url = None
        card = {
            "product_id": f"amazon:{asin.upper()}",
            "source_platform": "amazon_creators",
            "purchase_mode": "external",
            "asin": asin.upper(),
            "title": title,
            "brand": brand,
            "category": category_hint,
            "image_url": image_url,
            "image_alt": title,
            "price_major": price[0] if price and price[1] == "USD" else None,
            "currency": "USD",
            "highlights": highlights,
            "skus": [],
            "purchase_url": purchase_url,
        }
        return card

    async def search(self, spec) -> dict:
        max_price_cents = None
        price_warning = None
        cap = spec.price_max_major
        if cap is not None and spec.target_currency.upper() == "USD":
            max_price_cents = int(float(cap) * 100)
        elif cap is not None:
            price_warning = f"買家预算币种为 {spec.target_currency.upper()}，Amazon 仅提供 USD 报价；预算未作硬过滤，请在 Amazon 确认。"
        raw_items = await self.search_items(
            spec.normalized_query,
            item_count=min(10, max(1, spec.top_k)),
            max_price_cents=max_price_cents,
        )
        hits = []
        for item in raw_items:
            card = self.to_external_card(item, category_hint=spec.category)
            if card is None:
                continue
            if max_price_cents is not None and card["price_major"] is not None and card["price_major"] > float(cap):
                continue
            if max_price_cents is not None and card["price_major"] is None:
                continue
            hits.append(card)
            if len(hits) >= min(10, max(1, spec.top_k)):
                break
        notes = ["商品信息来自 Amazon.com Creators API；价格为 Amazon.com 的 USD 商品报价，不含目的国运费、税费，也不代表可配送到指定国家。"]
        if price_warning:
            notes.append(price_warning)
        if spec.ship_to:
            notes.append(f"未核验是否配送至 {spec.ship_to}，请在 Amazon 商品页确认。")
        if spec.required_material_tags or spec.excluded_material_tags:
            notes.append("Amazon API 未提供项目材质标签的结构化核验；材质条件不能作为已验证硬过滤，请查看商品页规格。")
        return {
            "hits": hits,
            "recall_strategy": "amazon_creators_search_items",
            "total_candidates": len(raw_items),
            "rerank_applied": False,
            "filtered_out": [],
            "notes": " ".join(notes),
            "price_filter_applied": max_price_cents is not None,
        }

    async def refresh_favorites(self, products: list[dict]) -> list[dict]:
        asins = [product["asin"] for product in products if isinstance(product.get("asin"), str)]
        current = {item["asin"].upper(): item for item in await self.get_items(asins)}
        refreshed = []
        for saved in products:
            asin = str(saved.get("asin", "")).upper()
            card = self.to_external_card(current[asin]) if asin in current else None
            if card is None:
                card = {
                    "product_id": f"amazon:{asin}",
                    "source_platform": "amazon_creators",
                    "purchase_mode": "external",
                    "asin": asin,
                    "title": f"Amazon 商品（ASIN: {asin}）",
                    "brand": "",
                    "category": "Amazon 商品",
                    "price_major": None,
                    "currency": "USD",
                    "highlights": [],
                    "skus": [],
                    "purchase_url": None,
                    "unavailable": True,
                }
            refreshed.append(card)
        return refreshed
