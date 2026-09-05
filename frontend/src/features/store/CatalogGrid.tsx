import type { CatalogItem } from "@/lib/api/storefront";
import { ProductImage } from "./ProductImage";

export function CatalogGrid({ catalog, onAdd }: { catalog: CatalogItem[]; onAdd: (sku: string) => void }) {
  return (
    <div>
      <p className="font-display font-semibold text-sm mb-3">Catalog</p>
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {catalog.map((p) => (
          <div key={p.id} className="rounded-2xl bg-white border border-ink/5 p-4 shadow-[0_20px_40px_-30px_rgba(234,88,12,0.35)]">
            <ProductImage sku={p.id} alt={p.name} className="w-full aspect-square rounded-xl mb-3" />
            <p className="text-sm font-medium mb-1">{p.name}</p>
            <p className="text-sm text-ink/50 mb-3 tabular-nums">₹{p.price_inr.toLocaleString("en-IN")}</p>
            <button
              onClick={() => onAdd(p.id)}
              className="w-full rounded-lg bg-ink text-cream text-xs font-semibold py-2 hover:bg-ink/90"
            >
              Add to cart
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
