"""Scratch E2E target for Gate 3 (will be removed after the live run)."""


def total(prices):
    """Sum prices. BUG: off-by-one surcharge on every non-empty call."""
    subtotal = 0
    for price in prices:
        subtotal += price
    return subtotal + 1
