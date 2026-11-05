"""SQLite storage.

Products are seeded from products.json on first start. Checkout sessions and
orders are stored as JSON documents, which keeps the code short and makes the
database easy to inspect with any SQLite viewer.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

PRODUCTS_FILE = Path(__file__).with_name("products.json")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  variant_id TEXT UNIQUE NOT NULL,
  handle TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  price INTEGER NOT NULL,
  inventory INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkouts (
  id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  checkout_id TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT NOT NULL,
  scope TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (key, scope)
);
"""

# Idempotency keys are kept at least 24 hours, as the REST binding requires.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class Database:
  def __init__(self, path: str):
    if path != ":memory:":
      Path(path).parent.mkdir(parents=True, exist_ok=True)
    self._conn = sqlite3.connect(path, check_same_thread=False)
    self._conn.row_factory = sqlite3.Row
    # One writer at a time keeps checkout updates consistent.
    self.lock = threading.RLock()
    with self.lock:
      self._conn.executescript(_SCHEMA)
      self._conn.commit()
    if self.count_products() == 0:
      self.seed_products()

  # Products ---------------------------------------------------------------

  def count_products(self) -> int:
    with self.lock:
      return self._conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

  def seed_products(self) -> None:
    rows = json.loads(PRODUCTS_FILE.read_text())
    with self.lock:
      self._conn.execute("DELETE FROM products")
      self._conn.executemany(
        "INSERT INTO products VALUES (:id, :variant_id, :handle, :title,"
        " :description, :category, :price, :inventory)",
        rows,
      )
      self._conn.commit()

  def list_products(self) -> list[dict[str, Any]]:
    with self.lock:
      rows = self._conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return [dict(r) for r in rows]

  def get_product(self, any_id: str) -> dict[str, Any] | None:
    """Find a product by product id, variant id or handle."""
    with self.lock:
      row = self._conn.execute(
        "SELECT * FROM products WHERE id = ? OR variant_id = ? OR handle = ?",
        (any_id, any_id, any_id),
      ).fetchone()
    return dict(row) if row else None

  def get_variant(self, variant_id: str) -> dict[str, Any] | None:
    """Checkout line items refer to the variant id (the SKU)."""
    with self.lock:
      row = self._conn.execute(
        "SELECT * FROM products WHERE variant_id = ?", (variant_id,)
      ).fetchone()
    return dict(row) if row else None

  def reserve_inventory(self, quantities: dict[str, int]) -> list[str]:
    """Take stock for an order. Returns variant ids that ran out (no change)."""
    with self.lock:
      short = []
      for variant_id, qty in quantities.items():
        row = self._conn.execute(
          "SELECT inventory FROM products WHERE variant_id = ?", (variant_id,)
        ).fetchone()
        if row is None or row[0] < qty:
          short.append(variant_id)
      if short:
        return short
      for variant_id, qty in quantities.items():
        self._conn.execute(
          "UPDATE products SET inventory = inventory - ? WHERE variant_id = ?",
          (qty, variant_id),
        )
      self._conn.commit()
      return []

  # Checkouts --------------------------------------------------------------

  def save_checkout(self, checkout: dict[str, Any]) -> None:
    with self.lock:
      self._conn.execute(
        "INSERT INTO checkouts (id, data, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET data = excluded.data,"
        " updated_at = excluded.updated_at",
        (checkout["id"], json.dumps(checkout), time.time()),
      )
      self._conn.commit()

  def get_checkout(self, checkout_id: str) -> dict[str, Any] | None:
    with self.lock:
      row = self._conn.execute(
        "SELECT data FROM checkouts WHERE id = ?", (checkout_id,)
      ).fetchone()
    return json.loads(row[0]) if row else None

  def list_checkouts(self, limit: int = 50) -> list[dict[str, Any]]:
    with self.lock:
      rows = self._conn.execute(
        "SELECT data FROM checkouts ORDER BY updated_at DESC LIMIT ?", (limit,)
      ).fetchall()
    return [json.loads(r[0]) for r in rows]

  # Orders -----------------------------------------------------------------

  def save_order(self, order: dict[str, Any]) -> None:
    with self.lock:
      self._conn.execute(
        "INSERT OR REPLACE INTO orders (id, checkout_id, data, created_at)"
        " VALUES (?, ?, ?, ?)",
        (order["id"], order["checkout_id"], json.dumps(order), time.time()),
      )
      self._conn.commit()

  def get_order(self, order_id: str) -> dict[str, Any] | None:
    with self.lock:
      row = self._conn.execute(
        "SELECT data FROM orders WHERE id = ?", (order_id,)
      ).fetchone()
    return json.loads(row[0]) if row else None

  def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
    with self.lock:
      rows = self._conn.execute(
        "SELECT data FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
      ).fetchall()
    return [json.loads(r[0]) for r in rows]

  # Idempotency ------------------------------------------------------------

  def get_idempotent(self, key: str, scope: str) -> sqlite3.Row | None:
    with self.lock:
      self._conn.execute(
        "DELETE FROM idempotency WHERE created_at < ?",
        (time.time() - IDEMPOTENCY_TTL_SECONDS,),
      )
      return self._conn.execute(
        "SELECT request_hash, status_code, response FROM idempotency"
        " WHERE key = ? AND scope = ?",
        (key, scope),
      ).fetchone()

  def put_idempotent(
    self, key: str, scope: str, request_hash: str, status: int, body: str
  ) -> None:
    with self.lock:
      self._conn.execute(
        "INSERT OR REPLACE INTO idempotency VALUES (?, ?, ?, ?, ?, ?)",
        (key, scope, request_hash, status, body, time.time()),
      )
      self._conn.commit()

  # Admin ------------------------------------------------------------------

  def reset(self) -> None:
    with self.lock:
      for table in ("checkouts", "orders", "idempotency"):
        self._conn.execute(f"DELETE FROM {table}")  # noqa: S608
      self._conn.commit()
    self.seed_products()
