#!/usr/bin/env python3
"""Independent UCP test client: shops at a UCP store the way an AI agent would.

It only uses the Python standard library, so it runs anywhere Python 3.10+
is installed. It knows nothing about the store's code; it only talks UCP over
HTTP, starting from the store's /.well-known/ucp profile.

Examples:
  python client/ucp_test_client.py                       # local store, all scenarios
  python client/ucp_test_client.py --url https://my-store.run.app --scenario happy
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

AGENT_PROFILE = "https://ucp-test-client.example/profile"
ADDRESS = {
  "full_name": "Test Buyer",
  "street_address": "1 Main St",
  "address_locality": "Kadikoy",
  "address_region": "Istanbul",
  "postal_code": "34710",
  "address_country": "TR",
}
SCENARIOS = {
  "happy": ("success_token", "completed"),
  "decline": ("decline_token", "incomplete"),
  "insufficient": ("fail_token", "incomplete"),
  "timeout": ("timeout_token", "incomplete"),
  "pending": ("pending_token", "completed"),
  "fraud": ("fraud_token", "requires_escalation"),
}


class Failure(Exception):
  pass


class Client:
  def __init__(self, base: str, verbose: bool):
    self.base = base.rstrip("/")
    self.verbose = verbose
    self.endpoint = self.base

  def call(self, method: str, path: str, body=None, timeout=60):
    url = path if path.startswith("http") else f"{self.endpoint}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
      "UCP-Agent": f'profile="{AGENT_PROFILE}"',
      "Accept": "application/json",
      "Request-Id": str(uuid.uuid4()),
    }
    if data is not None:
      headers["Content-Type"] = "application/json"
      headers["Idempotency-Key"] = str(uuid.uuid4())
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
      with urllib.request.urlopen(req, timeout=timeout) as resp:
        status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
      status, raw = exc.code, exc.read()
    try:
      payload = json.loads(raw) if raw else {}
    except ValueError as exc:
      raise Failure(f"{method} {path} did not return JSON (HTTP {status})") from exc
    if self.verbose:
      print(f"    → {method} {url}")
      if body is not None:
        print("      request:", json.dumps(body)[:400])
      print(f"      HTTP {status}:", json.dumps(payload)[:400])
    return status, payload


def expect(condition: bool, message: str):
  if not condition:
    raise Failure(message)


def check_checkout(c: dict):
  for field in ("ucp", "id", "status", "currency", "line_items", "totals", "links"):
    expect(field in c, f"checkout is missing required field '{field}'")
  types = [t["type"] for t in c["totals"]]
  expect(types.count("subtotal") == 1 and types.count("total") == 1, "totals need one subtotal and one total")
  expect("payment_handlers" in c["ucp"], "checkout ucp block must list payment_handlers")


def money(amount: int, currency: str = "TRY") -> str:
  return f"{amount / 100:,.2f} {currency}"


def discover(client: Client) -> dict:
  status, profile = client.call("GET", f"{client.base}/.well-known/ucp")
  expect(status == 200, f"discovery failed with HTTP {status}")
  ucp = profile.get("ucp", {})
  rest = [s for s in ucp.get("services", {}).get("dev.ucp.shopping", []) if s.get("transport") == "rest"]
  expect(rest, "profile has no REST shopping service")
  client.endpoint = rest[0]["endpoint"].rstrip("/")
  caps = sorted(ucp.get("capabilities", {}))
  handlers = ucp.get("payment_handlers", {})
  print(f"  ✓ Store found. UCP version {ucp.get('version')}, endpoint {client.endpoint}")
  print(f"    Capabilities: {', '.join(caps)}")
  print(f"    Payment handlers: {', '.join(h['id'] for v in handlers.values() for h in v)}")
  expect("dev.ucp.shopping.checkout" in caps, "store does not offer checkout")
  return profile


def find_product(client: Client) -> dict:
  status, result = client.call("POST", "/catalog/search", {"query": "beyaz peynir"})
  expect(status == 200 and result.get("products"), "catalog search returned no products")
  in_stock = [v for p in result["products"] for v in p["variants"] if v["availability"]["available"]]
  expect(in_stock, "no product in the search results is in stock")
  variant = in_stock[0]
  price = variant["price"]
  print(
    f"  ✓ Search 'beyaz peynir' → {variant['title']}"
    f" ({money(price['amount'], price['currency'])}), item id {variant['id']}"
  )
  return variant


def run_scenario(client: Client, name: str, variant: dict) -> None:
  token, expected = SCENARIOS[name]
  print(f"\n▶ Scenario '{name}' (test token {token}, expected result: {expected})")

  status, chk = client.call(
    "POST", "/checkout-sessions", {"line_items": [{"item": {"id": variant["id"]}, "quantity": 1}]}
  )
  expect(status in (200, 201), f"create checkout failed with HTTP {status}")
  check_checkout(chk)
  missing = [m.get("path") for m in chk.get("messages", []) if m["type"] == "error"]
  print(f"  ✓ Checkout {chk['id']} created, status {chk['status']}. Store asks for: {', '.join(filter(None, missing))}")

  body = {
    "line_items": [{"id": li["id"], "item": {"id": li["item"]["id"]}, "quantity": li["quantity"]} for li in chk["line_items"]],
    "buyer": {"email": "buyer@example.com", "first_name": "Test", "last_name": "Buyer"},
    "fulfillment": {"methods": [{"type": "shipping", "destinations": [ADDRESS]}]},
  }
  status, chk = client.call("PUT", f"/checkout-sessions/{chk['id']}", body)
  check_checkout(chk)
  expect(chk["status"] == "ready_for_complete", f"expected ready_for_complete, got {chk['status']}: {chk.get('messages')}")
  total = next(t["amount"] for t in chk["totals"] if t["type"] == "total")
  print(f"  ✓ Email and address added. Status {chk['status']}, total {money(total, chk['currency'])}")

  payment = {
    "payment": {
      "instruments": [
        {
          "id": "pi_test",
          "handler_id": "mock_payment_handler",
          "type": "card",
          "selected": True,
          "display": {"brand": "visa", "last_digits": "4242"},
          "credential": {"type": "token", "token": token},
        }
      ]
    }
  }
  started = time.time()
  status, chk = client.call("POST", f"/checkout-sessions/{chk['id']}/complete", payment, timeout=120)
  check_checkout(chk)
  print(f"  ✓ Payment sent. Store answered in {time.time() - started:.1f}s with status {chk['status']}")

  if chk["status"] == "complete_in_progress":
    print("    Payment is processing; checking again every 2 seconds…")
    deadline = time.time() + 60
    while chk["status"] == "complete_in_progress" and time.time() < deadline:
      time.sleep(2)
      _, chk = client.call("GET", f"/checkout-sessions/{chk['id']}")
    print(f"  ✓ Final status {chk['status']}")

  for m in chk.get("messages", []):
    print(f"    Store message [{m['type']}/{m.get('code')}]: {m['content']}")
  if chk["status"] == "requires_escalation":
    print(f"    Buyer must finish at: {chk.get('continue_url')}")
  expect(chk["status"] == expected, f"expected {expected}, got {chk['status']}")

  if chk.get("order"):
    status, order = client.call("GET", f"/orders/{chk['order']['id']}")
    expect(status == 200 and order.get("id") == chk["order"]["id"], "order could not be fetched")
    print(f"  ✓ Order {order['id']} fetched. Buyer page: {order['permalink_url']}")


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--url", default="http://localhost:8080", help="store address")
  parser.add_argument("--scenario", default="all", choices=["all", *SCENARIOS])
  parser.add_argument("--verbose", action="store_true", help="print every request and response")
  args = parser.parse_args()

  client = Client(args.url, args.verbose)
  names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
  failures = 0
  try:
    print(f"Connecting to {args.url}")
    discover(client)
    variant = find_product(client)
  except (Failure, urllib.error.URLError, OSError) as exc:
    print(f"  ✗ {exc}")
    return 1
  for name in names:
    try:
      run_scenario(client, name, variant)
    except (Failure, urllib.error.URLError, OSError) as exc:
      failures += 1
      print(f"  ✗ FAILED: {exc}")
  print(f"\n{len(names) - failures}/{len(names)} scenarios passed.")
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
