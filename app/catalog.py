"""Catalog: turn product rows into UCP product objects, search and lookup."""

from typing import Any

from .db import Database

CURRENCY = "TRY"

_FOLD = str.maketrans("çğıöşüéâîû", "cgiosueaiu")


def fold(text: str) -> str:
  """Lowercase and drop Turkish accents, so 'süt' matches 'sut' and 'Sütaş'."""
  return text.replace("I", "ı").replace("İ", "i").lower().translate(_FOLD)


def _money(amount: int) -> dict[str, Any]:
  return {"amount": amount, "currency": CURRENCY}


def _variant(product: dict[str, Any], v: dict[str, Any], url: str) -> dict[str, Any]:
  return {
    "id": v["id"],
    "sku": v["id"],
    "title": f"{product['title']} {v['size']}",
    "description": {"plain": f"{product['description']} Size: {v['size']}."},
    "price": _money(v["price"]),
    "availability": {"available": v["inventory"] > 0},
    "options": [{"name": "Size", "label": v["size"]}],
    "url": url,
  }


def to_ucp_product(product: dict[str, Any], base_url: str) -> dict[str, Any]:
  """One product per brand and type; each size is a variant.

  Checkout line items use the variant id (e.g. "sku_00002").
  """
  url = f"{base_url}/products/{product['handle']}"
  prices = [v["price"] for v in product["variants"]]
  return {
    "id": product["id"],
    "handle": product["handle"],
    "title": product["title"],
    "description": {"plain": product["description"]},
    "url": url,
    "categories": [{"value": product["category"], "taxonomy": "merchant"}],
    "tags": [product["brand"]],
    "price_range": {"min": _money(min(prices)), "max": _money(max(prices))},
    "options": [{"name": "Size", "values": [{"label": v["size"]} for v in product["variants"]]}],
    "variants": [_variant(product, v, url) for v in product["variants"]],
  }


def search(
  db: Database, body: dict[str, Any], base_url: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  words = fold(body.get("query") or "").split()
  filters = body.get("filters") or {}
  categories = {fold(c) for c in filters.get("categories") or [] if isinstance(c, str)}
  price = filters.get("price") or {}
  pmin, pmax = price.get("min"), price.get("max")

  results = []
  for p in db.list_products():
    text = fold(f"{p['title']} {p['brand']} {p['description']} {p['category']}")
    if words and not all(w in text for w in words):
      continue
    if categories and fold(p["category"]) not in categories:
      continue
    prices = [v["price"] for v in p["variants"]]
    if isinstance(pmin, int) and max(prices) < pmin:
      continue
    if isinstance(pmax, int) and min(prices) > pmax:
      continue
    results.append(p)

  pagination = body.get("pagination") or {}
  limit = pagination.get("limit") if isinstance(pagination.get("limit"), int) else 10
  limit = max(1, min(limit, 50))
  try:
    start = max(0, int(pagination.get("cursor") or 0))
  except (TypeError, ValueError):
    start = 0
  page = results[start : start + limit]
  has_next = start + limit < len(results)
  page_info: dict[str, Any] = {"has_next_page": has_next, "total_count": len(results)}
  if has_next:
    page_info["cursor"] = str(start + limit)
  return [to_ucp_product(p, base_url) for p in page], page_info


def lookup(
  db: Database, ids: list[str], base_url: str
) -> tuple[list[dict[str, Any]], list[str]]:
  found: dict[str, dict[str, Any]] = {}
  inputs: dict[str, dict[str, list[dict[str, str]]]] = {}
  missing = []
  for raw in ids:
    product = db.get_product(raw) if isinstance(raw, str) else None
    if product is None:
      missing.append(str(raw))
      continue
    found.setdefault(product["id"], product)
    variant_ids = [v["id"] for v in product["variants"]]
    # A variant id matches that size exactly; a product id or handle returns
    # every size, each marked "featured". Lookup variants must carry `inputs`.
    targets = [(raw, "exact")] if raw in variant_ids else [(vid, "featured") for vid in variant_ids]
    for target, match in targets:
      inputs.setdefault(product["id"], {}).setdefault(target, []).append({"id": raw, "match": match})
  products = []
  for pid, product in found.items():
    out = to_ucp_product(product, base_url)
    out["variants"] = [v for v in out["variants"] if v["id"] in inputs[pid]]
    for variant in out["variants"]:
      variant["inputs"] = inputs[pid][variant["id"]]
    products.append(out)
  return products, missing
