
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def normalize_phone(value):
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def normalize_date(value):
    formats = (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y"),
        (r"\d{2}-\d{2}-\d{4}", "%d-%m-%Y"),
        (r"\d{2}-[A-Za-z]{3}-\d{4}", "%d-%b-%Y"),
    )
    for pattern, date_format in formats:
        if re.fullmatch(pattern, value):
            try:
                return datetime.strptime(value, date_format).date().isoformat()
            except ValueError:
                return None
    return None


def normalize_decimal(value):
    cleaned = re.sub(
        r"^(?:₹|Rs\.?|INR)\s*", "", value, flags=re.IGNORECASE
    ).replace(",", "")
    try:
        amount = Decimal(cleaned)
        if not amount.is_finite() or amount < 0:
            return None
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def normalize_enum(value, values):
    normalized_value = value.lower()
    for candidate in values:
        if candidate.lower() == normalized_value:
            return candidate.lower()
    return None


def normalize_header(value):
    return re.sub(r"[_\s]+", " ", value.strip().lower())
