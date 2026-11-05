"""The store's UCP discovery profile (/.well-known/ucp) and response metadata."""

from typing import Any

UCP_VERSION = "2026-08-25"
SPEC = f"https://ucp.dev/{UCP_VERSION}"

CHECKOUT = "dev.ucp.shopping.checkout"
FULFILLMENT = "dev.ucp.shopping.fulfillment"
ORDER = "dev.ucp.shopping.order"
CATALOG_SEARCH = "dev.ucp.shopping.catalog.search"
CATALOG_LOOKUP = "dev.ucp.shopping.catalog.lookup"

# Mock payment handler. Same id and test tokens as the official UCP samples,
# plus a few extra scenarios (see app/payments.py).
MOCK_HANDLER_NAME = "dev.ucp.mock_payment"
MOCK_HANDLER_ID = "mock_payment_handler"

# Capabilities this store offers: name -> (spec path, schema path, extends).
BUSINESS_CAPABILITIES: dict[str, tuple[str, str, str | None]] = {
  CHECKOUT: ("shopping/checkout", "shopping/checkout.json", None),
  FULFILLMENT: (
    "shopping/extensions/fulfillment",
    "shopping/fulfillment.json",
    CHECKOUT,
  ),
  ORDER: ("shopping/order", "shopping/order.json", None),
  CATALOG_SEARCH: (
    "shopping/catalog/search",
    "shopping/catalog_search.json",
    None,
  ),
  CATALOG_LOOKUP: (
    "shopping/catalog/lookup",
    "shopping/catalog_lookup.json",
    None,
  ),
}

# Which capabilities each kind of response mentions (spec: "Response
# Capability Selection").
RESPONSE_ROOTS = {
  "checkout": CHECKOUT,
  "order": ORDER,
  "catalog_search": CATALOG_SEARCH,
  "catalog_lookup": CATALOG_LOOKUP,
}


def mock_handler_declaration() -> dict[str, Any]:
  return {
    "id": MOCK_HANDLER_ID,
    "version": UCP_VERSION,
    "available_instruments": [{"type": "card"}],
    "config": {
      "environment": "sandbox",
      "description": (
        "Test-only payment handler. No real money moves. Send an instrument"
        " with handler_id 'mock_payment_handler', type 'card' and"
        " credential {type: 'token', token: <test token>}."
      ),
      "supported_tokens": {
        "success_token": "Payment approved, order is placed.",
        "fail_token": "Declined: insufficient funds.",
        "decline_token": "Declined: card declined by issuer.",
        "fraud_token": "Held for buyer review (requires_escalation).",
        "timeout_token": "Processor times out after a delay, then fails.",
        "pending_token": "Accepted asynchronously; completes a few seconds later.",
      },
    },
  }


def business_profile(base_url: str) -> dict[str, Any]:
  capabilities: dict[str, list[dict[str, Any]]] = {}
  for name, (spec, schema, extends) in BUSINESS_CAPABILITIES.items():
    entry: dict[str, Any] = {
      "version": UCP_VERSION,
      "spec": f"{SPEC}/specification/{spec}",
      "schema": f"{SPEC}/schemas/{schema}",
    }
    if extends:
      entry["extends"] = extends
    capabilities[name] = [entry]
  return {
    "ucp": {
      "version": UCP_VERSION,
      "services": {
        "dev.ucp.shopping": [
          {
            "version": UCP_VERSION,
            "spec": f"{SPEC}/specification/overview",
            "transport": "rest",
            "endpoint": base_url,
            "schema": f"{SPEC}/services/shopping/rest.openapi.json",
          }
        ]
      },
      "capabilities": capabilities,
      "payment_handlers": {MOCK_HANDLER_NAME: [mock_handler_declaration()]},
    }
  }


def response_ucp(
  kind: str, active: set[str], *, status: str = "success"
) -> dict[str, Any]:
  """The `ucp` block placed at the top of every response."""
  root = RESPONSE_ROOTS[kind]
  names = [root] if root in active else []
  for name, (_, _, extends) in BUSINESS_CAPABILITIES.items():
    if extends == root and name in active:
      names.append(name)
  block: dict[str, Any] = {
    "version": UCP_VERSION,
    "status": status,
    "capabilities": {n: [{"version": UCP_VERSION}] for n in names},
  }
  if kind == "checkout":
    block["payment_handlers"] = {
      MOCK_HANDLER_NAME: [
        {
          "id": MOCK_HANDLER_ID,
          "version": UCP_VERSION,
          "available_instruments": [{"type": "card"}],
          "config": {"environment": "sandbox"},
        }
      ]
    }
  return block


def error_response(
  kind: str,
  active: set[str],
  code: str,
  content: str,
  *,
  severity: str = "unrecoverable",
  continue_url: str | None = None,
) -> dict[str, Any]:
  body: dict[str, Any] = {
    "ucp": response_ucp(kind, active, status="error"),
    "messages": [
      {"type": "error", "code": code, "content": content, "severity": severity}
    ],
  }
  if continue_url:
    body["continue_url"] = continue_url
  return body
