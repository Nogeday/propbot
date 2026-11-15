"""Exceptions nobody catches go through the redacting logger, never raw to stderr (invariant 6)."""

import asyncio
import gc
import io
import json
import sys
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from propbot.logging_setup import configure_logging, reset_logging
from propbot.signals import service
from propbot.signals.service import install_exception_logging

# A made-up value in the real format.
BOT_TOKEN = "7000000001:AAF-fake_bot_key_0123456789abcdefghi"
HOOKS = ((sys, "excepthook"), (threading, "excepthook"), (sys, "unraisablehook"))


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> Iterator[io.StringIO]:
    """Logging into a buffer with the hooks installed; the process-wide hooks come back after."""
    for owner, name in HOOKS:
        monkeypatch.setattr(owner, name, getattr(owner, name))
    out = io.StringIO()
    configure_logging(secrets=[BOT_TOKEN], stream=out)
    install_exception_logging()
    yield out
    reset_logging()


def events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def assert_nothing_raw(stream: io.StringIO, capsys: pytest.CaptureFixture[str]) -> None:
    captured = capsys.readouterr()
    assert captured.err == ""  # the interpreter's own hooks would have written here
    assert BOT_TOKEN not in stream.getvalue() + captured.out


def unraisable(kind: type[BaseException] | None, error: BaseException | None) -> Any:
    """What the interpreter hands to `sys.unraisablehook` (its type cannot be built here)."""
    return SimpleNamespace(
        exc_type=kind, exc_value=error, exc_traceback=None, err_msg=None, object=None
    )


def test_all_three_interpreter_hooks_are_replaced(stream: io.StringIO) -> None:
    assert sys.excepthook is not sys.__excepthook__
    assert threading.excepthook is not threading.__excepthook__
    assert sys.unraisablehook is not sys.__unraisablehook__


def test_an_uncaught_exception_of_the_main_thread_is_logged_redacted(
    stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        raise RuntimeError(f"The token `{BOT_TOKEN}` was rejected by the server.")
    except RuntimeError as error:
        sys.excepthook(type(error), error, error.__traceback__)

    (event,) = events(stream)
    assert (event["level"], event["event"]) == ("critical", "uncaught_exception")
    assert event["error_type"] == "RuntimeError"
    assert event["exception"].endswith("RuntimeError: The token `***` was rejected by the server.")
    assert_nothing_raw(stream, capsys)


def test_ctrl_c_is_not_logged(stream: io.StringIO) -> None:
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

    assert stream.getvalue() == ""


def test_an_uncaught_exception_of_another_thread_is_logged_redacted(
    stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise RuntimeError(f"thread saw {BOT_TOKEN}")

    thread = threading.Thread(target=boom, name="worker-7")
    thread.start()
    thread.join()

    (event,) = events(stream)
    assert (event["level"], event["event"]) == ("critical", "uncaught_thread_exception")
    assert (event["thread"], event["error_type"]) == ("worker-7", "RuntimeError")
    assert event["exception"].endswith("RuntimeError: thread saw ***")
    assert_nothing_raw(stream, capsys)


def test_a_thread_that_exits_is_not_logged(stream: io.StringIO) -> None:
    thread = threading.Thread(target=sys.exit)
    thread.start()
    thread.join()

    assert stream.getvalue() == ""


def test_an_exception_nobody_can_catch_is_logged_redacted(
    stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    # Raised in a finalizer: the interpreter "ignores" it and hands it to sys.unraisablehook,
    # whose default prints the exception text to stderr.
    class Finalized:
        def __del__(self) -> None:
            raise RuntimeError(f"finalizer saw {BOT_TOKEN}")

    Finalized()
    gc.collect()

    (event,) = events(stream)
    assert (event["level"], event["event"]) == ("error", "unraisable_exception")
    assert event["error_type"] == "RuntimeError"
    assert event["exception"].endswith("RuntimeError: finalizer saw ***")
    assert_nothing_raw(stream, capsys)


def test_the_unraisable_hook_tolerates_missing_details_and_a_failing_logger(
    stream: io.StringIO, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.unraisablehook(unraisable(None, None))
    (event,) = events(stream)
    assert (event["event"], event["error_type"]) == ("unraisable_exception", None)
    assert "exception" not in event

    class Broken:
        def error(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("log stream is gone")

    # E.g. while the interpreter shuts down. An error leaving the hook would be printed,
    # together with the hook itself, by the interpreter's default hook.
    monkeypatch.setattr(service, "_log", Broken())
    error = RuntimeError(f"saw {BOT_TOKEN}")
    sys.unraisablehook(unraisable(RuntimeError, error))

    assert len(events(stream)) == 1
    assert_nothing_raw(stream, capsys)


def test_event_loop_errors_are_logged_redacted(
    stream: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.set_exception_handler(service._log_loop_error)
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": RuntimeError(f"task saw {BOT_TOKEN}"),
            }
        )
        loop.call_exception_handler({"message": f"callback failed for bot{BOT_TOKEN}"})
    finally:
        loop.close()

    with_error, without_error = events(stream)
    assert (with_error["level"], with_error["event"]) == ("error", "event_loop_error")
    assert with_error["detail"] == "Task exception was never retrieved"
    assert with_error["error_type"] == "RuntimeError"
    assert with_error["exception"].endswith("RuntimeError: task saw ***")
    assert without_error["detail"] == "callback failed for bot***"
    assert without_error["error_type"] is None
    assert "exception" not in without_error
    assert_nothing_raw(stream, capsys)
