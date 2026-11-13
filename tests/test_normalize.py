from decimal import Decimal

import pytest

from solution import normalize


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("+91 98765-43210", "+919876543210"),
    ],
)
def test_normalize_phone_accepts_supported_formats(value, expected):
    assert normalize.normalize_phone(value) == expected


@pytest.mark.parametrize("value", ["987654321", "98765432100", "5876543210"])
def test_normalize_phone_rejects_invalid_length_or_start(value):
    assert normalize.normalize_phone(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-12-01", "2026-12-01"),
        ("01/12/2026", "2026-12-01"),
        ("01-12-2026", "2026-12-01"),
        ("01-Dec-2026", "2026-12-01"),
        ("01-dec-2026", "2026-12-01"),
    ],
)
def test_normalize_date_accepts_supported_formats(value, expected):
    assert normalize.normalize_date(value) == expected


@pytest.mark.parametrize(
    "value", ["31/02/2026", "", "2026/12/01", "2026-1-1"]
)
def test_normalize_date_rejects_invalid_or_unsupported_values(value):
    assert normalize.normalize_date(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("₹1,234.567", Decimal("1234.57")),
        ("Rs. 25", Decimal("25.00")),
        ("inr 10", Decimal("10.00")),
        ("1.235", Decimal("1.24")),
    ],
)
def test_normalize_decimal_accepts_supported_values(value, expected):
    assert normalize.normalize_decimal(value) == expected


@pytest.mark.parametrize("value", ["-1", "12x", "USD 25"])
def test_normalize_decimal_rejects_invalid_values(value):
    assert normalize.normalize_decimal(value) is None


def test_normalize_enum_matches_case_insensitively_and_returns_lowercase():
    assert normalize.normalize_enum("en", ["EN", "HI"]) == "en"


def test_normalize_enum_returns_none_for_unmatched_value():
    assert normalize.normalize_enum("ta", ["en", "hi"]) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("policy_expiry", "policy expiry"),
        ("Policy Expiry", "policy expiry"),
        ("  POLICY__\tEXPIRY  ", "policy expiry"),
    ],
)
def test_normalize_header_normalizes_case_and_separators(value, expected):
    assert normalize.normalize_header(value) == expected
