// Real product photography (Unsplash, hotlink-friendly, license-free for this
// use), one per catalog SKU. Square crop requested directly from Unsplash's
// imaging API so cards don't rely on CSS cropping alone.
const PARAMS = "w=640&h=640&q=80&fm=jpg&fit=crop";

export const PRODUCT_IMAGES: Record<string, string> = {
  sku_001: `https://images.unsplash.com/photo-1757168120889-4317e57a4849?${PARAMS}`, // wireless earbuds
  sku_002: `https://images.unsplash.com/photo-1669884210062-e3055c8c8f5d?${PARAMS}`, // mechanical keyboard
  sku_003: `https://images.unsplash.com/photo-1722153105551-cfea928e80de?${PARAMS}`, // smart watch
  sku_004: `https://plus.unsplash.com/premium_photo-1723759271930-3514bb76abb4?${PARAMS}`, // yoga mat
  sku_005: `https://images.unsplash.com/photo-1461988091159-192b6df7054f?${PARAMS}`, // espresso machine
  sku_006: `https://images.unsplash.com/photo-1542483381-41a479b1fb88?${PARAMS}`, // bluetooth speaker
};
