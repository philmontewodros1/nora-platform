# Prices in ETB. These match the spec's sample conversation
# (NOC 12kg — 1,700 ETB, delivery — 150 ETB, total — 1,850 ETB).
# Replace with real shop prices gathered during the pilot — and if you change
# one here, update the spec doc's example copy too so they stay consistent.
#
# Keyed by (product, size) -> price. Brand does not affect price in this simple
# model; add brand-specific pricing later if needed.

import os

PRODUCT_PRICES = {
    ("gas", "6kg"): 900,
    ("gas", "12kg"): 1700,
    ("gas", "22kg"): 3100,
    ("water", "jar"): 120,
    ("butane", "canister"): 250,
}

# Configurable so pilots can tune per-area pricing without a code change.
DELIVERY_FEE = float(os.getenv("NORA_DELIVERY_FEE", "150"))
DEFAULT_ETA_MINUTES = int(os.getenv("NORA_DEFAULT_ETA_MINUTES", "45"))

DEPOSIT_FEES = {
    "gas": 2000,      # refundable cylinder deposit for first-time customers
    "water": 300,     # refundable jar deposit
    "butane": 400,
}


def price_for(product: str, size: str) -> float:
    return PRODUCT_PRICES.get((product, size), 0)


def deposit_for(product: str) -> float:
    return DEPOSIT_FEES.get(product, 0)
