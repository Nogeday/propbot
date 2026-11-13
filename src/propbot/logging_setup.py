"""Structured logging: one JSON object per line, with the secrets redacted.

CLAUDE.md invariant 6: a secret never reaches a log line. Every record, whether it comes
from a structlog logger or from a stdlib `logging` logger (uvicorn, telegram, httpx), ends
in the same processor chain, which replaces each occurrence of each secret with `***` in
the event text, in every value (nested containers and object reprs included) and in the
formatted traceback, before the line is rendered.

structlog events are redacted a first time before they become a `logging.LogRecord`, so a
second handler on the root logger never sees them raw. Records of other libraries are
redacted only by the handler installed here.
"""

import logging
import math
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Final, TextIO
from urllib.parse import quote

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from propbot.core.clock import system_clock_ms, utc_from_ms

REDACTED: Final = "***"
# Shorter "secrets" would shred ordinary text; the real ones are far longer.
MIN_SECRET_LENGTH: Final = 4
# httpx and httpcore log request URLs at INFO/DEBUG; Telegram URLs contain the bot token.
QUIET_LOGGERS: Final = ("httpx", "httpx2", "httpcore", "httpcore2")
# python-telegram-bot logs whole updates at DEBUG, and an update may be the user's
# `/kill <PIN>` message. These loggers never go below INFO, whatever level is configured.
INFO_FLOOR_LOGGERS: Final = ("telegram", "telegram.ext")

_LEVELS: Final[Mapping[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_LEADING_KEYS: Final = ("timestamp", "level", "logger", "event")
# Containers nested deeper than this are logged as their (redacted) repr.
_MAX_DEPTH: Final = 6

_render_json: Final = structlog.processors.JSONRenderer(ensure_ascii=False)


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every occurrence of every secret of at least `MIN_SECRET_LENGTH` characters."""
    pattern = _secret_pattern(secrets)
    return text if pattern is None else pattern.sub(REDACTED, text)


def configure_logging(
    *, secrets: Sequence[str], level: str = "INFO", stream: TextIO | None = None
) -> None:
    """Route structlog and stdlib logging to one redacting JSON-lines handler on the root logger.

    `stream=None` writes to whatever `sys.stdout` is when a record is emitted. Calling this
    again replaces the handler installed by the previous call (new secrets, level, stream).
    """
    level_number = _LEVELS.get(level.upper())
    if level_number is None:
        raise ValueError(f"geçersiz log seviyesi: {level!r}; izin verilenler: {', '.join(_LEVELS)}")
    redactor = _Redactor(_secret_pattern(_with_url_encoded_forms(_as_list(secrets))))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redactor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Loggers created before this call (module level) must pick up the new chain.
        cache_logger_on_first_use=False,
    )

    foreign_pre_chain: list[Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
    ]
    processors: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        # Foreign records arrive with the raw exc_info; structlog events were rendered above.
        structlog.processors.format_exc_info,
        redactor,
        # Stamped after the redaction: a timestamp must never be starred out.
        _add_timestamp,
        _leading_keys_first,
        _render_json,
    ]
    handler = _JsonLinesHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=foreign_pre_chain, processors=processors
        )
    )

    root = logging.getLogger()
    _remove_own_handlers(root)
    root.addHandler(handler)
    root.setLevel(level_number)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in INFO_FLOOR_LOGGERS:
        logging.getLogger(name).setLevel(max(level_number, logging.INFO))


def reset_logging() -> None:
    """Undo `configure_logging`: remove its handler and restore the library defaults.

    For tests: a handler left on the root logger would keep writing to a stream the test
    has already closed.
    """
    root = logging.getLogger()
    _remove_own_handlers(root)
    root.setLevel(logging.WARNING)
    for name in (*QUIET_LOGGERS, *INFO_FLOOR_LOGGERS):
        logging.getLogger(name).setLevel(logging.NOTSET)
    structlog.reset_defaults()


def _as_list(secrets: Sequence[str]) -> list[str]:
    if isinstance(secrets, str):
        # A lone string is a sequence of one-character "secrets": nothing would be redacted.
        raise TypeError("secrets must be a sequence of strings, not a single string")
    return list(secrets)


def _secret_pattern(secrets: Sequence[str]) -> re.Pattern[str] | None:
    usable = {secret for secret in _as_list(secrets) if len(secret) >= MIN_SECRET_LENGTH}
    if not usable:
        return None
    # Longest first: when one secret contains another, the longer match must win.
    ordered = sorted(usable, key=lambda secret: (-len(secret), secret))
    return re.compile("|".join(re.escape(secret) for secret in ordered))


def _with_url_encoded_forms(secrets: Sequence[str]) -> list[str]:
    """Each secret plus its percent-encoded spelling (a bot token has a colon; URLs get logged)."""
    forms: list[str] = []
    for secret in secrets:
        forms.append(secret)
        encoded = quote(secret, safe="")
        if encoded != secret:
            forms.append(encoded)
    return forms


class _Redactor:
    """Processor: redact every string of the event dict and make every value JSON-native.

    Values that are neither text, numbers nor containers (exceptions, paths, bytes, models)
    are replaced by their repr here, so the renderer never has to stringify an object after
    the redaction has run.
    """

    def __init__(self, pattern: re.Pattern[str] | None) -> None:
        self._pattern = pattern

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        return {self._text(str(key)): self._clean(value, 0) for key, value in event_dict.items()}

    def _text(self, text: str) -> str:
        return text if self._pattern is None else self._pattern.sub(REDACTED, text)

    def _clean(self, value: object, depth: int) -> object:
        if isinstance(value, str):
            return self._text(str(value))
        if value is None or isinstance(value, bool | int):
            return value
        if isinstance(value, float):
            # NaN and infinity are not JSON.
            return value if math.isfinite(value) else repr(value)
        if depth < _MAX_DEPTH:
            if isinstance(value, Mapping):
                return {
                    self._text(str(key)): self._clean(item, depth + 1)
                    for key, item in value.items()
                }
            if isinstance(value, list | tuple | set | frozenset):
                return [self._clean(item, depth + 1) for item in value]
        return self._text(repr(value))


def _add_timestamp(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """UTC ISO 8601 with milliseconds: no run of digits long enough to look like a PIN."""
    now = utc_from_ms(system_clock_ms())
    event_dict["timestamp"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return event_dict


def _leading_keys_first(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    ordered = {key: event_dict[key] for key in _LEADING_KEYS if key in event_dict}
    ordered.update(event_dict)
    return ordered


class _JsonLinesHandler(logging.Handler):
    """Writes each formatted record as one line and flushes."""

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__()
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._write(self.format(record))
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        # The stock implementation prints the record's raw message and arguments to stderr,
        # past the redaction. Say only where the record came from and what went wrong.
        error = sys.exc_info()[1]
        event: EventDict = {
            "level": "error",
            "logger": record.name,
            "event": "log_record_dropped",
            "error": type(error).__name__,
        }
        event = _leading_keys_first(None, "error", _add_timestamp(None, "error", event))
        line = str(_render_json(None, "error", event))
        try:
            self._write(line)
        except Exception:
            # The stream itself is broken. Losing the line is better than raising into the
            # caller or printing the record.
            return

    def _write(self, line: str) -> None:
        stream = sys.stdout if self._stream is None else self._stream
        stream.write(line + "\n")
        stream.flush()


def _remove_own_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if isinstance(handler, _JsonLinesHandler):
            root.removeHandler(handler)
            handler.close()
