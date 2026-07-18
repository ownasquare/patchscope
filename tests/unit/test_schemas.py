from __future__ import annotations

import pytest
from pydantic import ValidationError

from patchscope.schemas import GitHubReviewRequest, TextReviewRequest


def test_text_request_preserves_source_content_exactly() -> None:
    content = "\n    value = 1  \n"

    request = TextReviewRequest(
        filename="  example.py  ",
        content=content,
        name="  Whitespace review  ",
    )

    assert request.filename == "example.py"
    assert request.content == content
    assert request.name == "Whitespace review"


def test_text_request_rejects_blank_source_without_mutating_code() -> None:
    with pytest.raises(ValidationError, match="content cannot be blank"):
        TextReviewRequest(filename="example.py", content="  \n\t")


def test_github_request_normalizes_only_semantic_fields() -> None:
    request = GitHubReviewRequest(
        url="  https://github.com/acme/repo/pull/7  ",
        name="  Checkout PR  ",
    )

    assert request.url == "https://github.com/acme/repo/pull/7"
    assert request.name == "Checkout PR"
