"""FastAPI application of the local panel (ADR-0009). Everything it needs is injected.

Routes (GET never changes anything; every change is a guarded form POST):

    GET  /                  live screen: account, signals, derived trades, control forms
    GET  /parca/canli       the live section alone (the page script swaps it in)
    GET  /api/live          the same view model as JSON
    GET  /analiz            analysis screen (report artefacts under reports/)
    GET  /assets/app.css    stylesheet;  GET /assets/app.js  refresh script
    POST /eylem/<action>    duraklat · devam · kill · kill-kaldir · ozet · bakiye

`SecurityMiddleware` checks the Host header of every request and adds the protective
headers to every response; `guarded_form` checks Origin/Referer, the form encoding, the
body size and the CSRF token of every POST before an action sees it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from propbot.core.clock import Clock
from propbot.dashboard import pages
from propbot.dashboard.actions import (
    ActionDeps,
    ActionKind,
    Actions,
    CommandsFactory,
    control_state,
)
from propbot.dashboard.analysis import render_analysis
from propbot.dashboard.assets import APP_CSS, APP_JS, CSS_PATH, JS_PATH
from propbot.dashboard.live import LiveView, StoreSource, load_view, view_json
from propbot.dashboard.security import Refusal, SecurityMiddleware, guarded_form
from propbot.risk.config import RiskConfig
from propbot.risk.venue import VenueProfile

_HTML = "text/html; charset=utf-8"
_NOT_FOUND_TEXT = "Bu adreste bir sayfa yok."
_METHOD_TEXT = (
    "Bu adres bu istek yöntemini kabul etmez (durum değiştiren her işlem form POST'udur)."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardDeps:
    source: StoreSource
    risk: RiskConfig
    venue: VenueProfile
    venue_name: str
    commands: CommandsFactory | None
    unavailable_reason: str | None
    """Why the controls are off for the whole run (secrets / tv_alerts.yaml); None if on."""
    clock: Clock
    reports_dir: Path
    port: int
    csrf_token: str = field(repr=False)


def create_app(deps: DashboardDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    actions = Actions(
        ActionDeps(
            source=deps.source,
            commands=deps.commands if deps.unavailable_reason is None else None,
            unavailable_reason=deps.unavailable_reason or "",
            venue=deps.venue,
            clock=deps.clock,
        )
    )

    def view() -> LiveView:
        return load_view(
            deps.source,
            venue=deps.venue,
            venue_name=deps.venue_name,
            risk=deps.risk,
            now_ms=deps.clock(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> Response:
        text = {404: _NOT_FOUND_TEXT, 405: _METHOD_TEXT}.get(error.status_code, "İstek reddedildi.")
        return PlainTextResponse(text, status_code=error.status_code, headers=error.headers)

    @app.get(pages.LIVE_PATH)
    def live_page() -> HTMLResponse:
        current = view()
        controls = control_state(
            unavailable_reason=deps.unavailable_reason,
            db=current.db,
            pin_locked_until_ms=current.pin_locked_until_ms,
        )
        return HTMLResponse(pages.live_page(current, controls, deps.csrf_token), media_type=_HTML)

    @app.get(pages.FRAGMENT_PATH)
    def live_fragment() -> HTMLResponse:
        return HTMLResponse(pages.live_fragment(view()), media_type=_HTML)

    @app.get("/api/live")
    def live_json() -> JSONResponse:
        return JSONResponse(view_json(view()))

    @app.get(pages.ANALYSIS_PATH)
    def analysis_page() -> HTMLResponse:
        return HTMLResponse(
            pages.analysis_page(render_analysis(deps.reports_dir)), media_type=_HTML
        )

    @app.get(CSS_PATH)
    def css() -> Response:
        return Response(APP_CSS, media_type="text/css; charset=utf-8")

    @app.get(JS_PATH)
    def js() -> Response:
        return Response(APP_JS, media_type="text/javascript; charset=utf-8")

    async def act(request: Request, kind: ActionKind) -> Response:
        form = await guarded_form(request, port=deps.port, csrf_token=deps.csrf_token)
        if isinstance(form, Refusal):
            return PlainTextResponse(form.text, status_code=form.status)
        result = await run_in_threadpool(actions.run, kind, form)
        return HTMLResponse(pages.result_page(result), status_code=result.status, media_type=_HTML)

    def endpoint_for(kind: ActionKind) -> Callable[[Request], Awaitable[Response]]:
        # A closure, not a default argument: FastAPI would read `kind=...` as a query
        # parameter, and `?kind=kill` must not turn one action into another.
        async def endpoint(request: Request) -> Response:
            return await act(request, kind)

        return endpoint

    for kind in ActionKind:
        app.add_api_route(
            pages.ACTION_PREFIX + kind.value,
            endpoint_for(kind),
            methods=["POST"],
            name=f"action_{kind.name.lower()}",
            include_in_schema=False,
        )

    app.add_middleware(SecurityMiddleware, port=deps.port)
    return app
