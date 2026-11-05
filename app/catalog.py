"""Catalog: turn product rows into UCP product objects, search and lookup."""

from typing import Any

from .db import Database

CURRENCY = "USD"


def _money(amount: int) -> dict[str, Any]:
  return {"amount": amount, "currency": CURRENCY}


def to_ucp_product(row: dict[str, Any], base_url: str) -> dict[str, Any]:
  """Each product has exactly one variant; checkout uses the variant id."""
  url = f"{base_url}/products/{row['handle']}"
  return {
    "id": row["id"],
    "handle": row["handle"],
    "title": row["title"],
    "description": {"plain": row["description"]},
    "url": url,
    "categories": [{"value": row["category"], "taxonomy": "merchant"}],
    "price_range": {"min": _money(row["price"]), "max": _money(row["price"])},
    "variants": [
      {
        "id": row["variant_id"],
        "sku": row["variant_id"],
        "title": row["title"],
        "description": {"plain": row["description"]},
        "price": _money(row["price"]),
        "availability": {"available": row["inventory"] > 0},
        "url": url,
      }
    ],
  }


def search(
  db: Database, body: dict[str, Any], base_url: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  query = (body.get("query") or "").strip().lower()
  filters = body.get("filters") or {}
  categories = {c.lower() for c in filters.get("categories") or [] if isinstance(c, str)}
  price = filters.get("price") or {}
  pmin, pmax = price.get("min"), price.get("max")

  words = [w for w in query.split() if w]
  results = []
  for row in db.list_products():
    text = f"{row['title']} {row['description']} {row['category']}".lower()
    if words and not all(w in text for w in words):
      continue
    if categories and row["category"].lower() not in categories:
      continue
    if isinstance(pmin, int) and row["price"] < pmin:
      continue
    if isinstance(pmax, int) and row["price"] > pmax:
      continue
    results.append(row)

  pagination = body.get("pagination") or {}
  limit = pagination.get("limit") if isinstance(pagination.get("limit"), int) else 10
  limit = max(1, min(limit, 50))
  try:
    start = int(pagination.get("cursor") or 0)
  except (TypeError, ValueError):
    start = 0
  page = results[start : start + limit]
  has_next = start + limit < len(results)
  page_info: dict[str, Any] = {"has_next_page": has_next, "total_count": len(results)}
  if has_next:
    page_info["cursor"] = str(start + limit)
  return [to_ucp_product(r, base_url) for r in page], page_info


def lookup(
  db: Database, ids: list[str], base_url: str
) -> tuple[list[dict[str, Any]], list[str]]:
  found: dict[str, dict[str, Any]] = {}
  inputs: dict[str, list[dict[str, str]]] = {}
  missing = []
  for raw in ids:
    row = db.get_product(raw) if isinstance(raw, str) else None
    if row is None:
      missing.append(str(raw))
      continue
    found.setdefault(row["id"], row)
    match = "exact" if raw == row["variant_id"] else "featured"
    inputs.setdefault(row["id"], []).append({"id": raw, "match": match})
  products = []
  for pid, row in found.items():
    product = to_ucp_product(row, base_url)
    product["variants"][0]["inputs"] = inputs[pid]
    products.append(product)
  return products, missing
