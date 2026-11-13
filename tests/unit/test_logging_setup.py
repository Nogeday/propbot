"""Logging setup: JSON lines, redaction of structlog and stdlib records, idempotent set-up."""

import datetime as dt
import io
import json
import logging
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import pytest
import structlog

from propbot.logging_setup import (
    INFO_FLOOR_LOGGERS,
    MIN_SECRET_LENGTH,
    QUIET_LOGGERS,
    REDACTED,
    configure_logging,
    redact,
    reset_logging,
)

# Made-up values in the real formats.
BOT_TOKEN = "7000000001:AAF-fake_bot_key_0123456789abcdefghi"
WEBHOOK_TOKEN = "wh_0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ_abc"
PIN = "424242"
SECRETS = (BOT_TOKEN, WEBHOOK_TOKEN, PIN)

# A logger created at import time, before any configure_logging call, like module-level
# loggers in the application.
EARLY_LOG = structlog.get_logger("propbot.early")


@pytest.fixture(autouse=True)
def logging_sandbox() -> Iterator[None]:
    """Every test starts and ends without the propbot handler on the root logger."""
    reset_logging()
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
    reset_logging()


def start(*, level: str = "INFO", secrets: tuple[str, ...] = SECRETS) -> io.StringIO:
    stream = io.StringIO()
    configure_logging(secrets=secrets, level=level, stream=stream)
    return stream


def _not_json(constant: str) -> float:
    raise AssertionError(f"not strict JSON: {constant}")


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """Every line of the stream, parsed as one strict JSON object."""
    parsed: list[dict[str, Any]] = []
    for line in stream.getvalue().splitlines():
        entry = json.loads(line, parse_constant=_not_json)
        assert isinstance(entry, dict)
        parsed.append(entry)
    return parsed


def only_line(stream: io.StringIO) -> dict[str, Any]:
    (entry,) = lines(stream)
    return entry


def own_handlers(before: list[logging.Handler]) -> list[logging.Handler]:
    return [handler for handler in logging.getLogger().handlers if handler not in before]


class Opaque:
    """An object the JSON renderer cannot serialize; its repr holds a secret."""

    def __repr__(self) -> str:
        return f"Opaque(token={BOT_TOKEN})"


# --------------------------------------------------------------------------- redact()


@pytest.mark.parametrize(
    ("text", "secrets", "expected"),
    [
        ("abcd1234", ["abcd1234"], "***"),
        ("token=abcd1234;", ["abcd1234"], "token=***;"),
        ("abcd1234 and abcd1234", ["abcd1234"], "*** and ***"),
        ("abcd1234abcd1234", ["abcd1234"], "******"),
        ("pin 424242, key SECRETKEY", ["424242", "SECRETKEY"], "pin ***, key ***"),
        ("nothing to hide", ["abcd1234"], "nothing to hide"),
        ("", ["abcd1234"], ""),
        ("anything", [], "anything"),
        ("anything", ["", "a", "an", "any"], "anything"),
        ("xany4", ["any4"], "x***"),
        # Regular-expression characters in a secret are literal.
        ("a.c+ matches a.c+ only, not abcc", ["a.c+"], "*** matches *** only, not abcc"),
        ("price (1|2)$ ^", ["(1|2)$"], "price *** ^"),
        # When one secret contains another, the longer one is replaced as a whole.
        ("key=abcdefgh", ["abcd", "abcdefgh"], "key=***"),
        ("key=abcdefgh", ["abcdefgh", "abcd"], "key=***"),
        ("key=abcdxxxx", ["abcd", "abcdefgh"], "key=***xxxx"),
        ("ABCD1234 is not abcd1234", ["abcd1234"], "ABCD1234 is not ***"),
        ("çığlık şifre1234 öğün", ["şifre1234"], "çığlık *** öğün"),
        ("line1\nabcd1234\nline3", ["abcd1234"], "line1\n***\nline3"),
        ("duplicates", ["dupl", "dupl"], "***icates"),
    ],
)
def test_redact(text: str, secrets: list[str], expected: str) -> None:
    assert redact(text, secrets) == expected


def test_redact_accepts_any_sequence_of_strings() -> None:
    assert redact(f"a {PIN} b", (PIN,)) == "a *** b"
    assert redact(f"a {PIN} b", [PIN]) == "a *** b"


def test_redacted_marker_and_minimum_length() -> None:
    assert REDACTED == "***"
    assert MIN_SECRET_LENGTH == 4
    assert redact("abc abcd", ["abc"]) == "abc abcd"
    assert redact("abc abcd", ["abcd"]) == "abc ***"


def test_single_string_instead_of_a_sequence_is_an_error() -> None:
    # Iterating a string yields one-character "secrets": nothing would be redacted.
    with pytest.raises(TypeError):
        redact("text", BOT_TOKEN)
    root_handlers = list(logging.getLogger().handlers)
    with pytest.raises(TypeError):
        configure_logging(secrets=BOT_TOKEN, stream=io.StringIO())
    assert logging.getLogger().handlers == root_handlers


# ------------------------------------------------------------------------ line format


def test_structlog_event_is_one_json_object_per_line() -> None:
    stream = start()

    structlog.get_logger("propbot.signals").info("signal_queued", signal_id=7, stale=False)
    structlog.get_logger("propbot.notify").warning("outbox_retry", attempts=2, wait_s=4.0)

    raw = stream.getvalue()
    assert raw.endswith("\n")
    assert len(raw.splitlines()) == 2
    first, second = lines(stream)
    assert list(first) == ["timestamp", "level", "logger", "event", "signal_id", "stale"]
    assert first["level"] == "info"
    assert first["logger"] == "propbot.signals"
    assert first["event"] == "signal_queued"
    assert first["signal_id"] == 7
    assert first["stale"] is False
    assert second["level"] == "warning"
    assert second["logger"] == "propbot.notify"
    assert second["wait_s"] == 4.0


def test_timestamp_is_utc_iso_8601() -> None:
    stream = start()
    before = dt.datetime.now(dt.UTC)

    structlog.get_logger("propbot.test").info("tick")
    logging.getLogger("telegram").warning("tock")

    after = dt.datetime.now(dt.UTC)
    for entry in lines(stream):
        # Millisecond precision: no run of digits is long enough to coincide with a PIN.
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", entry["timestamp"])
        stamp = dt.datetime.fromisoformat(entry["timestamp"])
        assert stamp.utcoffset() == dt.timedelta(0)
        assert before - dt.timedelta(milliseconds=1) <= stamp <= after


def test_stdlib_record_goes_through_the_same_chain() -> None:
    stream = start()

    logging.getLogger("uvicorn.error").info("Started server process [%d]", 4242)

    entry = only_line(stream)
    assert list(entry) == ["timestamp", "level", "logger", "event"]
    assert entry["level"] == "info"
    assert entry["logger"] == "uvicorn.error"
    assert entry["event"] == "Started server process [4242]"


def test_values_with_newlines_and_turkish_text_stay_on_one_readable_line() -> None:
    stream = start()

    structlog.get_logger("propbot.test").info("notice", text="günlük bütçe\n$500/$1000 ığüşöç")

    raw = stream.getvalue()
    assert raw.count("\n") == 1
    assert "günlük bütçe" in raw  # not \u-escaped
    assert only_line(stream)["text"] == "günlük bütçe\n$500/$1000 ığüşöç"


def test_values_that_are_not_json_are_rendered_as_text() -> None:
    stream = start()

    structlog.get_logger("propbot.test").info(
        "odd_values",
        nan=float("nan"),
        inf=float("-inf"),
        path=PurePosixPath("/srv/propbot/data"),
        raw=b"\x00\xff",
        when=dt.date(2026, 11, 13),
        items={3},
        frozen=frozenset({"a"}),
        pair=(1, "two"),
        keyed={1: "one", None: "none"},
        nothing=None,
        ratio=0.25,
    )

    entry = only_line(stream)
    assert entry["nan"] == "nan"
    assert entry["inf"] == "-inf"
    assert entry["path"] == "PurePosixPath('/srv/propbot/data')"
    assert entry["raw"] == "b'\\x00\\xff'"
    assert entry["when"] == "datetime.date(2026, 11, 13)"
    assert entry["items"] == [3]
    assert entry["frozen"] == ["a"]
    assert entry["pair"] == [1, "two"]
    assert entry["keyed"] == {"1": "one", "None": "none"}
    assert entry["nothing"] is None
    assert entry["ratio"] == 0.25


def test_cyclic_and_very_deep_values_do_not_break_the_line() -> None:
    stream = start()
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    deep: Any = BOT_TOKEN
    for _ in range(50):
        deep = {"next": [deep]}

    structlog.get_logger("propbot.test").info("shapes", cyclic=cyclic, deep=deep)

    assert only_line(stream)["event"] == "shapes"
    assert BOT_TOKEN not in stream.getvalue()


# ---------------------------------------------------------------------------- redaction


def _raise_with_secret() -> None:
    raise RuntimeError(f"request to bot{BOT_TOKEN} failed")


def _struct_value() -> None:
    structlog.get_logger("propbot.test").info("token_loaded", token=BOT_TOKEN)


def _struct_embedded() -> None:
    structlog.get_logger("propbot.test").info(
        "request_failed", url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )


def _struct_event_text() -> None:
    structlog.get_logger("propbot.test").warning(f"pin {PIN} rejected")


def _struct_nested_dict() -> None:
    structlog.get_logger("propbot.test").info(
        "payload", body={"v": 1, "auth": {"token": WEBHOOK_TOKEN}}
    )


def _struct_nested_list_and_tuple() -> None:
    structlog.get_logger("propbot.test").info(
        "args", items=["ok", (PIN, [f"x{WEBHOOK_TOKEN}y"])], tags={BOT_TOKEN}
    )


def _struct_dict_key() -> None:
    structlog.get_logger("propbot.test").info("lookup", table={WEBHOOK_TOKEN: "known"})


def _struct_keyword_name() -> None:
    structlog.get_logger("propbot.test").info("lookup", **{WEBHOOK_TOKEN: "known"})


def _struct_bound_context() -> None:
    structlog.get_logger("propbot.test").bind(pin=PIN).info("bound")


def _struct_contextvars() -> None:
    structlog.contextvars.bind_contextvars(token=WEBHOOK_TOKEN)
    structlog.get_logger("propbot.test").info("with_context")


def _struct_positional_args() -> None:
    structlog.get_logger("propbot.test").info("webhook token %s refused", WEBHOOK_TOKEN)


def _struct_exception() -> None:
    try:
        _raise_with_secret()
    except RuntimeError:
        structlog.get_logger("propbot.test").exception("send_failed")


def _struct_exc_info_instance() -> None:
    structlog.get_logger("propbot.test").error("send_failed", exc_info=ValueError(PIN))


def _struct_chained_cause() -> None:
    try:
        try:
            raise KeyError(WEBHOOK_TOKEN)
        except KeyError as inner:
            raise RuntimeError("lookup failed") from inner
    except RuntimeError:
        structlog.get_logger("propbot.test").exception("lookup_failed")


def _struct_chained_context() -> None:
    try:
        try:
            raise KeyError(WEBHOOK_TOKEN)
        except KeyError:
            # Deliberately without "from": the KeyError travels as the implicit __context__.
            raise RuntimeError("cleanup failed")  # noqa: B904
    except RuntimeError:
        structlog.get_logger("propbot.test").exception("handler_failed")


def _struct_exception_note() -> None:
    try:
        error = RuntimeError("send failed")
        error.add_note(f"url was https://api.telegram.org/bot{BOT_TOKEN}/getMe")
        raise error
    except RuntimeError:
        structlog.get_logger("propbot.test").exception("send_failed")


def _struct_exception_group() -> None:
    try:
        raise ExceptionGroup("tasks failed", [ValueError(PIN), RuntimeError(BOT_TOKEN)])
    except ExceptionGroup:
        structlog.get_logger("propbot.test").exception("tasks_failed")


def _struct_exception_object_as_value() -> None:
    structlog.get_logger("propbot.test").error("send_failed", error=RuntimeError(BOT_TOKEN))


def _struct_opaque_object() -> None:
    structlog.get_logger("propbot.test").info("state", holder=Opaque(), items=[Opaque()])


def _struct_bytes() -> None:
    structlog.get_logger("propbot.test").info("body", raw=f'{{"token":"{WEBHOOK_TOKEN}"}}'.encode())


def _struct_url_encoded() -> None:
    structlog.get_logger("propbot.test").info(
        "request", url=f"https://example.invalid/?t={quote(BOT_TOKEN, safe='')}"
    )


def _struct_early_logger() -> None:
    EARLY_LOG.info("early_logger", token=BOT_TOKEN)


def _struct_stack_info() -> None:
    structlog.get_logger("propbot.test").info(f"dump {PIN}", stack_info=True)


def _foreign_message() -> None:
    logging.getLogger("telegram").warning(f"Invalid token {BOT_TOKEN}")


def _foreign_positional_args() -> None:
    logging.getLogger("telegram.ext.Updater").warning(
        "Error while calling %s for bot%s (attempt %d)", "getUpdates", BOT_TOKEN, 3
    )


def _foreign_mapping_args() -> None:
    logging.getLogger("telegram").warning("pin %(pin)s refused", {"pin": PIN})


def _foreign_message_object() -> None:
    logging.getLogger("telegram").error(RuntimeError(f"bad pin {PIN}"))


def _foreign_exception() -> None:
    try:
        _raise_with_secret()
    except RuntimeError:
        logging.getLogger("telegram.ext.Application").exception("No error handlers registered")


def _foreign_exception_with_args() -> None:
    try:
        _raise_with_secret()
    except RuntimeError as error:
        logging.getLogger("telegram").exception("update %s caused %s", f"/kill {PIN}", error)


def _foreign_exc_info_tuple() -> None:
    try:
        raise ValueError(WEBHOOK_TOKEN)
    except ValueError:
        logging.getLogger("asyncio").error("Task exception was never retrieved", exc_info=True)


def _foreign_chained_exception() -> None:
    try:
        try:
            raise KeyError(WEBHOOK_TOKEN)
        except KeyError as inner:
            raise RuntimeError("network error") from inner
    except RuntimeError:
        logging.getLogger("telegram").exception("polling failed")


def _foreign_stack_info() -> None:
    logging.getLogger("telegram").warning("slow call with %s", BOT_TOKEN, stack_info=True)


def _foreign_dict_message() -> None:
    logging.getLogger("telegram").warning({"token": BOT_TOKEN})


def _foreign_root_logger() -> None:
    logging.warning("root says %s", PIN)


def _foreign_url_encoded() -> None:
    logging.getLogger("telegram").warning("GET /bot%s/getMe", quote(BOT_TOKEN, safe=""))


def _foreign_warning_level_http_client() -> None:
    logging.getLogger("httpx").warning("retrying https://api.telegram.org/bot%s/getMe", BOT_TOKEN)


EMITTERS: list[Callable[[], None]] = [
    _struct_value,
    _struct_embedded,
    _struct_event_text,
    _struct_nested_dict,
    _struct_nested_list_and_tuple,
    _struct_dict_key,
    _struct_keyword_name,
    _struct_bound_context,
    _struct_contextvars,
    _struct_positional_args,
    _struct_exception,
    _struct_exc_info_instance,
    _struct_chained_cause,
    _struct_chained_context,
    _struct_exception_note,
    _struct_exception_group,
    _struct_exception_object_as_value,
    _struct_opaque_object,
    _struct_bytes,
    _struct_url_encoded,
    _struct_early_logger,
    _struct_stack_info,
    _foreign_message,
    _foreign_positional_args,
    _foreign_mapping_args,
    _foreign_message_object,
    _foreign_exception,
    _foreign_exception_with_args,
    _foreign_exc_info_tuple,
    _foreign_chained_exception,
    _foreign_stack_info,
    _foreign_dict_message,
    _foreign_root_logger,
    _foreign_url_encoded,
    _foreign_warning_level_http_client,
]


@pytest.mark.parametrize("emit", EMITTERS, ids=lambda emit: emit.__name__.lstrip("_"))
def test_secret_never_reaches_the_stream(
    emit: Callable[[], None], capsys: pytest.CaptureFixture[str]
) -> None:
    stream = start(level="DEBUG")

    emit()

    raw = stream.getvalue()
    assert len(lines(stream)) == 1
    assert REDACTED in raw  # the secret-bearing part was logged, not dropped
    captured = capsys.readouterr()
    for secret in SECRETS:
        assert secret not in raw
        assert quote(secret, safe="") not in raw
        assert secret not in captured.out + captured.err


def test_plain_value_is_replaced_and_the_rest_is_kept() -> None:
    stream = start()

    structlog.get_logger("propbot.test").info(
        "request_failed",
        token=BOT_TOKEN,
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        nested={"auth": [WEBHOOK_TOKEN, {"pin": PIN}], "attempt": 3},
        chat_id=987654321,
    )

    entry = only_line(stream)
    assert entry["token"] == "***"
    assert entry["url"] == "https://api.telegram.org/bot***/sendMessage"
    assert entry["nested"] == {"auth": ["***", {"pin": "***"}], "attempt": 3}
    assert entry["chat_id"] == 987654321


def test_structlog_exception_text_is_redacted_but_kept() -> None:
    stream = start()

    _struct_exception()

    entry = only_line(stream)
    assert entry["level"] == "error"
    assert entry["event"] == "send_failed"
    assert entry["exception"].startswith("Traceback (most recent call last):")
    assert "_raise_with_secret" in entry["exception"]
    assert entry["exception"].endswith("RuntimeError: request to bot*** failed")
    assert "exc_info" not in entry


def test_foreign_record_args_are_merged_then_redacted() -> None:
    stream = start()

    _foreign_positional_args()

    entry = only_line(stream)
    assert entry["logger"] == "telegram.ext.Updater"
    assert entry["level"] == "warning"
    assert entry["event"] == "Error while calling getUpdates for bot*** (attempt 3)"
    assert "positional_args" not in entry


def test_foreign_exception_traceback_is_redacted_but_kept() -> None:
    stream = start()

    _foreign_exception_with_args()

    entry = only_line(stream)
    assert entry["logger"] == "telegram"
    assert entry["level"] == "error"
    assert entry["event"] == "update /kill *** caused request to bot*** failed"
    assert entry["exception"].startswith("Traceback (most recent call last):")
    assert entry["exception"].endswith("RuntimeError: request to bot*** failed")


def test_chained_exceptions_are_all_redacted() -> None:
    stream = start()

    _foreign_chained_exception()

    text = only_line(stream)["exception"]
    assert "KeyError: '***'" in text
    assert "direct cause" in text
    assert text.endswith("RuntimeError: network error")


def test_foreign_stack_info_is_kept_as_text() -> None:
    stream = start()

    _foreign_stack_info()

    entry = only_line(stream)
    assert entry["event"] == "slow call with ***"
    assert entry["stack_info"].startswith("Stack (most recent call last):")


def test_extra_attributes_of_foreign_records_are_not_rendered() -> None:
    stream = start()

    logging.getLogger("uvicorn.error").info("Started", extra={"color_message": BOT_TOKEN})

    assert list(only_line(stream)) == ["timestamp", "level", "logger", "event"]
    assert BOT_TOKEN not in stream.getvalue()


def test_record_that_cannot_be_formatted_is_dropped_without_its_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # logging's own error handler would print the raw message and arguments to stderr.
    # The record is handed to the handler directly: pytest's capture handler, which sits on
    # the root logger as well, re-raises formatting errors.
    before = list(logging.getLogger().handlers)
    stream = start()
    (handler,) = own_handlers(before)
    broken = logging.LogRecord(
        "telegram", logging.WARNING, __file__, 1, "%d updates for %s", (BOT_TOKEN,), None
    )
    with pytest.raises(TypeError):
        broken.getMessage()

    handler.handle(broken)
    logging.getLogger("telegram").warning("still alive")

    dropped, alive = lines(stream)
    assert list(dropped) == ["timestamp", "level", "logger", "event", "error"]
    assert dropped["event"] == "log_record_dropped"
    assert dropped["logger"] == "telegram"
    assert dropped["level"] == "error"
    assert dropped["error"] == "TypeError"
    assert alive["event"] == "still alive"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert BOT_TOKEN not in stream.getvalue() + captured.out


def test_timestamp_digits_are_not_redacted() -> None:
    year_prefix = f"{dt.datetime.now(dt.UTC).year}-"  # e.g. "2026-", part of every timestamp
    stream = start(secrets=(year_prefix,))

    structlog.get_logger("propbot.test").info("tick", day=f"{year_prefix}01-02")
    logging.getLogger("telegram").warning("tock %s", f"{year_prefix}01-02")

    first, second = lines(stream)
    assert first["day"] == "***01-02"
    assert second["event"] == "tock ***01-02"
    assert first["timestamp"].startswith(year_prefix)
    assert second["timestamp"].startswith(year_prefix)


def test_numbers_are_left_alone() -> None:
    # Only text is redacted: a number that happens to contain the PIN digits is not a leak.
    stream = start()

    structlog.get_logger("propbot.test").info("bar", bar_time=1700424242000, text="1700424242000")

    entry = only_line(stream)
    assert entry["bar_time"] == 1700424242000
    assert entry["text"] == "1700***000"


def test_without_secrets_everything_is_logged_unchanged() -> None:
    stream = start(secrets=())

    structlog.get_logger("propbot.test").info("plain", text=f"pin {PIN}")

    assert only_line(stream)["text"] == f"pin {PIN}"


def test_secrets_shorter_than_the_minimum_are_ignored() -> None:
    stream = start(secrets=("abc", ""))

    structlog.get_logger("propbot.test").info("alphabet", text="abcdef")

    assert only_line(stream)["text"] == "abcdef"


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def second_handler() -> Iterator[_Collector]:
    """Another handler on the root logger, like pytest's capture handler or a stray one."""
    collector = _Collector()
    root = logging.getLogger()
    root.addHandler(collector)
    yield collector
    root.removeHandler(collector)


@pytest.mark.parametrize(
    "emit",
    [emit for emit in EMITTERS if emit.__name__.startswith("_struct_")],
    ids=lambda emit: emit.__name__.lstrip("_"),
)
def test_structlog_events_are_already_redacted_for_other_root_handlers(
    emit: Callable[[], None], second_handler: _Collector
) -> None:
    start(level="DEBUG")

    emit()

    (message,) = second_handler.messages
    assert REDACTED in message
    for secret in SECRETS:
        assert secret not in message
        assert quote(secret, safe="") not in message


def test_early_logger_is_redacted_with_the_new_secrets_for_other_root_handlers(
    second_handler: _Collector,
) -> None:
    start(secrets=("first-secret",))
    EARLY_LOG.info("one", text="first-secret")
    start(secrets=("second-secret",))

    EARLY_LOG.info("two", text="second-secret")

    assert "first-secret" not in second_handler.messages[0]
    assert "second-secret" not in second_handler.messages[1]


# -------------------------------------------------------------------------------- levels


def test_default_level_is_info() -> None:
    stream = io.StringIO()
    configure_logging(secrets=SECRETS, stream=stream)

    structlog.get_logger("propbot.test").debug("hidden")
    logging.getLogger("telegram").debug("hidden too")
    structlog.get_logger("propbot.test").info("shown")
    logging.getLogger("telegram").info("shown too")

    assert [entry["event"] for entry in lines(stream)] == ["shown", "shown too"]
    assert logging.getLogger().level == logging.INFO


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("DEBUG", ["debug", "info", "warning", "error", "critical"]),
        ("INFO", ["info", "warning", "error", "critical"]),
        ("WARNING", ["warning", "error", "critical"]),
        ("ERROR", ["error", "critical"]),
        ("CRITICAL", ["critical"]),
        ("debug", ["debug", "info", "warning", "error", "critical"]),
        ("Warning", ["warning", "error", "critical"]),
    ],
)
def test_level_filters_structlog_and_stdlib_alike(level: str, expected: list[str]) -> None:
    stream = start(level=level)
    struct_log = structlog.get_logger("propbot.test")
    stdlib_log = logging.getLogger("uvicorn.error")  # telegram loggers have an INFO floor

    for name in ("debug", "info", "warning", "error", "critical"):
        getattr(struct_log, name)("struct_event")
        getattr(stdlib_log, name)("stdlib event")

    entries = lines(stream)
    assert [e["level"] for e in entries if e["event"] == "struct_event"] == expected
    assert [e["level"] for e in entries if e["event"] == "stdlib event"] == expected


@pytest.mark.parametrize("level", ["", "LOUD", "NOTSET", "TRACE", "10", "WARN ", "INFO,DEBUG"])
def test_unknown_level_is_refused_before_anything_changes(level: str) -> None:
    root = logging.getLogger()
    handlers, root_level = list(root.handlers), root.level

    with pytest.raises(ValueError, match="geçersiz log seviyesi"):
        configure_logging(secrets=SECRETS, level=level, stream=io.StringIO())

    assert root.handlers == handlers
    assert root.level == root_level


# ------------------------------------------------------------------ http client loggers


def test_quiet_loggers_are_the_http_clients() -> None:
    assert set(QUIET_LOGGERS) == {"httpx", "httpx2", "httpcore", "httpcore2"}


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
@pytest.mark.parametrize(
    "name",
    [
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "httpx._client",
        "httpcore.http11",
        "httpcore.connection",
        "httpcore2.http2",
    ],
)
def test_http_client_loggers_emit_nothing_below_warning(name: str, level: str) -> None:
    stream = start(level=level)
    logger = logging.getLogger(name)

    # The line httpx writes for every request; the URL of a Telegram call holds the token.
    logger.info(
        'HTTP Request: %s %s "%s %d %s"',
        "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        "HTTP/1.1",
        200,
        "OK",
    )
    logger.debug("connect_tcp.started host=%r port=%d", "api.telegram.org", 443)

    assert stream.getvalue() == ""
    assert logger.getEffectiveLevel() == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)


@pytest.mark.parametrize("name", QUIET_LOGGERS)
def test_http_client_warnings_still_arrive_redacted(name: str) -> None:
    stream = start(level="DEBUG")

    logging.getLogger(name).warning("retry https://api.telegram.org/bot%s/getMe", BOT_TOKEN)

    entry = only_line(stream)
    assert entry["logger"] == name
    assert entry["event"] == "retry https://api.telegram.org/bot***/getMe"


def test_http_client_loggers_stay_quiet_after_reconfiguring() -> None:
    start(level="INFO")
    stream = start(level="DEBUG")

    for name in QUIET_LOGGERS:
        logging.getLogger(name).info("HTTP Request: GET /bot%s/getMe", BOT_TOKEN)

    assert stream.getvalue() == ""


@pytest.mark.parametrize("name", INFO_FLOOR_LOGGERS)
def test_telegram_loggers_never_go_below_info(name: str) -> None:
    # python-telegram-bot logs whole updates at DEBUG; one of them may be `/kill <PIN>`.
    stream = start(level="DEBUG")

    logging.getLogger(name).debug("Processing update %s", "text='/kill 4821-not-the-real-pin'")
    logging.getLogger(f"{name}.Updater").debug("Got update: %s", "text='/devam 000000'")

    assert stream.getvalue() == ""
    assert logging.getLogger(name).getEffectiveLevel() == logging.INFO
    logging.getLogger(name).info("polling started")
    assert only_line(stream)["event"] == "polling started"


def test_telegram_loggers_follow_a_stricter_level() -> None:
    stream = start(level="ERROR")

    for name in INFO_FLOOR_LOGGERS:
        logging.getLogger(name).warning("ignored at this level")

    assert stream.getvalue() == ""
    assert {logging.getLogger(name).level for name in INFO_FLOOR_LOGGERS} == {logging.ERROR}


def test_reset_logging_also_restores_the_telegram_loggers() -> None:
    start(level="DEBUG")

    reset_logging()

    assert {logging.getLogger(name).level for name in INFO_FLOOR_LOGGERS} == {logging.NOTSET}


# ------------------------------------------------------------- idempotence and streams


def test_configuring_twice_keeps_exactly_one_handler_and_one_line_per_event() -> None:
    before = list(logging.getLogger().handlers)

    start()
    start()
    stream = start()
    structlog.get_logger("propbot.test").info("once")
    logging.getLogger("telegram").warning("once more")

    assert len(own_handlers(before)) == 1
    assert [entry["event"] for entry in lines(stream)] == ["once", "once more"]


def test_reconfiguring_switches_stream_level_and_secrets() -> None:
    first = start(level="DEBUG", secrets=("first-secret",))
    second = start(level="WARNING", secrets=("second-secret",))

    structlog.get_logger("propbot.test").info("below_the_new_level")
    structlog.get_logger("propbot.test").warning("values", a="first-secret", b="second-secret")
    logging.getLogger("telegram").warning("first-secret second-secret")

    assert first.getvalue() == ""
    values, foreign = lines(second)
    assert values["a"] == "first-secret"
    assert values["b"] == "***"
    assert foreign["event"] == "first-secret ***"


def test_logger_created_before_configuration_uses_the_current_set_up() -> None:
    first = start(secrets=("first-secret",))
    EARLY_LOG.info("one", text="first-secret")
    second = start(secrets=("second-secret",))
    EARLY_LOG.info("two", text="second-secret")
    EARLY_LOG.bind(component="receiver").info("three", text="second-secret")

    assert [(entry["logger"], entry["text"]) for entry in lines(first)] == [
        ("propbot.early", "***")
    ]
    assert [(entry["event"], entry["text"]) for entry in lines(second)] == [
        ("two", "***"),
        ("three", "***"),
    ]
    assert lines(second)[1]["component"] == "receiver"


def test_closed_stream_of_an_earlier_configuration_is_never_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = start()
    first.close()
    second = start()

    structlog.get_logger("propbot.test").info("after_close")
    logging.getLogger("telegram").warning("after close")

    assert len(lines(second)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_broken_stream_does_not_raise_into_the_caller_or_print_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = start()
    stream.close()

    structlog.get_logger("propbot.test").info("lost", token=BOT_TOKEN)
    logging.getLogger("telegram").warning("lost %s", BOT_TOKEN)

    captured = capsys.readouterr()
    assert BOT_TOKEN not in captured.out + captured.err


def test_default_stream_is_the_current_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(secrets=SECRETS)

    structlog.get_logger("propbot.test").info("to_stdout", pin=PIN)
    logging.getLogger("telegram").warning("to stdout as well")

    captured = capsys.readouterr()
    first, second = (json.loads(line) for line in captured.out.splitlines())
    assert first["event"] == "to_stdout"
    assert first["pin"] == "***"
    assert second["event"] == "to stdout as well"
    assert captured.err == ""


def test_default_stream_follows_a_replaced_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging(secrets=SECRETS)
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)

    structlog.get_logger("propbot.test").info("redirected")

    assert only_line(replacement)["event"] == "redirected"


def test_reset_logging_removes_the_handler_and_restores_defaults() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    stream = start(level="DEBUG")

    reset_logging()
    logging.getLogger("telegram").warning("after reset")

    assert root.handlers == before
    assert root.level == logging.WARNING
    assert all(logging.getLogger(name).level == logging.NOTSET for name in QUIET_LOGGERS)
    assert not structlog.is_configured()
    assert stream.getvalue() == ""


def test_reset_logging_is_safe_without_a_previous_configuration() -> None:
    before = list(logging.getLogger().handlers)

    reset_logging()
    reset_logging()

    assert logging.getLogger().handlers == before
