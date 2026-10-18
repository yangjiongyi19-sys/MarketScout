"""将现有本地目录 RAG 检索用例接入统一商品来源端口。"""

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.product_search_provider import ProductSearchProvider


class LocalFixtureProductSearchProvider(ProductSearchProvider):
    def __init__(self, usecase: CatalogSearchUseCase) -> None:
        self._usecase = usecase

    async def search(self, spec) -> dict:
        return await self._usecase.execute(spec)
