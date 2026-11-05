"""End-to-end tests. Every response is validated with the official UCP Python
SDK models (ucp-sdk, generated from the 2026-08-25 schemas)."""

import dataclasses
import json
import time

import pytest
from fastapi.testclient import TestClient
from ucp_sdk.models.schemas.common.types.error_response import ErrorResponse
from ucp_sdk.models.schemas.profile import UcpProfileDocument
from ucp_sdk.models.schemas.shopping.catalog_lookup import LookupResponse
from ucp_sdk.models.schemas.shopping.catalog_search import SearchResponse
from ucp_sdk.models.schemas.shopping.fulfillment import Checkout
from ucp_sdk.models.schemas.shopping.order import Order

from app.config import load_settings
from app.db import PRODUCTS_FILE as PRODUCTS
from app.main import create_app

AGENT = {"UCP-Agent": 'profile="https://agent.example/profile"'}
ADDRESS = {
  "full_name": "Test Buyer",
  "street_address": "1 Main St",
  "address_locality": "Austin",
  "address_region": "TX",
  "postal_code": "78701",
  "address_country": "US",
}


@pytest.fixture
def client(tmp_path):
  settings = dataclasses.replace(
    load_settings(),
    db_path=str(tmp_path / "store.db"),
    mock_timeout_seconds=0.2,
    mock_pending_seconds=0.3,
    admin_token="secret",
  )
  return TestClient(create_app(settings))


def check_totals(totals):
  types = [t["type"] for t in totals]
  assert types.count("subtotal") == 1 and types.count("total") == 1
  assert sum(t["amount"] for t in totals if t["type"] != "total") == next(
    t["amount"] for t in totals if t["type"] == "total"
  )


def checkout_ok(resp, status_code=200):
  assert resp.status_code == status_code, resp.text
  data = resp.json()
  Checkout.model_validate(data)
  check_totals(data["totals"])
  assert "_request" not in data
  return data


def ready_checkout(client, sku="sku_00002", qty=1):
  data = checkout_ok(
    client.post(
      "/checkout-sessions",
      headers=AGENT,
      json={
        "line_items": [{"item": {"id": sku}, "quantity": qty}],
        "buyer": {"email": "buyer@example.com"},
        "fulfillment": {"methods": [{"type": "shipping", "destinations": [ADDRESS]}]},
      },
    ),
    201,
  )
  assert data["status"] == "ready_for_complete", data.get("messages")
  return data


def pay(token):
  return {
    "payment": {
      "instruments": [
        {
          "id": "pi_1",
          "handler_id": "mock_payment_handler",
          "type": "card",
          "selected": True,
          "display": {"brand": "visa", "last_digits": "4242"},
          "credential": {"type": "token", "token": token},
        }
      ]
    }
  }


def test_discovery_profile(client):
  resp = client.get("/.well-known/ucp")
  assert resp.status_code == 200
  profile = UcpProfileDocument.model_validate(resp.json())
  ucp = resp.json()["ucp"]
  assert ucp["version"] == "2026-08-25"
  assert "dev.ucp.shopping.checkout" in ucp["capabilities"]
  assert ucp["services"]["dev.ucp.shopping"][0]["endpoint"] == "http://testserver"
  assert "dev.ucp.mock_payment" in ucp["payment_handlers"]
  assert profile is not None


def test_catalog_has_500_supermarket_products(client):
  data = client.post("/catalog/search", headers=AGENT, json={"pagination": {"limit": 50}}).json()
  SearchResponse.model_validate(data)
  assert data["pagination"]["total_count"] == 500
  product = data["products"][0]
  assert product["title"] == "Sütaş Whole Milk"
  assert [v["options"][0]["label"] for v in product["variants"]] == ["500 ml", "1 L", "1.5 L"]
  assert product["variants"][0]["price"]["currency"] == "TRY"


def test_catalog_search_and_lookup(client):
  # Turkish accents are optional in search: "sut" finds "Sütaş" and "süt".
  data = client.post("/catalog/search", headers=AGENT, json={"query": "sutas beyaz peynir"}).json()
  assert data["products"] and all("Sütaş" in p["title"] for p in data["products"])
  data = client.post("/catalog/search", headers=AGENT, json={"query": "süt", "filters": {"categories": ["milk"]}}).json()
  assert data["pagination"]["total_count"] > 20
  data = client.post("/catalog/lookup", headers=AGENT, json={"ids": ["sku_00002", "prod_0003", "nope"]}).json()
  LookupResponse.model_validate(data)
  assert [p["id"] for p in data["products"]] == ["prod_0001", "prod_0003"]
  assert [v["id"] for v in data["products"][0]["variants"]] == ["sku_00002"]
  assert data["products"][0]["variants"][0]["inputs"] == [{"id": "sku_00002", "match": "exact"}]
  assert all(v["inputs"][0]["match"] == "featured" for v in data["products"][1]["variants"])
  assert data["messages"][0]["code"] == "not_found"


def test_happy_path_creates_order(client):
  chk = ready_checkout(client, qty=2)
  totals = {t["type"]: t["amount"] for t in chk["totals"]}
  assert totals["fulfillment"] == 4990
  done = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("success_token")))
  assert done["status"] == "completed"
  assert "continue_url" not in done
  assert "credential" not in done["payment"]["instruments"][0]
  order = client.get(f"/orders/{done['order']['id']}", headers=AGENT).json()
  Order.model_validate(order)
  assert order["checkout_id"] == chk["id"]
  assert order["line_items"][0]["quantity"]["total"] == 2
  # Stock went down by 2.
  start = json.loads(PRODUCTS.read_text())[0]["variants"][1]["inventory"]
  assert client.app.state.db.get_variant("sku_00002")["inventory"] == start - 2


def test_create_reports_what_is_missing(client):
  data = checkout_ok(
    client.post("/checkout-sessions", headers=AGENT, json={"line_items": [{"item": {"id": "sku_00004"}, "quantity": 1}]}),
    201,
  )
  assert data["status"] == "incomplete"
  paths = {m.get("path") for m in data["messages"]}
  assert "$.buyer.email" in paths
  assert "$.fulfillment.methods[0].selected_destination_id" in paths
  assert data["continue_url"].endswith(data["id"])


def test_update_selects_express_shipping(client):
  chk = ready_checkout(client)
  body = {
    "line_items": [{"id": chk["line_items"][0]["id"], "item": {"id": "sku_00002"}, "quantity": 1}],
    "buyer": {"email": "buyer@example.com"},
    "fulfillment": {
      "methods": [
        {
          "id": "shipping_1",
          "type": "shipping",
          "selected_destination_id": "dest_1",
          "destinations": [{**ADDRESS, "id": "dest_1"}],
          "groups": [{"id": "package_1", "selected_option_id": "express"}],
        }
      ]
    },
  }
  data = checkout_ok(client.put(f"/checkout-sessions/{chk['id']}", headers=AGENT, json=body))
  assert {t["type"]: t["amount"] for t in data["totals"]}["fulfillment"] == 9990


def test_unknown_items_give_error_response(client):
  resp = client.post("/checkout-sessions", headers=AGENT, json={"line_items": [{"item": {"id": "x"}, "quantity": 1}]})
  assert resp.status_code == 200
  ErrorResponse.model_validate(resp.json())
  assert resp.json()["ucp"]["status"] == "error"


def test_out_of_stock_item(client):
  data = checkout_ok(
    client.post(
      "/checkout-sessions",
      headers=AGENT,
      json={"line_items": [{"item": {"id": "sku_00041"}, "quantity": 1}], "buyer": {"email": "a@b.co"}},
    ),
    201,
  )
  assert any(m["code"] == "out_of_stock" for m in data["messages"])
  assert data["status"] == "incomplete"


@pytest.mark.parametrize("token", ["fail_token", "decline_token", "unknown_token"])
def test_declines_are_recoverable(client, token):
  chk = ready_checkout(client)
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay(token)))
  assert data["status"] == "incomplete"
  assert data["messages"][-1]["code"] == "payment_failed"
  assert data["messages"][-1]["severity"] == "recoverable"
  # A retry with a good token works.
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("success_token")))
  assert data["status"] == "completed"


def test_timeout_then_failure(client):
  chk = ready_checkout(client)
  start = time.time()
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("timeout_token")))
  assert time.time() - start >= 0.2
  assert data["status"] == "incomplete"
  assert data["messages"][-1]["code"] == "payment_failed"


def test_pending_completes_later(client):
  chk = ready_checkout(client)
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("pending_token")))
  assert data["status"] == "complete_in_progress"
  assert "order" not in data
  # Updates are refused while in progress.
  data = checkout_ok(client.put(f"/checkout-sessions/{chk['id']}", headers=AGENT, json={"line_items": []}))
  assert data["status"] == "complete_in_progress"
  time.sleep(0.35)
  data = checkout_ok(client.get(f"/checkout-sessions/{chk['id']}", headers=AGENT))
  assert data["status"] == "completed" and data["order"]["id"]


def test_fraud_requires_escalation_and_buyer_can_approve(client):
  chk = ready_checkout(client)
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("fraud_token")))
  assert data["status"] == "requires_escalation"
  assert data["continue_url"]
  assert any(m.get("severity") == "requires_buyer_review" for m in data["messages"])
  page = client.post(f"/continue/{chk['id']}/approve", follow_redirects=False)
  assert page.status_code == 303
  data = checkout_ok(client.get(f"/checkout-sessions/{chk['id']}", headers=AGENT))
  assert data["status"] == "completed"


def test_cancel(client):
  chk = ready_checkout(client)
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/cancel", headers=AGENT))
  assert data["status"] == "canceled"
  data = checkout_ok(client.post(f"/checkout-sessions/{chk['id']}/complete", headers=AGENT, json=pay("success_token")))
  assert data["status"] == "canceled"


def test_idempotency(client):
  body = {"line_items": [{"item": {"id": "sku_00002"}, "quantity": 1}]}
  headers = {**AGENT, "Idempotency-Key": "abc-123"}
  first = client.post("/checkout-sessions", headers=headers, json=body)
  second = client.post("/checkout-sessions", headers=headers, json=body)
  assert first.json()["id"] == second.json()["id"]
  conflict = client.post("/checkout-sessions", headers=headers, json={**body, "buyer": {"email": "x@y.z"}})
  assert conflict.status_code == 409


def test_not_found(client):
  resp = client.get("/checkout-sessions/nope", headers=AGENT)
  ErrorResponse.model_validate(resp.json())
  assert resp.json()["messages"][0]["code"] == "not_found"


def test_strict_mode_requires_agent_header(tmp_path):
  settings = dataclasses.replace(load_settings(), db_path=str(tmp_path / "s.db"), strict_negotiation=True)
  strict = TestClient(create_app(settings))
  resp = strict.post("/checkout-sessions", json={"line_items": []})
  assert resp.status_code == 400
  assert resp.json()["code"] == "invalid_profile_url"


def test_admin_reset(client):
  ready_checkout(client)
  assert client.post("/admin/reset").status_code == 403
  assert client.post("/admin/reset", headers={"Authorization": "Bearer secret"}).status_code == 200
  assert client.app.state.db.list_checkouts() == []


def test_negotiation_intersection():
  from app.negotiation import intersect, parse_agent_header

  assert parse_agent_header('profile="https://a.example/p"') == "https://a.example/p"
  assert parse_agent_header("nonsense") is None
  v = "2026-08-25"
  platform = {
    "ucp": {
      "capabilities": {
        "dev.ucp.shopping.checkout": [{"version": v}],
        "dev.ucp.shopping.fulfillment": [{"version": v}],
        "dev.ucp.shopping.order": [{"version": "2026-04-08"}],
      }
    }
  }
  assert intersect(platform) == {"dev.ucp.shopping.checkout", "dev.ucp.shopping.fulfillment"}
  # An extension without its parent is dropped.
  orphan = {"ucp": {"capabilities": {"dev.ucp.shopping.fulfillment": [{"version": v}]}}}
  assert intersect(orphan) == set()
