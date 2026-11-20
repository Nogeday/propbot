"""Browser-side attack defences of the local panel (ADR-0009 item 2).

The panel has no login: it listens on loopback only, and what is left to defend against is
the user's own browser being used by another site. So:

- every request must carry a `Host` header naming this panel exactly (DNS rebinding: a
  hostile name that resolves to 127.0.0.1 still sends its own name);
- every state-changing request is a form `POST` whose `Origin` (when the browser sends one)
  is this panel and whose `Referer` (when there is no `Origin`) is not another origin, and
  which carries the per-process CSRF token, compared in constant time;
- every response carries a strict CSP and the other protective headers, errors and
  assets included (the middleware adds them to whatever the app sends).

The checks are plain functions so they are tested without a server; `SecurityMiddleware`
and `guarded_form` are the FastAPI/ASGI wiring.
"""

import hmac
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlsplit

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Final[Mapping[str, str]] = {
    "content-security-policy": CSP,
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    # Not "no-referrer": under that policy a browser sends `Origin: null` with the panel's
    # own form posts, and every action would be refused as cross-site. "same-origin" keeps
    # the real Origin for the panel's own requests and still sends nothing to other sites.
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
}
# The largest panel form (balance snapshot) is a few hundred bytes with its token.
MAX_FORM_BYTES: Final = 2048
MAX_FORM_FIELDS: Final = 12
CSRF_FIELD: Final = "csrf"
FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"
_LOOPBACK_NAMES: Final = ("127.0.0.1", "localhost", "[::1]")
_TEXT_TYPE: Final = b"text/plain; charset=utf-8"

_log = structlog.get_logger(__name__)

HOST_REFUSED_TEXT: Final = (
    "İstek reddedildi: Host başlığı bu panele ait değil. Panel yalnızca "
    "http://127.0.0.1:<port>, http://localhost:<port> ya da http://[::1]:<port> adresiyle "
    "açılır (uzaktan bakmak için SSH tüneli)."
)
ORIGIN_REFUSED_TEXT: Final = (
    "İstek reddedildi: form başka bir siteden gönderilmiş görünüyor (Origin/Referer bu panel "
    "değil). Paneli adres çubuğundan aç ve formu oradan gönder."
)
CSRF_REFUSED_TEXT: Final = (
    "İstek reddedildi: form belirteci eksik ya da geçersiz (panel yeniden başlatılmış "
    "olabilir). Sayfayı yenileyip yeniden dene."
)
TOO_LARGE_TEXT: Final = "İstek reddedildi: form gövdesi çok büyük."
CONTENT_TYPE_TEXT: Final = (
    "İstek reddedildi: yalnızca application/x-www-form-urlencoded biçiminde form kabul edilir."
)
INTERNAL_ERROR_TEXT: Final = (
    "Panelde beklenmeyen bir hata oldu; ayrıntı günlükte (dashboard_request_failed)."
)
BAD_FORM_TEXT: Final = "İstek reddedildi: form okunamadı (bozuk ya da yinelenen alan)."


def new_csrf_token() -> str:
    """A fresh token for this process: a restart invalidates every page served before it."""
    return secrets.token_urlsafe(32)


def allowed_hosts(port: int) -> frozenset[str]:
    """The `Host` header values this panel answers to (the listening port included)."""
    return frozenset(f"{name}:{port}" for name in _LOOPBACK_NAMES)


def host_allowed(host: str | None, port: int) -> bool:
    """Exact match only: no default port, no trailing dot, no other name for loopback."""
    return host is not None and host in allowed_hosts(port)


def origin_allowed(origin: str | None, referer: str | None, port: int) -> bool:
    """Same-origin check of a state-changing request.

    `Origin` decides when present (modern browsers send it on every POST); "null" (a
    sandboxed frame, a file:// page) is foreign. Without it a `Referer` must name this
    panel; with neither, the CSRF token alone decides. The panel's own pages are served
    with Referrer-Policy "same-origin", so its forms arrive with their real Origin.
    """
    allowed = {f"http://{host}" for host in allowed_hosts(port)}
    if origin is not None:
        return origin in allowed
    if referer is None:
        return True
    try:
        parts = urlsplit(referer)
    except ValueError:
        return False
    return f"{parts.scheme}://{parts.netloc}" in allowed


def csrf_matches(expected: str, given: str | None) -> bool:
    if given is None or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def parse_form(body: bytes) -> dict[str, str] | None:
    """Decode an urlencoded body; None when it is not UTF-8, malformed or repeats a field.

    A repeated field is refused rather than resolved: which copy wins would be a choice
    an attacker could steer.
    """
    try:
        text = body.decode("utf-8")
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=bool(text),
            max_num_fields=MAX_FORM_FIELDS,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        return None
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            return None
        form[name] = value
    return form


@dataclass(frozen=True, slots=True)
class Refusal:
    """A request the panel will not act on: HTTP status and the Turkish text to show."""

    status: int
    text: str


async def _read_limited(request: Request, max_bytes: int) -> bytes | None:
    declared = request.headers.get("content-length")
    declared_ok = declared is not None and declared.isascii() and declared.isdigit()
    if declared_ok and int(str(declared)) > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    stream: AsyncIterator[bytes] = request.stream()
    async for chunk in stream:
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def guarded_form(request: Request, *, port: int, csrf_token: str) -> dict[str, str] | Refusal:
    """Every check of a state-changing POST, in order; the form fields when all pass."""
    if not origin_allowed(request.headers.get("origin"), request.headers.get("referer"), port):
        return Refusal(403, ORIGIN_REFUSED_TEXT)
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != FORM_CONTENT_TYPE:
        return Refusal(415, CONTENT_TYPE_TEXT)
    body = await _read_limited(request, MAX_FORM_BYTES)
    if body is None:
        return Refusal(413, TOO_LARGE_TEXT)
    form = parse_form(body)
    if form is None:
        return Refusal(400, BAD_FORM_TEXT)
    if not csrf_matches(csrf_token, form.pop(CSRF_FIELD, None)):
        return Refusal(403, CSRF_REFUSED_TEXT)
    return form


def _with_security_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    names = {name.encode("latin-1") for name in SECURITY_HEADERS}
    kept = [(name, value) for name, value in headers if name.lower() not in names]
    added = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in SECURITY_HEADERS.items()
    ]
    return kept + added


class SecurityMiddleware:
    """Pure ASGI middleware: Host check on every request, protective headers on every answer.

    Pure ASGI rather than Starlette's BaseHTTPMiddleware so nothing buffers or re-reads
    the request body, and so the headers also reach the framework's own error answers.
    """

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self._app = app
        self._port = port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            # The panel has no websocket route; refuse before any host check could be skipped.
            await send({"type": "websocket.close", "code": 1008})
            return

        started = False

        async def send_secured(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": _with_security_headers(message["headers"])}
            await send(message)

        hosts = [value for name, value in scope["headers"] if name == b"host"]
        host = hosts[0].decode("latin-1") if len(hosts) == 1 else None
        if not host_allowed(host, self._port):
            # 400 when the header is missing or repeated, 421 when it names another server.
            await _send_text(send_secured, 421 if host is not None else 400, HOST_REFUSED_TEXT)
            return
        try:
            await self._app(scope, receive, send_secured)
        except Exception as error:
            # Answered here rather than by Starlette's outermost error middleware, whose
            # answer would bypass the headers above. Only the class name is logged: an
            # exception text is not known to be free of what the request carried.
            _log.error("dashboard_request_failed", error_type=type(error).__name__)
            if not started:
                await _send_text(send_secured, 500, INTERNAL_ERROR_TEXT)


async def _send_text(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", _TEXT_TYPE),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
