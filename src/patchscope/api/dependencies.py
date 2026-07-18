"""Small FastAPI dependency boundaries."""

from __future__ import annotations

from fastapi import Request

from patchscope.service import ReviewService


def get_review_service(request: Request) -> ReviewService:
    service = getattr(request.app.state, "review_service", None)
    if not isinstance(service, ReviewService):
        raise RuntimeError("PatchScope review service is not initialized")
    return service
