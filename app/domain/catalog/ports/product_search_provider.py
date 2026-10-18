"""商品来源适配端口。"""
from __future__ import annotations

from typing import Protocol


class ProductSearchProvider(Protocol):
    """对上层统一提供按买家条件检索商品的能力。"""

    async def search(self, spec) -> dict:
        """按结构化条件返回商品卡结果。"""
        ...
