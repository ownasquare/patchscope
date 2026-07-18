"""FastAPI factory with explicit lifecycle and sanitized failures."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from patchscope import __version__
from patchscope.api.routes import router
from patchscope.config import Settings, get_settings
from patchscope.errors import PatchScopeError
from patchscope.service import ReviewService

LOGGER = logging.getLogger("patchscope.api")


def create_app(
    *,
    settings: Settings | None = None,
    service: ReviewService | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is None:
            from patchscope.container import build_service

            app.state.review_service = build_service(active_settings)
        else:
            app.state.review_service = service
        yield
        close = getattr(app.state.review_service, "close", None)
        if callable(close):
            close()

    app = FastAPI(
        title="PatchScope API",
        summary="Evidence-backed code review and safe refactor previews",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    if service is not None:
        app.state.review_service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        request.state.started_at = started
        response = await call_next(request)
        return _apply_response_headers(response, request_id=request_id, started=started)

    @app.exception_handler(PatchScopeError)
    async def patchscope_error(request: Request, exc: PatchScopeError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()[:10]]
        return _error_response(
            request,
            422,
            "validation_error",
            "The request did not match PatchScope's input contract.",
            {"fields": fields},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception(
            "Unhandled request failure",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return _error_response(
            request,
            500,
            "internal_error",
            "PatchScope could not complete the request.",
            {"error_type": type(exc).__name__},
        )

    app.include_router(router)
    return app


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    context: dict[str, object] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
            "detail": context or {},
        },
    )
    request_id = getattr(request.state, "request_id", "")
    started = getattr(request.state, "started_at", time.perf_counter())
    return _apply_response_headers(response, request_id=request_id, started=started)


def _apply_response_headers[ResponseT: Response](
    response: ResponseT,
    *,
    request_id: str,
    started: float,
) -> ResponseT:
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app = create_app()
