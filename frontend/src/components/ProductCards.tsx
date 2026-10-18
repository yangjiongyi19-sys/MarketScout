import { memo, useState } from "react";
import type { ProductCard } from "../types";
import Icon from "./Icon";
import "./compactProductCards.css";
export function syncTimeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
export function money(value: number | undefined, currency: string) {
  if (value === undefined || !Number.isFinite(value)) return "待确认";
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value} ${currency}`;
  }
}
export function ProductImage({
  product,
  className = "",
}: {
  product: ProductCard;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  // 图片只使用服务端目录来源，不按商品名或 ID 注入前端样例图。
  if (!product.image_url || failed)
    return (
      <div className={`image-placeholder ${className}`}>
        <Icon name="bag" />
        <span>{product.category || "好物详情"}</span>
        <small>暂未提供商品图片</small>
      </div>
    );
  return (
    <img
      className={className}
      src={product.image_url}
      alt={product.image_alt || product.title}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
interface ProductCardsProps {
  products: ProductCard[];
  favoriteIds: Set<string>;
  comparedIds: Set<string>;
  onFavorite: (product: ProductCard) => void;
  onCompare: (product: ProductCard) => void;
  onDetail: (product: ProductCard) => void;
}
function ProductCards({
  products,
  favoriteIds,
  comparedIds,
  onFavorite,
  onCompare,
  onDetail,
}: ProductCardsProps) {
  return (
    <div className="product-grid compact-product-grid">
      {products.map((product, index) => {
        const external = product.purchase_mode === "external" || product.source_platform === "amazon_creators" || product.source_platform === "amazon_rainforest";
        const saved = favoriteIds.has(product.product_id),
          selected = comparedIds.has(product.product_id),
          rating = product.rating_summary;
        const primary = product.skus.find(
            (sku) => sku.sku_id === product.default_sku_id,
          ),
          landed = product.landed_price;
        return (
          <article
            className={`product-card ${selected ? "selected" : ""}`}
            key={product.product_id}
            style={{ animationDelay: `${Math.min(index, 5) * 55}ms` }}
          >
            <div className="product-visual">
              <button
                className="image-open"
                onClick={() => onDetail(product)}
                aria-label={`查看 ${product.title} 详情`}
              >
                <ProductImage product={product} />
              </button>
              <span className="product-badge">{external ? "Amazon.com" : product.category || "精选商品"}</span>
              <button
                className={`heart ${saved ? "saved" : ""}`}
                onClick={() => onFavorite(product)}
                aria-label={`${saved ? "取消收藏" : "收藏"} ${product.title}`}
                aria-pressed={saved}
              >
                <Icon name="heart" />
              </button>
              <span className="visual-caption">
                {product.image_kind === "illustration"
                  ? "商品示意图 · 非实物照片"
                  : external ? "Amazon 商品图片" : product.image_url ? "商品图片" : "商品图片待补充"}
              </span>
            </div>
            <div className="product-body">
              <div className="product-topline">
                <span>{product.brand || (external ? `ASIN ${product.asin || ""}` : "精选商品")}</span>
                {rating && (
                  <span className="rating">
                    <i>★</i> {rating.average.toFixed(1)}{" "}
                    <span>({rating.review_count}{product.rating_is_live ? "" : external ? " · 同步快照" : " · 样例"})</span>
                  </span>
                )}
              </div>
              <button
                className="product-title"
                onClick={() => onDetail(product)}
              >
                {product.title}
              </button>
              <div className="product-subtitle">
                {primary?.spec || product.highlights[0] || (external ? "Amazon.com 外部商品" : "查看商品详细信息")}
              </div>
              <div className="product-price-row">
                <div className="price">
                  {external && product.price_major === undefined
                    ? "前往 Amazon 查看价格"
                    : money(product.price_major, product.currency)}
                </div>
                <span className="price-kind">
                  {external
                    ? `Amazon USD 同步价${syncTimeLabel(product.synced_at) ? ` · ${syncTimeLabel(product.synced_at)}` : ""}`
                    : "商品价"}
                </span>
              </div>
              {!external && landed &&
              !landed.unavailable_reason &&
              Number.isFinite(landed.landed_total_major) ? (
                <div className="card-landed">
                  到手价 {money(landed.landed_total_major, landed.currency)}
                  <span>配送至 {landed.ship_to} · 默认规格</span>
                </div>
              ) : !external ? (
                <div className="card-landed pending">
                  到手价待目的地与规格确认
                </div>
              ) : null}
              <div className="product-footer">
                <label className="compare-check">
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => onCompare(product)}
                    aria-label={`将 ${product.title} 加入比较`}
                  />
                  加入比较
                </label>
                {external && product.purchase_url ? (
                  <a className="detail-button" href={product.purchase_url} target="_blank" rel="noopener noreferrer">
                    前往 Amazon <Icon name="arrow" />
                  </a>
                ) : <button className="detail-button" onClick={() => onDetail(product)}>
                    看看细节 <Icon name="arrow" />
                  </button>}
              </div>
            </div>
          </article>
        );
      })}
    </div>
  );
}

// 商品引用与交互回调不变时，不随助手逐字输出重绘商品区域。
export default memo(ProductCards);
