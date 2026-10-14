"""Decimal arithmetic; all monetary values are CAD."""
from decimal import Decimal, ROUND_FLOOR


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Amounts must be finite and nonnegative")
    return result


def calculate(*, retail, retail_tax=13, retail_delivery=0, premium=15,
              auction_tax=13, premium_taxable=True, shipping=0,
              shipping_taxable=False, fees=0, fees_taxable=True,
              current=0, increment=1, discount=70):
    values = {k: decimal(v) for k, v in locals().copy().items()
              if k not in {"premium_taxable", "shipping_taxable", "fees_taxable"}}
    if values["increment"] <= 0 or not 70 <= values["discount"] <= 95:
        raise ValueError("Positive bid increment and 70–95% discount required")
    if any(values[k] > 100 for k in ("retail_tax", "premium", "auction_tax")):
        raise ValueError("Rates cannot exceed 100%")
    one, hundred = Decimal(1), Decimal(100)
    tax = values["auction_tax"] / hundred
    rate = values["premium"] / hundred
    multiplier = one + tax + rate * (one + tax if premium_taxable else one)
    fixed = values["shipping"] * (one + tax if shipping_taxable else one)
    fixed += values["fees"] * (one + tax if fees_taxable else one)
    retail_total = values["retail"] * (one + values["retail_tax"] / hundred) + values["retail_delivery"]
    budget = retail_total * (one - values["discount"] / hundred)
    affordable = retail_total > 0 and fixed <= budget
    raw = max(Decimal(0), (budget - fixed) / multiplier)
    ceiling = (raw / values["increment"]).to_integral_value(rounding=ROUND_FLOOR) * values["increment"]
    total = values["current"] * multiplier + fixed
    savings = hundred * (one - total / retail_total) if retail_total else Decimal(0)
    return {"retail_total": retail_total, "budget": budget, "max_hammer": ceiling,
            "current_total": total, "savings_percent": savings,
            "affordable": affordable, "meets_discount": affordable and total <= budget,
            "above_ceiling": values["current"] > ceiling}
