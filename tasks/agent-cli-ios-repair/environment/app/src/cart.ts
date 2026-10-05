type Product = { id: string; name: string; priceCents: number };

export function cartTotalCents(products: Product[], quantities: Record<string, number>): number {
  return products.reduce((sum, product) => sum + product.priceCents, 0);
}
