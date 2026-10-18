# -*- coding: utf-8 -*-
"""Amazon 平台商品的同步快照模型；不包含 Globex SKU、库存或本地下单字段。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AmazonProductSnapshot:
    amazon_domain: str
    asin: str
    title: str
    brand: str
    category: str
    highlights: list[str]
    price_major: float | None
    currency: str
    rating_summary: dict[str, float | int] | None
    image_url: str | None
    purchase_url: str | None
    search_term: str
    synced_at: str

    @property
    def product_id(self) -> str:
        # 对外 ID 与既有收藏保持兼容；Qdrant point ID 同时包含域名与 ASIN。
        return f"amazon:{self.asin}"

    def searchable_text(self) -> str:
        return " ".join(value for value in (
            self.title, self.brand, self.category, self.search_term, " ".join(self.highlights),
        ) if value).strip()

    def text_hash(self) -> str:
        return hashlib.sha256(self.searchable_text().encode("utf-8")).hexdigest()

    def to_payload(self, embedding_key: str) -> dict[str, Any]:
        return {
            "schema": "amazon-product-snapshot-v1",
            "product_id": self.product_id,
            "amazon_domain": self.amazon_domain,
            "asin": self.asin,
            "source_platform": "amazon_rainforest",
            "purchase_mode": "external",
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "highlights": list(self.highlights),
            "price_major": self.price_major,
            "currency": self.currency,
            "rating_summary": self.rating_summary,
            "rating_is_live": False,
            "image_url": self.image_url,
            "purchase_url": self.purchase_url,
            "search_term": self.search_term,
            "searchable_text": self.searchable_text(),
            "synced_at": self.synced_at,
            "embedding_key": embedding_key,
            "text_hash": self.text_hash(),
            "embedding_schema": "amazon-product-text-v1",
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AmazonProductSnapshot":
        rating = payload.get("rating_summary")
        if not isinstance(rating, dict):
            rating = None
        highlights = payload.get("highlights")
        return cls(
            amazon_domain=str(payload.get("amazon_domain") or "amazon.com"),
            asin=str(payload.get("asin") or "").upper(),
            title=str(payload.get("title") or ""),
            brand=str(payload.get("brand") or ""),
            category=str(payload.get("category") or "Amazon 商品"),
            highlights=[str(value) for value in highlights if isinstance(value, str)] if isinstance(highlights, list) else [],
            price_major=payload.get("price_major") if isinstance(payload.get("price_major"), (int, float)) else None,
            currency=str(payload.get("currency") or "USD"),
            rating_summary=rating,
            image_url=payload.get("image_url") if isinstance(payload.get("image_url"), str) else None,
            purchase_url=payload.get("purchase_url") if isinstance(payload.get("purchase_url"), str) else None,
            search_term=str(payload.get("search_term") or ""),
            synced_at=str(payload.get("synced_at") or ""),
        )

    def to_card(self, score: float | None = None) -> dict[str, Any]:
        card: dict[str, Any] = {
            "product_id": self.product_id,
            "source_platform": "amazon_rainforest",
            "purchase_mode": "external",
            "asin": self.asin,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "image_url": self.image_url,
            "image_alt": self.title,
            "price_major": self.price_major,
            "currency": self.currency,
            "highlights": list(self.highlights),
            "skus": [],
            "purchase_url": self.purchase_url,
            "rating_is_live": False,
            "synced_at": self.synced_at,
            "updated_at": self.synced_at,
        }
        if self.rating_summary is not None:
            card["rating_summary"] = dict(self.rating_summary)
        if score is not None:
            card["score"] = round(float(score), 4)
        return card

    @classmethod
    def from_external_card(
        cls,
        card: dict[str, Any],
        *,
        amazon_domain: str,
        category: str,
        search_term: str,
        synced_at: str,
    ) -> "AmazonProductSnapshot":
        asin = str(card.get("asin") or "").strip().upper()
        title = str(card.get("title") or "").strip()
        if len(asin) != 10 or not title:
            raise ValueError("Amazon 搜索结果缺少有效 ASIN 或商品标题")
        price = card.get("price_major")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            price = None
        rating = card.get("rating_summary")
        if not isinstance(rating, dict):
            rating = None
        highlights = card.get("highlights")
        return cls(
            amazon_domain=amazon_domain,
            asin=asin,
            title=title,
            brand=str(card.get("brand") or ""),
            category=category,
            highlights=[str(value) for value in highlights if isinstance(value, str)] if isinstance(highlights, list) else [],
            price_major=float(price) if price is not None else None,
            currency=str(card.get("currency") or "USD").upper(),
            rating_summary=rating,
            image_url=card.get("image_url") if isinstance(card.get("image_url"), str) else None,
            purchase_url=card.get("purchase_url") if isinstance(card.get("purchase_url"), str) else None,
            search_term=search_term,
            synced_at=synced_at,
        )
