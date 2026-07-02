"""
Payment safety guard.

The problem this fixes: without this, a bot can tell a rider "order is paid" and dispatch a
real cylinder from a real shop when no money has actually moved -- because the payment
"integration" is a stub that fakes success. That's fine in development. It is not fine the
moment a stranger can message the bot.

The fix: online payment methods (TeleBirr, CBE) are only offered to customers when
PAYMENTS_LIVE=true is explicitly set in the environment AND a real provider webhook secret is
configured. Until then, the ONLY payment method offered is cash on delivery, which has no
false-positive "paid" state -- the rider physically collects money, and the order is marked
paid only when the rider confirms that on delivery (see mark_delivered in crud.py).

This is intentionally conservative: it removes a payment option rather than risk a fake one.
"""
import os
import logging

log = logging.getLogger("nora.payments")

PAYMENTS_LIVE = os.getenv("PAYMENTS_LIVE", "false").strip().lower() == "true"
TELEBIRR_WEBHOOK_SECRET = os.getenv("TELEBIRR_WEBHOOK_SECRET", "")
CBE_WEBHOOK_SECRET = os.getenv("CBE_WEBHOOK_SECRET", "")


def telebirr_ready() -> bool:
    """TeleBirr should only be offered if PAYMENTS_LIVE is on AND a webhook secret is set --
    both, not either, because PAYMENTS_LIVE alone doesn't prove the provider is wired."""
    return PAYMENTS_LIVE and bool(TELEBIRR_WEBHOOK_SECRET)


def cbe_ready() -> bool:
    return PAYMENTS_LIVE and bool(CBE_WEBHOOK_SECRET)


def available_payment_methods() -> list[dict]:
    """Call this wherever the bot builds the payment-method choice buttons, instead of
    hardcoding [TeleBirr, CBE, Cash]. Cash is always available. Online methods only appear
    once they're genuinely wired -- this is the actual fix, not a config comment."""
    methods = [{"id": "cash", "label": "Cash on delivery"}]
    if telebirr_ready():
        methods.append({"id": "telebirr", "label": "TeleBirr"})
    if cbe_ready():
        methods.append({"id": "cbe", "label": "CBE Birr"})
    return methods


def mark_paid_is_safe(payment_method: str) -> bool:
    """Call this before ANY code path sets order.paid = True. Returns False for telebirr/cbe
    unless they're actually ready -- this is the hard stop that prevents the false-paid state,
    even if a bug elsewhere in the order flow tries to mark an unready method as paid."""
    if payment_method == "cash":
        return False  # cash is only marked paid by the rider on delivery, never at order time
    if payment_method == "telebirr":
        return telebirr_ready()
    if payment_method == "cbe":
        return cbe_ready()
    return False


def startup_warning() -> str | None:
    """Call once at app startup and log the result loudly. Returns None if nothing's wrong."""
    if not PAYMENTS_LIVE:
        return ("PAYMENTS_LIVE is not set to true -- only cash on delivery is being offered "
                "to customers. This is the safe default. Do not set PAYMENTS_LIVE=true until "
                "TELEBIRR_WEBHOOK_SECRET / CBE_WEBHOOK_SECRET are wired to a real provider "
                "integration, or the system will mark unpaid orders as paid.")
    if PAYMENTS_LIVE and not (TELEBIRR_WEBHOOK_SECRET or CBE_WEBHOOK_SECRET):
        return ("PAYMENTS_LIVE=true but no webhook secrets are set -- online payment methods "
                "are still hidden, so this has no effect yet, but PAYMENTS_LIVE should stay "
                "false until secrets are actually configured, to avoid confusion.")
    return None
