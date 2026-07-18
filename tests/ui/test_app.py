"""Streamlit AppTest coverage for the PatchScope workbench."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from streamlit.testing.v1 import AppTest

from patchscope.ui.client import ApiError
from patchscope.ui.views import _validate_text_source

from .conftest import FakeUiClient, find_button, visible_text


@pytest.mark.ui
def test_workbench_shell_leads_with_intake_and_persisted_review_inbox(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    app = run_app(fake_client)

    assert not app.exception
    assert [title.value for title in app.title] == ["PatchScope"]
    workspace = app.segmented_control(key="patchscope_workspace_navigation")
    assert workspace.options == ["New review", "Reviews"]
    assert workspace.value == "new"
    tab_labels = [tab.label for tab in app.tabs]
    assert {"Paste code", "Upload file", "GitHub PR"} <= set(tab_labels)
    assert "Review code, inspect the evidence" in visible_text(app)
    assert "Run review" in [button.label for button in app.button]
    assert "Service: Ready" not in visible_text(app)
    assert "API health" in visible_text(app)

    app = workspace.set_value("reviews").run()

    assert "Open" in [button.label for button in app.button]
    assert "Run review" not in [button.label for button in app.button]
    assert "Jul 18, 2026 · 12:00 UTC" in visible_text(app)
    assert not app.chat_input
    assert not app.chat_message


@pytest.mark.ui
def test_run_review_replaces_intake_with_evidence_backed_detail(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    app = run_app(fake_client)
    next(widget for widget in app.text_input if widget.key == "paste_review_name").set_value(
        "Checkout review"
    )
    next(widget for widget in app.text_input if widget.key == "paste_filename").set_value(
        "checkout.py"
    )
    next(area for area in app.text_area if area.key == "paste_source").set_value(
        "def checkout(value):\n    return eval(value)\n"
    )
    app = find_button(app, "Run review").click().run()

    assert fake_client.text_calls == [
        {
            "filename": "checkout.py",
            "content": "def checkout(value):\n    return eval(value)\n",
            "name": "Checkout review",
        }
    ]
    text = visible_text(app)
    assert "Review completed" in text
    assert "Unsafe dynamic execution" in text
    assert "return eval(user_input)" in text
    assert "+++ b/checkout.py" in text
    assert "Source check" not in text
    assert "rev_demo" not in text
    assert app.session_state["patchscope_workspace_page"] == "detail"
    assert "Run review" not in [button.label for button in app.button]
    assert {"Back to reviews", "Review another"} <= {button.label for button in app.button}
    assert "Review execution details" in [expander.label for expander in app.expander]

    app = find_button(app, "Review another").click().run()

    assert app.session_state["patchscope_workspace_page"] == "new"
    assert "Run review" in [button.label for button in app.button]
    assert "Unsafe dynamic execution" not in visible_text(app)


@pytest.mark.ui
def test_example_loader_populates_a_credential_free_review(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    app = find_button(run_app(fake_client), "Load example review").click().run()

    review_name = next(widget for widget in app.text_input if widget.key == "paste_review_name")
    filename = next(widget for widget in app.text_input if widget.key == "paste_filename")
    source = next(widget for widget in app.text_area if widget.key == "paste_source")

    assert review_name.value == "Insecure checkout example"
    assert filename.value == "insecure_checkout.py"
    assert "eval(expression)" in source.value
    assert "httpx.get" in source.value


@pytest.mark.ui
def test_finding_triage_persists_through_api_readback(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    app = run_app(fake_client)
    app = app.segmented_control(key="patchscope_workspace_navigation").set_value("reviews").run()
    app = find_button(app, "Open").click().run()

    assert "Search reviews" not in visible_text(app)
    assert "Unsafe dynamic execution" in visible_text(app)

    app = find_button(app, "Acknowledge").click().run()

    assert fake_client.triage_calls == [
        {
            "review_id": "rev_demo",
            "fingerprint": "finding-security",
            "status": "acknowledged",
            "note": None,
        }
    ]
    assert "Finding acknowledged" in visible_text(app)
    assert "Reopen" in [button.label for button in app.button]
    assert app.session_state["detail_status_filter"] == ["acknowledged", "open"]

    app = find_button(app, "Back to reviews").click().run()

    assert any(widget.key == "inbox_query" for widget in app.text_input)
    assert "Open" in [button.label for button in app.button]
    assert "Unsafe dynamic execution" not in visible_text(app)


@pytest.mark.ui
def test_ignore_records_the_canonical_status_and_optional_note(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    app = run_app(fake_client)
    app = app.segmented_control(key="patchscope_workspace_navigation").set_value("reviews").run()
    app = find_button(app, "Open").click().run()

    acknowledge = find_button(app, "Acknowledge")
    assert "No source code is changed" in acknowledge.help
    note = next(widget for widget in app.text_input if widget.key.startswith("detail_ignore_note_"))
    note.set_value("Generated fixture intentionally demonstrates this pattern.")
    app = find_button(app, "Confirm ignore").click().run()

    assert fake_client.triage_calls == [
        {
            "review_id": "rev_demo",
            "fingerprint": "finding-security",
            "status": "ignored",
            "note": "Generated fixture intentionally demonstrates this pattern.",
        }
    ]
    assert "Finding ignored" in visible_text(app)
    assert "Reopen" in [button.label for button in app.button]


@pytest.mark.ui
def test_inbox_api_failure_is_actionable_without_breaking_intake(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    fake_client.list_error = ApiError(
        "The PatchScope service is unavailable. Start the API, then try again.",
        code="service_unavailable",
        retryable=True,
    )
    app = run_app(fake_client)
    app = app.segmented_control(key="patchscope_workspace_navigation").set_value("reviews").run()

    assert not app.exception
    assert "service is unavailable" in visible_text(app)
    app = app.segmented_control(key="patchscope_workspace_navigation").set_value("new").run()
    assert "Run review" in [button.label for button in app.button]


@pytest.mark.ui
def test_unavailable_review_service_blocks_submission_with_one_recovery_command(
    fake_client: FakeUiClient,
    run_app: Callable[[FakeUiClient], AppTest],
) -> None:
    fake_client.health_error = ApiError(
        "The PatchScope service is unavailable.",
        code="service_unavailable",
        retryable=True,
    )

    app = run_app(fake_client)

    assert "The review service isn't running" in visible_text(app)
    run_buttons = [button for button in app.button if button.label == "Run review"]
    assert len(run_buttons) == 3
    assert all(button.disabled for button in run_buttons)
    assert "patchscope serve" in visible_text(app)
    assert "prefix the command with `uv run`" in visible_text(app)


def test_paste_validation_uses_the_shared_language_registry() -> None:
    assert _validate_text_source("Dockerfile", "FROM python:3.12\n") is None
    assert _validate_text_source("change.patch", "@@ -1 +1 @@\n-old\n+new\n") is None
    assert _validate_text_source("payload.bin", "binary-ish") is not None
