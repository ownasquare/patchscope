from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
def test_review_refactor_triage_and_inbox_readback(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 1000})

    page.goto(base_url, wait_until="domcontentloaded")

    expect(page).to_have_title(re.compile(r"PatchScope"))
    expect(page.get_by_text("Review code, inspect the evidence")).to_be_visible()

    run_marker = uuid.uuid4().hex
    review_name = f"E2E review {run_marker[:8]}"
    paste_panel = page.get_by_role("tabpanel", name="Paste code")
    paste_panel.get_by_text("More options", exact=True).click()
    paste_panel.get_by_role("textbox", name="Review name (optional)").fill(review_name)
    paste_panel.get_by_role("textbox", name="Filename").fill("shared_default.py")
    paste_panel.get_by_role("textbox", name="Source code").fill(
        "def collect(items=[]):\n"
        '    items.append("reviewed")\n'
        "    return items\n"
        f"# E2E run {run_marker}\n"
    )
    paste_panel.get_by_role("button", name="Run review").click()

    expect(page.get_by_text("Review completed. Findings are ready to triage.")).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_role("button", name="Back to reviews")).to_be_visible()
    expect(page.get_by_role("button", name="Review another")).to_be_visible()
    expect(
        page.get_by_role("heading", name="A mutable default argument is shared between calls")
    ).to_be_visible()
    expect(page.locator("code").filter(has_text="+++ b/shared_default.py")).to_be_visible()

    page.get_by_role("button", name="Acknowledge").click()
    expect(page.get_by_text("Finding acknowledged.")).to_be_visible()
    expect(page.get_by_role("button", name="Reopen")).to_be_visible()

    page.get_by_role("button", name="Back to reviews").click()

    search = page.get_by_role("textbox", name="Search reviews")
    search.fill(review_name)
    search.press("Enter")
    page.wait_for_timeout(750)
    expect(page.get_by_text(review_name, exact=True)).to_be_visible()
    page.get_by_text("Open", exact=True).filter(visible=True).click()

    expect(page.get_by_role("heading", name=review_name).filter(visible=True)).to_be_visible()
    expect(page.get_by_role("button", name="Reopen").filter(visible=True)).to_be_visible()


@pytest.mark.e2e
def test_workbench_reflows_to_mobile_without_horizontal_page_overflow(
    page: Page,
    base_url: str,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})

    page.goto(base_url, wait_until="domcontentloaded")

    expect(page).to_have_title(re.compile(r"PatchScope"))
    expect(page.get_by_role("heading", name="New review")).to_be_visible()
    has_overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
    )
    assert has_overflow is False
