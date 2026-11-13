"""Telegram number and time formatting."""

import datetime as dt

import pytest

from propbot.core.clock import ms_from_datetime
from propbot.notify.format import (
    ISTANBUL,
    NEW_YORK,
    fmt_clock,
    fmt_date,
    fmt_duration,
    fmt_lots,
    fmt_money,
    fmt_percent,
    fmt_price,
    fmt_signed_points,
    local_date,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5123.25, "5123.25"),
        (12.5, "12.50"),
        (5123.0, "5123.00"),
        (1.08345, "1.08345"),
        (0.1 + 0.2, "0.30"),
        (3.125, "3.125"),
        (0.0, "0.00"),
    ],
)
def test_price_keeps_two_to_five_decimals(value: float, expected: str) -> None:
    assert fmt_price(value) == expected


def test_signed_points_use_a_real_minus_sign() -> None:
    assert fmt_signed_points(-12.5) == "−12.50"
    assert fmt_signed_points(25.0) == "+25.00"
    assert fmt_signed_points(0.0) == "+0.00"


def test_lots_show_at_least_two_decimals() -> None:
    assert fmt_lots(20.0) == "20.00"
    assert fmt_lots(0.375) == "0.375"


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (250.0, "USD", "$250"),
        (249.5, "USD", "$249.50"),
        (1000.004, "USD", "$1000"),
        (250.0, "EUR", "250 EUR"),
        (0.0, "USD", "$0"),
    ],
)
def test_money(amount: float, currency: str, expected: str) -> None:
    assert fmt_money(amount, currency) == expected


def test_percent_drops_padding_zeros() -> None:
    assert fmt_percent(0.25) == "%0.25"
    assert fmt_percent(1.0) == "%1"


def test_clock_and_date_follow_the_zone_including_dst() -> None:
    # 2026-11-13 14:45 UTC: New York is on EST (UTC-5), Istanbul is always UTC+3.
    winter = ms_from_datetime(dt.datetime(2026, 11, 13, 14, 45, 30, tzinfo=dt.UTC))
    # 2026-07-14 13:45 UTC: New York is on EDT (UTC-4).
    summer = ms_from_datetime(dt.datetime(2026, 7, 14, 13, 45, tzinfo=dt.UTC))
    late = ms_from_datetime(dt.datetime(2026, 11, 13, 22, 30, tzinfo=dt.UTC))

    assert fmt_clock(winter, NEW_YORK) == "09:45"
    assert fmt_clock(winter, ISTANBUL, seconds=True) == "17:45:30"
    assert fmt_clock(summer, NEW_YORK) == "09:45"
    assert fmt_clock(summer, ISTANBUL) == "16:45"
    assert fmt_date(late, ISTANBUL) == "2026-11-14"
    assert fmt_date(late, NEW_YORK) == "2026-11-13"
    assert local_date(late, ISTANBUL) == dt.date(2026, 11, 14)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 sn"),
        (-5, "0 sn"),
        (42.4, "42 sn"),
        (60, "1 dk"),
        (200, "3 dk 20 sn"),
        (3600, "1 sa"),
        (7500, "2 sa 5 dk"),
        (86400, "1 gün"),
        (273600, "3 gün 4 sa"),
    ],
)
def test_duration(seconds: float, expected: str) -> None:
    assert fmt_duration(seconds) == expected
