"""Versioned HTTP routes for review intake and readback."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response

from patchscope import __version__
from patchscope.api.dependencies import get_review_service
from patchscope.domain import Finding, ReviewDetail, ReviewPage, ReviewStatus
from patchscope.errors import IntakeError
from patchscope.schemas import (
    CapabilitiesResponse,
    FindingTriageRequest,
    GitHubReviewRequest,
    TextReviewRequest,
)
from patchscope.service import ReviewService

router = APIRouter()
ServiceDependency = Annotated[ReviewService, Depends(get_review_service)]


@router.get("/health", tags=["operations"])
def health(service: ServiceDependency) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "patchscope",
        "version": __version__,
        "database": "ready" if service.ready() else "unavailable",
        "source_execution": False,
    }


@router.get("/ready", tags=["operations"])
def ready(service: ServiceDependency) -> Response:
    is_ready = service.ready()
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not_ready"},
    )


@router.get(
    "/api/v1/capabilities",
    tags=["operations"],
    response_model=CapabilitiesResponse,
)
def capabilities(service: ServiceDependency) -> dict[str, object]:
    return service.capabilities()


@router.post(
    "/api/v1/reviews/text",
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
    response_model=ReviewDetail,
)
def create_text_review(payload: TextReviewRequest, service: ServiceDependency) -> ReviewDetail:
    result = service.review_text(
        filename=payload.filename,
        content=payload.content,
        title=payload.name,
    )
    return result


@router.post(
    "/api/v1/reviews/file",
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
    response_model=ReviewDetail,
)
async def create_file_review(
    service: ServiceDependency,
    file: Annotated[UploadFile, File(description="Source file or bounded ZIP archive")],
    name: Annotated[str | None, Form(max_length=160)] = None,
) -> ReviewDetail:
    filename = file.filename or "upload.txt"
    limit = service.settings.max_review_bytes
    content = await file.read(limit + 1)
    await file.close()
    if len(content) > limit:
        raise IntakeError(f"The upload exceeds the {limit}-byte review limit")
    result = service.review_upload(filename=filename, content=content, title=name)
    return result


@router.post(
    "/api/v1/reviews/github",
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
    response_model=ReviewDetail,
)
async def create_github_review(
    payload: GitHubReviewRequest,
    service: ServiceDependency,
) -> ReviewDetail:
    result = await service.review_github(url=payload.url, title=payload.name)
    return result


@router.get("/api/v1/reviews", tags=["reviews"], response_model=ReviewPage)
def list_reviews(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    review_status: Annotated[ReviewStatus | None, Query(alias="status")] = None,
) -> ReviewPage:
    page = service.list_reviews(limit=limit, offset=offset, status=review_status)
    return page


@router.get("/api/v1/reviews/{review_id}", tags=["reviews"], response_model=ReviewDetail)
def get_review(review_id: str, service: ServiceDependency) -> ReviewDetail:
    return service.get_review(review_id)


@router.patch(
    "/api/v1/reviews/{review_id}/findings/{fingerprint}",
    tags=["reviews"],
    response_model=Finding,
)
def update_finding(
    review_id: str,
    fingerprint: str,
    payload: FindingTriageRequest,
    service: ServiceDependency,
) -> Finding:
    result = service.update_finding(
        review_id=review_id,
        fingerprint=fingerprint,
        status=payload.status,
        note=payload.note,
    )
    return result


@router.get("/api/v1/reviews/{review_id}/exports/{export_format}", tags=["reviews"])
def export_review(review_id: str, export_format: str, service: ServiceDependency) -> Response:
    if export_format not in {"markdown", "sarif"}:
        raise IntakeError("Export format must be markdown or sarif")
    content, media_type, filename = service.export(review_id, export_format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
