import { useState } from "react";

import { PRODUCT_IMAGES } from "./productImages";
import { ProductIcon } from "./ProductIcon";

// Real photo when we have one mapped and it loads; falls back to the
// hand-drawn icon tile (never a broken-image glyph) if the SKU is unmapped
// or the external image ever fails to load.
export function ProductImage({
  sku,
  alt,
  className = "",
}: {
  sku: string;
  alt: string;
  className?: string;
}) {
  const src = PRODUCT_IMAGES[sku];
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return <ProductIcon sku={sku} className={className} />;
  }

  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
      className={`object-cover bg-sand ${className}`}
    />
  );
}
