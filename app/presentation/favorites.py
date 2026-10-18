"""收藏按买家隔离读取与维护。"""
import json
import re
from fastapi import HTTPException,Query,Request
from pydantic import BaseModel,ConfigDict,Field
from app.presentation.identity import require_buyer

_EXTERNAL_AMAZON_SOURCES = {'amazon_creators', 'amazon_rainforest'}

def _is_external_amazon(product):
    return product.get('source_platform') in _EXTERNAL_AMAZON_SOURCES

class FavoriteWrite(BaseModel):
    model_config=ConfigDict(extra='forbid')
    product: dict=Field()

def _external_placeholder(product):
    asin = str(product.get('asin', '')).upper()
    return {
        'product_id': f'amazon:{asin}', 'source_platform': product.get('source_platform'),
        'purchase_mode': 'external', 'asin': asin,
        'title': f'Amazon 商品（ASIN: {asin}）', 'brand': '', 'category': 'Amazon 商品',
        'price_major': None, 'currency': 'USD', 'highlights': [], 'skus': [],
        'purchase_url': None, 'unavailable': True,
    }


def register_favorite_routes(api,get_store,get_external_provider=lambda:None):
    async def list_products(buyer):
        products=await get_store().list(buyer)
        external=[product for product in products if _is_external_amazon(product)]
        if external:
            provider=get_external_provider()
            try:
                refreshed=await provider.refresh_favorites(external) if provider is not None else [_external_placeholder(item) for item in external]
            except Exception as error:
                from app.infrastructure.amazon_creators import AmazonCreatorsError
                from app.infrastructure.rainforest_api import RainforestError
                if not isinstance(error,(AmazonCreatorsError,RainforestError)): raise
                raise HTTPException(503,str(error)) from error
            by_id={product['product_id']:product for product in refreshed}
            products=[by_id.get(product.get('product_id'),product) if _is_external_amazon(product) else product for product in products]
        return products

    @api.get('/commerce/favorites')
    async def favorites(request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        return {'products':await list_products(buyer)}

    @api.put('/commerce/favorites/{product_id}')
    async def save_favorite(product_id:str,body:FavoriteWrite,request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id)
        if body.product.get('product_id')!=product_id or not 1<=len(product_id)<=128 or len(json.dumps(body.product))>30000:
            raise HTTPException(422,'收藏商品信息无效')
        store=get_store()
        product=body.product
        if _is_external_amazon(product):
            asin=str(product.get('asin','')).upper()
            if not re.fullmatch(r'[A-Z0-9]{10}',asin) or product_id!=f'amazon:{asin}' or product.get('purchase_mode')!='external':
                raise HTTPException(422,'Amazon 收藏必须包含有效 ASIN')
            # Amazon API 商品数据有缓存期限；持久收藏只留 ASIN 与平台身份，列表读取时再刷新。
            product={'product_id':product_id,'source_platform':product.get('source_platform'),'purchase_mode':'external','asin':asin}
        try:await store.save(buyer,product)
        except ValueError as error:raise HTTPException(422,str(error)) from error
        return {'products':await list_products(buyer)}

    @api.delete('/commerce/favorites/{product_id}')
    async def delete_favorite(product_id:str,request:Request,buyer_id:str=Query(min_length=1)):
        buyer=await require_buyer(request,buyer_id);store=get_store()
        await store.delete(buyer,product_id)
        return {'products':await list_products(buyer)}
