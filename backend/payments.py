"""
Payment integration plumbing.

STATUS: stub. Orders get marked paid without a real TeleBirr/CBE Birr charge
going through. This module is the single swap point: wire a real provider's
merchant API into `initiate_payment` and `verify_payment` and the rest of the
flow (order confirmation, notifications) keeps working unchanged.

TeleBirr and CBE Birr merchant API docs are not publicly available without a
registered merchant account — get those from your provider rep before wiring
real charges.
"""
import os
import secrets
from models import Order, PaymentMethod


def initiate_payment(order: Order, method: PaymentMethod) -> dict:
    """Return what the channel needs to send the customer to pay.

    For a real provider this would be a checkout URL / USSD shortcode / payment
    token. The stub returns a fake reference so the rest of the flow is testable
    end-to-end without a merchant account.
    """
    ref = f"NORA-{order.id}-{secrets.token_hex(3)}".upper()
    order.payment_method = method
    order.payment_ref = ref
    if method == PaymentMethod.cash:
        # cash on delivery — paid flag stays false until the rider collects
        return {
            "method": method.value,
            "reference": ref,
            "amount_etb": order.total_price,
            "instructions": "Pay cash to the rider on delivery.",
            "requires_redirect": False,
        }
    # telebirr / cbe — would return a redirect URL / USSD prompt from the provider
    fake_url = f"https://pay.example.com/{ref}"  # replace with real provider checkout
    return {
        "method": method.value,
        "reference": ref,
        "amount_etb": order.total_price,
        "checkout_url": fake_url,
        "requires_redirect": True,
    }


def verify_payment(order: Order) -> bool:
    """For the stub, a payment is 'verified' as soon as a reference exists.
    Replace with a real transaction-status lookup against the provider."""
    return bool(order.payment_ref)
