"""UCP Test Market: a small UCP (Universal Commerce Protocol) merchant server.

Run locally:  uvicorn app.main:app --reload --port 8080
"""

import hashlib
import html
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import catalog
from .checkout import CheckoutService
from .config import load_settings
from .db import Database
from .negotiation import NegotiationError, negotiate
from .profile import (
  BUSINESS_CAPABILITIES,
  RESPONSE_ROOTS,
  UCP_VERSION,
  business_profile,
  error_response,
  response_ucp,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ucp.store")


def create_app(settings=None) -> FastAPI:
  settings = settings or load_settings()
  db = Database(settings.db_path)
  checkouts = CheckoutService(db, settings)
  app = FastAPI(title=settings.store_name, version="0.1.0", docs_url="/docs")
  app.state.db = db
  app.state.settings = settings

  # Helpers ------------------------------------------------------------------

  def base_url(request: Request) -> str:
    if settings.base_url:
      return settings.base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"

  def protocol_error(status: int, code: str, content: str) -> JSONResponse:
    return JSONResponse({"code": code, "content": content}, status_code=status)

  async def read_json(request: Request) -> tuple[dict[str, Any] | None, bytes]:
    raw = await request.body()
    if not raw.strip():
      return {}, raw
    try:
      data = json.loads(raw)
    except ValueError:
      return None, raw
    return (data if isinstance(data, dict) else None), raw

  def negotiated(request: Request, kind_root: str):
    """Returns (active capability set, None) or (None, error response)."""
    try:
      result = negotiate(request.headers.get("ucp-agent"), settings)
    except NegotiationError as exc:
      return None, protocol_error(exc.status, exc.code, exc.content)
    if kind_root not in result.active:
      if settings.strict_negotiation:
        kind = {v: k for k, v in _ROOTS.items()}[kind_root]
        body = error_response(
          kind,
          result.active,
          "capabilities_incompatible",
          f"Your profile does not declare {kind_root} at version {UCP_VERSION}.",
          continue_url=base_url(request),
        )
        return None, JSONResponse(body)
      log.info("Capability %s not negotiated; lenient mode uses all", kind_root)
      result.active = set(_ALL)
    log.info("%s %s agent=%s (%s)", request.method, request.url.path, result.profile_url, result.note)
    return result.active, None

  def with_idempotency(request: Request, raw: bytes, handler):
    key = request.headers.get("idempotency-key")
    if not key:
      return handler()
    scope = f"{request.method} {request.url.path}"
    digest = hashlib.sha256(raw).hexdigest()
    hit = db.get_idempotent(key, scope)
    if hit is not None:
      if hit["request_hash"] != digest:
        return protocol_error(
          409, "idempotency_conflict", "Idempotency-Key was already used with a different body."
        )
      return Response(hit["response"], status_code=hit["status_code"], media_type="application/json")
    response = handler()
    if response.status_code < 500:
      db.put_idempotent(key, scope, digest, response.status_code, response.body.decode())
    return response

  def checkout_json(view, active, status_code=200) -> JSONResponse:
    return JSONResponse({"ucp": response_ucp("checkout", active), **view}, status_code=status_code)

  def not_found(kind, active, what) -> JSONResponse:
    return JSONResponse(error_response(kind, active, "not_found", f"{what} not found."))

  # Discovery ----------------------------------------------------------------

  @app.get("/.well-known/ucp")
  def well_known(request: Request):
    return JSONResponse(business_profile(base_url(request)), headers={"Cache-Control": "public, max-age=300"})

  # Catalog ------------------------------------------------------------------

  @app.post("/catalog/search")
  async def catalog_search(request: Request):
    active, err = negotiated(request, _ROOTS["catalog_search"])
    if err:
      return err
    body, _ = await read_json(request)
    if body is None:
      return protocol_error(400, "invalid_request", "Body must be a JSON object.")
    products, page = catalog.search(db, body, base_url(request))
    return JSONResponse({"ucp": response_ucp("catalog_search", active), "products": products, "pagination": page})

  @app.post("/catalog/lookup")
  async def catalog_lookup(request: Request):
    active, err = negotiated(request, _ROOTS["catalog_lookup"])
    if err:
      return err
    body, _ = await read_json(request)
    if body is None or not isinstance(body.get("ids"), list):
      return protocol_error(400, "invalid_request", "Body must contain an 'ids' array.")
    products, missing = catalog.lookup(db, body["ids"], base_url(request))
    out: dict[str, Any] = {"ucp": response_ucp("catalog_lookup", active), "products": products}
    if missing:
      out["messages"] = [
        {"type": "info", "code": "not_found", "content": f"Not found: {', '.join(missing)}."}
      ]
    return JSONResponse(out)

  @app.post("/catalog/product")
  async def catalog_product(request: Request):
    active, err = negotiated(request, _ROOTS["catalog_lookup"])
    if err:
      return err
    body, _ = await read_json(request)
    if body is None or not isinstance(body.get("id"), str):
      return protocol_error(400, "invalid_request", "Body must contain an 'id' string.")
    products, _ = catalog.lookup(db, [body["id"]], base_url(request))
    if not products:
      return not_found("catalog_lookup", active, "Product")
    return JSONResponse({"ucp": response_ucp("catalog_lookup", active), "product": products[0]})

  # Checkout -----------------------------------------------------------------

  @app.post("/checkout-sessions")
  async def create_checkout(request: Request):
    active, err = negotiated(request, _ROOTS["checkout"])
    if err:
      return err
    body, raw = await read_json(request)
    if body is None:
      return protocol_error(400, "invalid_request", "Body must be a JSON object.")

    def run():
      view, message = checkouts.create(body, base_url(request), active)
      if view is None:
        out = error_response("checkout", active, message["code"], message["content"], continue_url=base_url(request))
        return JSONResponse(out)
      return checkout_json(view, active, 201)

    return await run_in_threadpool(with_idempotency, request, raw, run)

  @app.get("/checkout-sessions/{checkout_id}")
  def get_checkout(checkout_id: str, request: Request):
    active, err = negotiated(request, _ROOTS["checkout"])
    if err:
      return err
    view = checkouts.get(checkout_id, base_url(request), active)
    return checkout_json(view, active) if view else not_found("checkout", active, "Checkout")

  @app.put("/checkout-sessions/{checkout_id}")
  async def update_checkout(checkout_id: str, request: Request):
    active, err = negotiated(request, _ROOTS["checkout"])
    if err:
      return err
    body, raw = await read_json(request)
    if body is None:
      return protocol_error(400, "invalid_request", "Body must be a JSON object.")

    def run():
      view = checkouts.update(checkout_id, body, base_url(request), active)
      return checkout_json(view, active) if view else not_found("checkout", active, "Checkout")

    return await run_in_threadpool(with_idempotency, request, raw, run)

  @app.post("/checkout-sessions/{checkout_id}/complete")
  async def complete_checkout(checkout_id: str, request: Request):
    active, err = negotiated(request, _ROOTS["checkout"])
    if err:
      return err
    body, raw = await read_json(request)
    if body is None:
      return protocol_error(400, "invalid_request", "Body must be a JSON object.")

    def run():
      view = checkouts.complete(checkout_id, body, base_url(request), active)
      return checkout_json(view, active) if view else not_found("checkout", active, "Checkout")

    return await run_in_threadpool(with_idempotency, request, raw, run)

  @app.post("/checkout-sessions/{checkout_id}/cancel")
  async def cancel_checkout(checkout_id: str, request: Request):
    active, err = negotiated(request, _ROOTS["checkout"])
    if err:
      return err
    _, raw = await read_json(request)

    def run():
      view = checkouts.cancel(checkout_id, base_url(request), active)
      return checkout_json(view, active) if view else not_found("checkout", active, "Checkout")

    return await run_in_threadpool(with_idempotency, request, raw, run)

  # Orders -------------------------------------------------------------------

  @app.get("/orders/{order_id}")
  def get_order(order_id: str, request: Request):
    active, err = negotiated(request, _ROOTS["order"])
    if err:
      return err
    order = db.get_order(order_id)
    if order is None:
      return not_found("order", active, "Order")
    public = {k: v for k, v in order.items() if not k.startswith("_")}
    return JSONResponse({"ucp": response_ucp("order", active), **public})

  # Human pages (buyer handoff, order status, store front) --------------------

  @app.get("/", response_class=HTMLResponse)
  def home(request: Request):
    rows = "".join(
      f"<tr><td>{html.escape(p['variant_id'])}</td><td>{html.escape(p['title'])}</td>"
      f"<td>${p['price'] / 100:,.2f}</td><td>{p['inventory']}</td></tr>"
      for p in db.list_products()
    )
    body = (
      f"<p>This is a <b>test store</b> that speaks UCP version {UCP_VERSION}. "
      f"No real orders or payments. Discovery profile: "
      f"<a href='/.well-known/ucp'>/.well-known/ucp</a> · API docs: <a href='/docs'>/docs</a></p>"
      f"<table><tr><th>Item id</th><th>Product</th><th>Price</th><th>Stock</th></tr>{rows}</table>"
    )
    return _page(settings.store_name, body)

  @app.get("/products/{handle}", response_class=HTMLResponse)
  def product_page(handle: str):
    p = db.get_product(handle)
    if p is None:
      return HTMLResponse(_page("Not found", "<p>Product not found.</p>"), status_code=404)
    return _page(
      p["title"],
      f"<p>{html.escape(p['description'])}</p><p><b>${p['price'] / 100:,.2f}</b> · "
      f"item id <code>{html.escape(p['variant_id'])}</code> · stock {p['inventory']}</p>",
    )

  @app.get("/continue/{checkout_id}", response_class=HTMLResponse)
  def continue_page(checkout_id: str, request: Request):
    view = checkouts.get(checkout_id, base_url(request), set(_ALL))
    if view is None:
      return HTMLResponse(_page("Not found", "<p>Checkout not found.</p>"), status_code=404)
    items = "".join(
      f"<li>{li['quantity']} × {html.escape(li['item']['title'])}</li>" for li in view["line_items"]
    )
    total = next(t["amount"] for t in view["totals"] if t["type"] == "total")
    msgs = "".join(f"<li>{html.escape(m['content'])}</li>" for m in view.get("messages", []))
    action = ""
    if view["status"] == "requires_escalation":
      action = (
        f"<form method='post' action='/continue/{html.escape(checkout_id)}/approve'>"
        "<button>Approve this test order</button></form>"
      )
    return _page(
      "Your checkout",
      f"<p>Status: <b>{view['status']}</b></p><ul>{items}</ul><p>Total: ${total / 100:,.2f}</p>"
      f"{'<p>Notes:</p><ul>' + msgs + '</ul>' if msgs else ''}{action}",
    )

  @app.post("/continue/{checkout_id}/approve")
  def approve(checkout_id: str, request: Request):
    checkouts.approve_review(checkout_id, base_url(request), set(_ALL))
    return RedirectResponse(f"/continue/{checkout_id}", status_code=303)

  @app.get("/order-status/{order_id}", response_class=HTMLResponse)
  def order_page(order_id: str):
    order = db.get_order(order_id)
    if order is None:
      return HTMLResponse(_page("Not found", "<p>Order not found.</p>"), status_code=404)
    items = "".join(
      f"<li>{li['quantity']['total']} × {html.escape(li['item']['title'])}</li>" for li in order["line_items"]
    )
    total = next(t["amount"] for t in order["totals"] if t["type"] == "total")
    return _page(
      order["label"], f"<p>Status: processing (test order)</p><ul>{items}</ul><p>Total: ${total / 100:,.2f}</p>"
    )

  @app.get("/terms", response_class=HTMLResponse)
  def terms():
    return _page("Terms of service", "<p>This is a test store. Nothing is sold, shipped or charged.</p>")

  @app.get("/privacy", response_class=HTMLResponse)
  def privacy():
    return _page("Privacy policy", "<p>Test data only. Do not send real personal or card data.</p>")

  # Operations ---------------------------------------------------------------

  @app.get("/healthz")
  def healthz():
    return {"ok": True}

  @app.post("/admin/reset")
  def admin_reset(request: Request):
    token = settings.admin_token
    if not token or request.headers.get("authorization") != f"Bearer {token}":
      return protocol_error(403, "forbidden", "Admin token required.")
    db.reset()
    return {"ok": True, "message": "Checkouts, orders and stock were reset."}

  return app


_ROOTS = RESPONSE_ROOTS
_ALL = set(BUSINESS_CAPABILITIES)


def _page(title: str, body: str) -> HTMLResponse:
  return HTMLResponse(
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    f"<title>{html.escape(title)}</title><style>body{{font-family:system-ui,sans-serif;"
    "max-width:760px;margin:2rem auto;padding:0 16px;line-height:1.5}"
    "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;"
    "padding:6px;text-align:left}</style></head>"
    f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
  )


app = create_app()
