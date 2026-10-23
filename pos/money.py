from decimal import Decimal, ROUND_HALF_UP

from rest_framework.exceptions import ValidationError


def taka_to_paisa(value, allow_negative: bool = False) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValidationError("Enter a valid amount.") from exc
    if amount < 0 and not allow_negative:
        raise ValidationError("Amount cannot be negative.")
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if quantized != amount:
        raise ValidationError("Use at most two decimal places.")
    return int(quantized * 100)


def paisa_to_taka(paisa: int) -> str:
    return f"{Decimal(paisa) / Decimal(100):.2f}"


def percent_to_bps(value) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValidationError("Enter a valid discount rate.") from exc
    if amount < 0 or amount > 100:
        raise ValidationError("Discount rate must be between 0 and 100.")
    bps = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(bps)


def bps_to_percent(bps: int) -> str:
    return f"{Decimal(bps) / Decimal(100):.2f}"


def selling_paisa(mrp_paisa: int, discount_bps: int) -> int:
    return (mrp_paisa * (10000 - discount_bps) + 5000) // 10000
