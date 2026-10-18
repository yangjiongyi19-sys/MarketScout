# -*- coding: utf-8 -*-
"""Rainforest API 同步客户端与 Amazon.com 商品卡映射。"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx



API_URL = "https://api.rainforestapi.com/request"
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
PRODUCT_CACHE_SECONDS = 3600


class RainforestError(RuntimeError):
    """可安全展示给用户的 Rainforest API 检索错误。"""


class RainforestConfigurationError(RainforestError):
    pass


class RainforestRateLimitError(RainforestError):
    pass


class RainforestApiProvider:
    """从 Rainforest 读取同步页，并按 ASIN 刷新用户显式收藏。"""

    def __init__(
        self,
        *,
        api_key: str,
        amazon_domain: str = "amazon.com",
        associate_id: str = "",
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self._api_key = api_key.strip()
        self.amazon_domain = amazon_domain.strip().lower()
        self._associate_id = associate_id.strip()
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._owns_client = client is None
        self._clock = clock
        self._sleep = sleep
        self._product_cache: dict[str, tuple[float, dict]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _require_configuration(self) -> None:
        if not self._api_key:
            raise RainforestConfigurationError(
                "Rainforest API 尚未配置凭据，请在后端环境变量 RAINFOREST_API_KEY 中设置后重启服务"
            )
        if self.amazon_domain != "amazon.com":
            raise RainforestConfigurationError("当前 Rainforest 接入仅支持 Amazon.com（amazon.com）")

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

    def _params(self, request_type: str, **values: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "api_key": self._api_key,
            "type": request_type,
            "amazon_domain": self.amazon_domain,
        }
        if self._associate_id:
            params["associate_id"] = self._associate_id
        params.update(values)
        return params

    @staticmethod
    def _request_error(response: httpx.Response) -> RainforestError:
        status = response.status_code
        if status in (401, 403):
            return RainforestConfigurationError(
                "Rainforest API 凭据无效或账户当前无访问权限，请检查 Dashboard 中的 API key 与额度状态"
            )
        if status == 429:
            wait_seconds = RainforestApiProvider._retry_after(response)
            return RainforestRateLimitError(
                f"Rainforest API 触发限流，请在 {max(1, int(wait_seconds + 0.999))} 秒后重试"
            )
        if status >= 500:
            return RainforestError("Rainforest API 服务暂时异常，请稍后重试")
        return RainforestError(f"Rainforest API 请求失败（HTTP {status}），请检查参数与账户状态")

    @staticmethod
    def _check_request_info(result: dict) -> None:
        info = result.get("request_info")
        if not isinstance(info, dict) or info.get("success") is not False:
            return
        status = info.get("status_code")
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        message = str(info.get("message", "")).casefold()
        if status in (401, 403) or any(word in message for word in ("api key", "api_key", "unauthorized", "authentication")):
            raise RainforestConfigurationError(
                "Rainforest API 凭据无效或账户当前无访问权限，请检查 Dashboard 中的 API key 与额度状态"
            )
        if status == 429 or "rate limit" in message or "too many request" in message:
            raise RainforestRateLimitError("Rainforest API 触发限流，请稍后重试")
        if "credit" in message or "quota" in message or "balance" in message:
            raise RainforestConfigurationError("Rainforest API 账户额度不足，请检查 Dashboard 中的试用额度或套餐")
        raise RainforestError("Rainforest API 未能完成此次检索，请检查账户状态或稍后重试")

    async def _get(self, params: dict[str, Any]) -> dict:
        self._require_configuration()
        for attempt in range(2):
            try:
                response = await self._client.get(API_URL, params=params)
            except httpx.TimeoutException:
                raise RainforestError("Rainforest API 请求超时，请稍后重试") from None
            except httpx.HTTPError:
                # httpx 异常可能包含完整请求 URL；API key 位于查询参数中，不能向上层传递原异常。
                raise RainforestError("Rainforest API 网络请求失败，请稍后重试") from None
            if response.status_code == 429:
                wait_seconds = self._retry_after(response)
                if attempt == 0 and wait_seconds <= 3:
                    await self._sleep(wait_seconds)
                    continue
                raise self._request_error(response)
            if response.status_code >= 400:
                raise self._request_error(response)
            try:
                result = response.json()
            except ValueError:
                raise RainforestError("Rainforest API 返回了无效 JSON") from None
            if not isinstance(result, dict):
                raise RainforestError("Rainforest API 返回了无效数据")
            self._check_request_info(result)
            return result
        raise RainforestRateLimitError("Rainforest API 触发限流，请稍后重试")

    async def search_page(self, keywords: str, *, page: int = 1, category_id: str | None = None) -> list[dict]:
        """读取 Rainforest 搜索结果的一页，供离线同步器建索引。"""
        query = keywords.strip()
        if not query:
            raise RainforestError("Amazon 同步关键词不能为空")
        if type(page) is not int or not 1 <= page <= 5:
            raise RainforestError("Rainforest 实时搜索分页仅支持第 1 到第 5 页")
        values: dict[str, Any] = {
            "search_term": query,
            "page": page,
            "exclude_sponsored": "true",
        }
        if category_id:
            values["category_id"] = category_id
        result = await self._get(self._params("search", **values))
        items = result.get("search_results", [])
        if not isinstance(items, list):
            raise RainforestError("Rainforest API 返回了无效的搜索结果")
        return [item for item in items if isinstance(item, dict)]

    async def get_items(self, asins: list[str]) -> list[dict]:
        normalized = list(dict.fromkeys(asin.strip().upper() for asin in asins if isinstance(asin, str)))
        if any(not ASIN_PATTERN.fullmatch(asin) for asin in normalized):
            raise RainforestError("Amazon 商品标识必须是 10 位 ASIN")
        output: dict[str, dict] = {}
        now = self._clock()
        for asin in normalized:
            cached = self._product_cache.get(asin)
            if cached and cached[0] > now:
                output[asin] = copy.deepcopy(cached[1])
                continue
            result = await self._get(self._params("product", asin=asin))
            product = result.get("product")
            if not isinstance(product, dict) or str(product.get("asin", "")).upper() != asin:
                continue
            self._product_cache[asin] = (self._clock() + PRODUCT_CACHE_SECONDS, copy.deepcopy(product))
            output[asin] = copy.deepcopy(product)
        return [output[asin] for asin in normalized if asin in output]

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @staticmethod
    def _safe_amazon_url(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme != "https" or parsed.hostname not in {"amazon.com", "www.amazon.com"}:
            return None
        return value

    @classmethod
    def to_external_card(cls, item: dict, *, category_hint: str | None = None) -> dict | None:
        node = item.get("product") if isinstance(item.get("product"), dict) else item
        asin = node.get("asin") or item.get("asin")
        title = node.get("title") or item.get("title")
        if not isinstance(asin, str) or not ASIN_PATTERN.fullmatch(asin.upper()):
            return None
        if not isinstance(title, str) or not title.strip():
            return None

        price = node.get("price")
        if not isinstance(price, dict):
            buybox = node.get("buybox_winner") or {}
            price = buybox.get("price") if isinstance(buybox, dict) else None
        if not isinstance(price, dict):
            price = {}
        currency = str(price.get("currency") or "USD").upper()
        amount = cls._number(price.get("value")) if currency == "USD" else None

        main_image = node.get("main_image") or {}
        image_url = node.get("image")
        if isinstance(main_image, dict):
            image_url = image_url or main_image.get("link")
        if not isinstance(image_url, str) or not image_url.startswith("https://"):
            image_url = None

        link = cls._safe_amazon_url(node.get("link") or item.get("link"))
        features = node.get("feature_bullets") or node.get("features") or []
        if isinstance(features, dict):
            features = features.get("text") or features.get("values") or []
        if isinstance(features, list):
            highlights = [
                (value.get("text") if isinstance(value, dict) else value).strip()
                for value in features
                if isinstance(value, str) or isinstance(value, dict) and isinstance(value.get("text"), str)
            ][:5]
            highlights = [value for value in highlights if value]
        else:
            highlights = []

        card = {
            "product_id": f"amazon:{asin.upper()}",
            "source_platform": "amazon_rainforest",
            "purchase_mode": "external",
            "asin": asin.upper(),
            "title": title.strip(),
            "brand": node.get("brand") if isinstance(node.get("brand"), str) else "",
            "category": category_hint,
            "image_url": image_url,
            "image_alt": title.strip(),
            "price_major": amount,
            "currency": "USD",
            "highlights": highlights,
            "skus": [],
            "purchase_url": link,
        }
        rating = cls._number(node.get("rating"))
        count = node.get("ratings_total")
        if rating is not None and rating <= 5 and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            card["rating_summary"] = {"average": rating, "review_count": count}
            card["rating_is_live"] = True
        return card

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
                    "source_platform": "amazon_rainforest",
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
