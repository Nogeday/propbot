"""timeframe_seconds: only fixed-length intraday bars of Pine's `timeframe.period` are accepted."""

import pytest

from propbot.core.timeframe import MAX_MINUTES, MAX_SECONDS, TimeframeError, timeframe_seconds


@pytest.mark.parametrize(
    ("tf", "expected"),
    [
        ("1", 60),
        ("3", 180),
        ("5", 300),
        ("15", 900),
        ("30", 1_800),
        ("45", 2_700),
        ("60", 3_600),
        ("120", 7_200),
        ("240", 14_400),
        ("999", 59_940),
        ("1000", 60_000),
        ("1440", 86_400),
        ("1S", 1),
        ("5S", 5),
        ("10S", 10),
        ("30S", 30),
        ("59S", 59),
    ],
)
def test_supported_timeframes(tf: str, expected: int) -> None:
    assert timeframe_seconds(tf) == expected


def test_every_minute_and_second_count_in_range() -> None:
    assert MAX_MINUTES == 1440
    assert MAX_SECONDS == 59
    for minutes in range(1, MAX_MINUTES + 1):
        assert timeframe_seconds(str(minutes)) == minutes * 60
    for seconds in range(1, MAX_SECONDS + 1):
        assert timeframe_seconds(f"{seconds}S") == seconds


@pytest.mark.parametrize(
    "tf",
    [
        # daily and longer: the bar closes on the session calendar
        "D",
        "1D",
        "2D",
        "W",
        "1W",
        "M",
        "3M",
        "12M",
        # out of range
        "0",
        "1441",
        "2000",
        "9999",
        "10000",
        "0S",
        "60S",
        "99S",
        "100S",
        # not the exact Pine spelling
        "",
        "S",
        "00",
        "01",
        "005",
        "05S",
        "30s",
        "5m",
        "1h",
        "1H",
        "5M",
        "5 S",
        "S5",
        "+5",
        "-5",
        "5.0",
        "1e2",
        "1_0",
        "0x10",
        # whitespace is never trimmed (a trailing newline defeats `$` in a careless regex)
        " 5",
        "5 ",
        "5\n",
        "30S\n",
        "\t5",
        # digits outside ASCII
        "１５",
        "١٥",
        "5٠",
        # ticks, ranges and other chart types
        "1T",
        "100T",
        "10R",
        "1000R",
    ],
)
def test_unsupported_timeframes_are_refused(tf: str) -> None:
    with pytest.raises(TimeframeError):
        timeframe_seconds(tf)


def test_error_is_a_value_error_with_a_turkish_value_free_message() -> None:
    marker = "s3cr3t-looking-value"

    with pytest.raises(TimeframeError) as excinfo:
        timeframe_seconds(marker)

    assert isinstance(excinfo.value, ValueError)
    message = str(excinfo.value)
    assert "desteklenmeyen zaman dilimi" in message
    assert "günlük ve daha uzun barlar" in message
    assert marker not in message
    assert marker not in repr(excinfo.value)


def test_message_is_the_same_for_every_refusal() -> None:
    messages = set()
    for tf in ("D", "0", "", "60S", "abc"):
        with pytest.raises(TimeframeError) as excinfo:
            timeframe_seconds(tf)
        messages.add(str(excinfo.value))

    assert len(messages) == 1
