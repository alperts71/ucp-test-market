"""Checkout sessions: create, update, complete, cancel, and the order they make.

The stored checkout keeps the last request from the agent ("request state")
and every response is rebuilt from it, so prices, stock and totals are always
fresh. Keys starting with "_" are internal and never sent to the agent.
"""

import copy
import datetime as dt
import secrets
import time
from typing import Any

from . import payments
from .db import Database
from .profile import FULFILLMENT

CURRENCY = "USD"
SESSION_TTL = dt.timedelta(hours=6)
TERMINAL = ("completed", "canceled")

SHIPPING_OPTIONS = [
  ("standard", "Standard Shipping", "Arrives in 5-7 business days", 999),
  ("express", "Express Shipping", "Arrives in 1-2 business days", 2499),
]
ADDRESS_REQUIRED = ("street_address", "address_locality", "postal_code", "address_country")
ADDRESS_FIELDS = (
  "first_name",
  "last_name",
  "full_name",
  "street_address",
  "extended_address",
  "address_locality",
  "address_region",
  "postal_code",
  "address_country",
  "phone_number",
)


def new_id(prefix: str) -> str:
  return f"{prefix}_{secrets.token_hex(8)}"


def _now() -> dt.datetime:
  return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
  return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(code: str, content: str, path: str | None = None, severity: str = "recoverable"):
  msg = {"type": "error", "code": code, "content": content, "severity": severity}
  if path:
    msg["path"] = path
  return msg


class CheckoutService:
  def __init__(self, db: Database, settings):
    self.db = db
    self.settings = settings

  # Public operations --------------------------------------------------------

  def create(self, body: dict[str, Any], base_url: str, active: set[str]):
    """Returns (checkout, None) or (None, error_message)."""
    requested = body.get("line_items")
    if not isinstance(requested, list) or not requested:
      return None, _err(
        "invalid", "line_items must contain at least one item.", severity="unrecoverable"
      )
    known = [
      li
      for li in requested
      if isinstance(li, dict)
      and isinstance(li.get("item"), dict)
      and self.db.get_variant(str(li["item"].get("id"))) is not None
    ]
    if not known:
      return None, _err(
        "item_unavailable",
        "None of the requested items exist in this store. Use catalog search"
        " to find valid item ids (variant ids such as 'sku_001').",
        severity="unrecoverable",
      )
    now = _now()
    checkout = {
      "id": new_id("chk"),
      "status": "incomplete",
      "_created_at": _iso(now),
      "expires_at": _iso(now + SESSION_TTL),
      "_request": self._request_state(body),
    }
    return self._evaluate_and_save(checkout, base_url, active), None

  def get(self, checkout_id: str, base_url: str, active: set[str]):
    checkout = self._load(checkout_id)
    if checkout is None:
      return None
    self._maybe_finish_pending(checkout, base_url, active)
    return self._evaluate_and_save(checkout, base_url, active)

  def update(self, checkout_id: str, body: dict[str, Any], base_url: str, active: set[str]):
    checkout = self._load(checkout_id)
    if checkout is None:
      return None
    self._maybe_finish_pending(checkout, base_url, active)
    if checkout["status"] in TERMINAL or checkout["status"] == "complete_in_progress":
      return self._with_extra(
        self._evaluate_and_save(checkout, base_url, active),
        _err(
          "not_modifiable",
          f"Checkout is {checkout['status']} and cannot be updated.",
        ),
      )
    checkout["_request"] = self._request_state(body)
    checkout.pop("_payment_message", None)
    if checkout["status"] == "requires_escalation":
      checkout["status"] = "incomplete"
    return self._evaluate_and_save(checkout, base_url, active)

  def cancel(self, checkout_id: str, base_url: str, active: set[str]):
    checkout = self._load(checkout_id)
    if checkout is None:
      return None
    self._maybe_finish_pending(checkout, base_url, active)
    if checkout["status"] == "completed":
      return self._with_extra(
        self._evaluate_and_save(checkout, base_url, active),
        _err("not_cancelable", "Checkout is already completed."),
      )
    checkout["status"] = "canceled"
    checkout.pop("_pending", None)
    return self._evaluate_and_save(checkout, base_url, active)

  def complete(self, checkout_id: str, body: dict[str, Any], base_url: str, active: set[str]):
    checkout = self._load(checkout_id)
    if checkout is None:
      return None
    self._maybe_finish_pending(checkout, base_url, active)
    status = checkout["status"]
    if status in TERMINAL or status == "complete_in_progress":
      view = self._evaluate_and_save(checkout, base_url, active)
      if status == "completed":
        return view
      return self._with_extra(
        view, _err("not_completable", f"Checkout is {status}; it cannot be completed.")
      )

    # Payment from the complete request replaces what was stored before.
    payment = body.get("payment")
    if isinstance(payment, dict) and isinstance(payment.get("instruments"), list):
      checkout["_request"]["payment"] = {"instruments": payment["instruments"]}
    checkout.pop("_payment_message", None)

    view = self._evaluate(checkout, base_url, active)
    blocking = [m for m in view.get("messages", []) if m["type"] == "error"]
    if blocking:
      self.db.save_checkout(checkout)
      return view

    instrument = self._selected_instrument(checkout)
    if instrument is None:
      checkout["_payment_message"] = _err(
        "missing", "A selected payment instrument is required.", "$.payment.instruments"
      )
      return self._evaluate_and_save(checkout, base_url, active)

    result = payments.authorize(instrument)
    if result.outcome == "approved":
      self._place_order(checkout, base_url)
    elif result.outcome == "pending":
      checkout["status"] = "complete_in_progress"
      checkout["_pending"] = time.time() + self.settings.mock_pending_seconds
    elif result.outcome == "review":
      checkout["status"] = "requires_escalation"
      checkout["_payment_message"] = _err(
        result.code, result.content, severity="requires_buyer_review"
      )
    elif result.outcome == "timeout":
      # Show complete_in_progress to anyone who polls while we "wait".
      checkout["status"] = "complete_in_progress"
      self.db.save_checkout(checkout)
      time.sleep(self.settings.mock_timeout_seconds)
      checkout = self._load(checkout_id) or checkout
      if checkout["status"] == "complete_in_progress":
        checkout["status"] = "incomplete"
        checkout["_payment_message"] = _err(
          result.code, result.content, "$.payment.instruments"
        )
    else:  # declined or invalid
      checkout["_payment_message"] = _err(result.code, result.content, "$.payment.instruments")
    return self._evaluate_and_save(checkout, base_url, active)

  def approve_review(self, checkout_id: str, base_url: str, active: set[str]):
    """Buyer approved a held order on the continue_url page."""
    checkout = self._load(checkout_id)
    if checkout is None or checkout["status"] != "requires_escalation":
      return checkout
    checkout.pop("_payment_message", None)
    self._place_order(checkout, base_url)
    return self._evaluate_and_save(checkout, base_url, active)

  # Internals ----------------------------------------------------------------

  def _load(self, checkout_id: str) -> dict[str, Any] | None:
    checkout = self.db.get_checkout(checkout_id)
    if checkout is None:
      return None
    expired = dt.datetime.fromisoformat(checkout["expires_at"]) <= _now()
    if expired and checkout["status"] not in TERMINAL:
      checkout["status"] = "canceled"
      checkout.pop("_pending", None)
    return checkout

  def _maybe_finish_pending(self, checkout, base_url, active) -> None:
    pending = checkout.get("_pending")
    if checkout["status"] == "complete_in_progress" and pending and time.time() >= pending:
      checkout.pop("_pending", None)
      self._place_order(checkout, base_url)

  @staticmethod
  def _request_state(body: dict[str, Any]) -> dict[str, Any]:
    keep = ("line_items", "buyer", "context", "fulfillment", "payment", "signals", "attribution")
    return {k: copy.deepcopy(body[k]) for k in keep if k in body}

  @staticmethod
  def _with_extra(view: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    view.setdefault("messages", []).append(message)
    return view

  def _selected_instrument(self, checkout) -> dict[str, Any] | None:
    instruments = (checkout["_request"].get("payment") or {}).get("instruments") or []
    instruments = [i for i in instruments if isinstance(i, dict)]
    selected = [i for i in instruments if i.get("selected") is True]
    if selected:
      return selected[0]
    return instruments[0] if len(instruments) == 1 else None

  def _place_order(self, checkout, base_url) -> None:
    view = self._evaluate(checkout, base_url, {FULFILLMENT})
    quantities: dict[str, int] = {}
    for li in view["line_items"]:
      quantities[li["item"]["id"]] = quantities.get(li["item"]["id"], 0) + li["quantity"]
    short = self.db.reserve_inventory(quantities)
    if short:
      checkout["status"] = "incomplete"
      checkout["_payment_message"] = _err(
        "out_of_stock", f"Items sold out before payment: {', '.join(short)}."
      )
      return
    order_id = new_id("ord")
    permalink = f"{base_url}/order-status/{order_id}"
    expectations = []
    for method in (view.get("fulfillment") or {}).get("methods", []):
      dest = next(
        (d for d in method["destinations"] if d["id"] == method.get("selected_destination_id")),
        None,
      )
      group = method["groups"][0] if method.get("groups") else None
      option = next(
        (o for o in (group or {}).get("options", []) if o["id"] == (group or {}).get("selected_option_id")),
        None,
      )
      if dest:
        expectations.append(
          {
            "id": new_id("exp"),
            "line_items": [
              {"id": li["id"], "quantity": li["quantity"]}
              for li in view["line_items"]
              if li["id"] in method["line_item_ids"]
            ],
            "method_type": method["type"],
            "destination": {k: v for k, v in dest.items() if k in ADDRESS_FIELDS},
            "description": option["description"]["plain"] if option else "Shipping",
          }
        )
    order = {
      "id": order_id,
      "checkout_id": checkout["id"],
      "label": f"Order {order_id[-6:].upper()}",
      "permalink_url": permalink,
      "currency": CURRENCY,
      "line_items": [
        {
          "id": li["id"],
          "item": li["item"],
          "quantity": {"original": li["quantity"], "total": li["quantity"], "fulfilled": 0},
          "totals": li["totals"],
          "status": "processing",
        }
        for li in view["line_items"]
      ],
      "fulfillment": {"expectations": expectations, "events": []},
      "adjustments": [],
      "totals": view["totals"],
      "_buyer": view.get("buyer"),
      "_created_at": _iso(_now()),
    }
    self.db.save_order(order)
    checkout["status"] = "completed"
    checkout["order"] = {"id": order_id, "label": order["label"], "permalink_url": permalink}

  def _evaluate_and_save(self, checkout, base_url, active) -> dict[str, Any]:
    view = self._evaluate(checkout, base_url, active)
    self.db.save_checkout(checkout)
    return view

  def _evaluate(self, checkout: dict[str, Any], base_url: str, active: set[str]) -> dict[str, Any]:
    """Build the response checkout from the stored request state."""
    req = checkout["_request"]
    messages: list[dict[str, Any]] = []

    # Line items ---------------------------------------------------------------
    line_items = []
    for index, li in enumerate(req.get("line_items") or []):
      if not isinstance(li, dict) or not isinstance(li.get("item"), dict):
        continue
      variant_id = str(li["item"].get("id"))
      product = self.db.get_variant(variant_id)
      if product is None:
        messages.append(
          {
            "type": "warning",
            "code": "item_unavailable",
            "content": f"Item '{variant_id}' does not exist and was removed.",
          }
        )
        continue
      quantity = li.get("quantity", 1)
      if not isinstance(quantity, int) or quantity < 1:
        quantity = 1
      line_id = li.get("id") if isinstance(li.get("id"), str) else f"li_{index + 1}"
      amount = product["price"] * quantity
      position = len(line_items)
      line_items.append(
        {
          "id": line_id,
          "item": {"id": product["variant_id"], "title": product["title"], "price": product["price"]},
          "quantity": quantity,
          "totals": [{"type": "subtotal", "amount": amount}, {"type": "total", "amount": amount}],
        }
      )
      if checkout["status"] not in TERMINAL and product["inventory"] < quantity:
        messages.append(
          _err(
            "out_of_stock",
            f"'{product['title']}' has only {product['inventory']} in stock.",
            f"$.line_items[{position}]",
          )
        )
    if not line_items:
      messages.append(_err("missing", "Checkout has no valid line items.", "$.line_items"))

    subtotal = sum(li["totals"][0]["amount"] for li in line_items)

    # Buyer --------------------------------------------------------------------
    buyer = req.get("buyer") if isinstance(req.get("buyer"), dict) else None
    email = (buyer or {}).get("email")
    if not isinstance(email, str) or not email.strip():
      messages.append(_err("missing", "Buyer email is required.", "$.buyer.email"))
    elif "@" not in email:
      messages.append(_err("invalid", "Buyer email is not valid.", "$.buyer.email"))

    # Fulfillment (shipping) ----------------------------------------------------
    fulfillment = None
    shipping_cost = None
    if FULFILLMENT in active and line_items:
      fulfillment, shipping_cost, f_messages = self._fulfillment(req, line_items)
      messages.extend(f_messages)

    # Totals -------------------------------------------------------------------
    totals = [{"type": "subtotal", "amount": subtotal}]
    if shipping_cost is not None:
      totals.append({"type": "fulfillment", "display_text": "Shipping", "amount": shipping_cost})
    totals.append({"type": "total", "amount": subtotal + (shipping_cost or 0)})

    # Payment ------------------------------------------------------------------
    instruments = []
    for inst in (req.get("payment") or {}).get("instruments") or []:
      if isinstance(inst, dict) and {"id", "handler_id", "type"} <= inst.keys():
        # Credentials are write-only and never echoed back.
        instruments.append({k: v for k, v in inst.items() if k != "credential"})

    if checkout.get("_payment_message"):
      messages.append(checkout["_payment_message"])

    # Status -------------------------------------------------------------------
    status = checkout["status"]
    if status not in TERMINAL and status not in ("complete_in_progress", "requires_escalation"):
      blocking = [m for m in messages if m["type"] == "error"]
      status = "incomplete" if blocking else "ready_for_complete"
      checkout["status"] = status

    view: dict[str, Any] = {
      "id": checkout["id"],
      "status": status,
      "currency": CURRENCY,
      "line_items": line_items,
      "totals": totals,
      "links": [
        {"type": "terms_of_service", "url": f"{base_url}/terms"},
        {"type": "privacy_policy", "url": f"{base_url}/privacy"},
      ],
      "expires_at": checkout["expires_at"],
    }
    if buyer:
      view["buyer"] = buyer
    if isinstance(req.get("context"), dict):
      view["context"] = req["context"]
    if fulfillment:
      view["fulfillment"] = fulfillment
    if instruments:
      view["payment"] = {"instruments": instruments}
    if messages:
      view["messages"] = messages
    if status not in TERMINAL:
      view["continue_url"] = f"{base_url}/continue/{checkout['id']}"
    if checkout.get("order"):
      view["order"] = checkout["order"]
    return view

  @staticmethod
  def _fulfillment(req, line_items):
    messages = []
    methods_in = ((req.get("fulfillment") or {}).get("methods")) or []
    method_in = methods_in[0] if methods_in and isinstance(methods_in[0], dict) else {}

    destinations = []
    for i, dest in enumerate(method_in.get("destinations") or []):
      if not isinstance(dest, dict):
        continue
      out = {"type": "shipping_address", "id": dest.get("id") or f"dest_{i + 1}"}
      out.update({k: dest[k] for k in ADDRESS_FIELDS if k in dest})
      missing = [f for f in ADDRESS_REQUIRED if not out.get(f)]
      if missing:
        messages.append(
          _err(
            "invalid_address",
            f"Shipping address is missing: {', '.join(missing)}.",
            f"$.fulfillment.methods[0].destinations[{len(destinations)}]",
          )
        )
      destinations.append(out)

    selected_dest = method_in.get("selected_destination_id")
    ids = [d["id"] for d in destinations]
    if selected_dest not in ids:
      selected_dest = ids[0] if len(ids) == 1 else None
    if selected_dest is None:
      messages.append(
        _err(
          "missing",
          "A shipping address is required.",
          "$.fulfillment.methods[0].selected_destination_id",
        )
      )

    line_ids = [li["id"] for li in line_items]
    method = {
      "id": method_in.get("id") or "shipping_1",
      "type": "shipping",
      "line_item_ids": line_ids,
      "destinations": destinations,
    }
    shipping_cost = None
    if selected_dest:
      method["selected_destination_id"] = selected_dest
      groups_in = method_in.get("groups") or []
      wanted = groups_in[0].get("selected_option_id") if groups_in and isinstance(groups_in[0], dict) else None
      option_ids = [o[0] for o in SHIPPING_OPTIONS]
      chosen = wanted if wanted in option_ids else "standard"
      if wanted and wanted not in option_ids:
        messages.append(
          {
            "type": "warning",
            "code": "option_unavailable",
            "content": f"Shipping option '{wanted}' does not exist; using 'standard'.",
          }
        )
      method["groups"] = [
        {
          "id": "package_1",
          "line_item_ids": line_ids,
          "selected_option_id": chosen,
          "options": [
            {
              "id": oid,
              "title": title,
              "description": {"plain": desc},
              "totals": [{"type": "total", "amount": amount}],
            }
            for oid, title, desc, amount in SHIPPING_OPTIONS
          ],
        }
      ]
      shipping_cost = next(o[3] for o in SHIPPING_OPTIONS if o[0] == chosen)
    return {"methods": [method]}, shipping_cost, messages
