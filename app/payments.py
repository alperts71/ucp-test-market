"""Mock payment handler.

No real payment provider is called. The token inside the payment credential
decides what happens, so an agent (or the test client) can try every path:

  success_token  -> approved, order placed
  fail_token     -> declined, insufficient funds
  decline_token  -> declined by card issuer
  fraud_token    -> held for buyer review (checkout goes to requires_escalation)
  timeout_token  -> processor does not answer for a while, then fails
  pending_token  -> accepted, finishes asynchronously a few seconds later
"""

from dataclasses import dataclass
from typing import Any

from .profile import MOCK_HANDLER_ID


@dataclass
class PaymentResult:
  # approved | declined | review | timeout | pending | invalid
  outcome: str
  code: str = ""
  content: str = ""


TOKENS = {
  "success_token": PaymentResult("approved"),
  "fail_token": PaymentResult(
    "declined", "payment_failed", "Payment declined: insufficient funds (mock)."
  ),
  "decline_token": PaymentResult(
    "declined", "payment_failed", "Payment declined by the card issuer (mock)."
  ),
  "fraud_token": PaymentResult(
    "review",
    "payment_review_required",
    "Payment flagged for review. The buyer must confirm the order (mock).",
  ),
  "timeout_token": PaymentResult(
    "timeout",
    "payment_failed",
    "Payment processor timed out. No charge was made; try again (mock).",
  ),
  "pending_token": PaymentResult("pending"),
}


def authorize(instrument: dict[str, Any]) -> PaymentResult:
  if instrument.get("handler_id") != MOCK_HANDLER_ID:
    return PaymentResult(
      "invalid",
      "payment_failed",
      f"Unsupported payment handler '{instrument.get('handler_id')}'."
      f" Use '{MOCK_HANDLER_ID}'.",
    )
  credential = instrument.get("credential")
  if not isinstance(credential, dict) or not isinstance(
    credential.get("token"), str
  ):
    return PaymentResult(
      "invalid",
      "payment_failed",
      "Payment credential must be {type: 'token', token: '<test token>'}."
      " Raw card numbers are not accepted.",
    )
  token = credential["token"]
  result = TOKENS.get(token)
  if result is None:
    return PaymentResult(
      "invalid",
      "payment_failed",
      f"Unknown test token '{token}'. Valid tokens: {', '.join(TOKENS)}.",
    )
  return result
