"""Command-surface tests for the Camoufox sidecar (Unit 4 of the plan).

Scenarios exercised against a real Camoufox browser via the sidecar stdio
protocol. These tests rely on the ``camoufox`` package being installed and
the browser binary fetched (``python -m camoufox fetch``); they skip
otherwise so the suite stays informative on lighter environments.

Fixture page lives at ``cli/tests/fixtures/form.html`` and is reused by the
Rust-side parity test in ``cli/tests/camoufox_parity.rs`` — keep the two in
sync.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from conftest import Sidecar, spawn_sidecar  # noqa: E402


pytestmark = pytest.mark.asyncio


FIXTURE_URL = (
    "file://"
    + str(
        pathlib.Path(__file__).resolve().parents[3] / "cli/tests/fixtures/form.html"
    )
)


async def _launch_and_goto(sc: Sidecar, url: str) -> None:
    await sc.expect_event("ready")
    await sc.send({"id": 1, "cmd": "launch", "args": {"headless": True}})
    response = await sc.read_frame(timeout=60.0)
    assert response["ok"] is True, response
    await sc.send({"id": 2, "cmd": "page.goto", "args": {"url": url}})
    response = await sc.read_frame(timeout=30.0)
    assert response["ok"] is True, response


async def _snapshot(sc: Sidecar, id: int, **args: Any) -> dict:
    await sc.send({"id": id, "cmd": "page.snapshot", "args": args})
    response = await sc.read_frame(timeout=30.0)
    assert response["ok"] is True, response
    return response["result"]


@pytest.fixture
async def camoufox_sidecar(requires_camoufox: None):
    sc = await spawn_sidecar()
    try:
        yield sc
    finally:
        await sc.kill()


async def test_snapshot_produces_refs_for_form_fixture(
    camoufox_sidecar: Sidecar,
) -> None:
    """#1 happy path (snapshot): fixture produces refs for every actionable element."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)
    result = await _snapshot(sc, 3)

    refs = result["refs"]
    roles = sorted(r["role"] for r in refs.values())
    # 1 heading, 3 textboxes, 1 checkbox, 1 button — parity with Chrome golden
    # (cli/tests/fixtures/form-chrome-golden.json).
    assert roles == ["button", "checkbox", "heading", "textbox", "textbox", "textbox"]

    by_role_name = {(r["role"], r["name"].strip()) for r in refs.values()}
    assert ("heading", "Contact Form") in by_role_name
    assert ("button", "Submit") in by_role_name
    assert ("textbox", "Name") in by_role_name
    assert ("textbox", "Email") in by_role_name
    assert ("textbox", "Message") in by_role_name
    # Checkbox name may include leading/trailing whitespace depending on how
    # the engine whitespace-folds the label — we match on role+non-empty name.
    assert any(role == "checkbox" and name for role, name in by_role_name)

    assert result["origin"].endswith("form.html")


async def test_click_and_fill_by_ref_update_dom(camoufox_sidecar: Sidecar) -> None:
    """#2 + #3: click/fill by ref drive the form."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)
    snap = await _snapshot(sc, 3)

    # Find the ref for the Submit button and the Email textbox.
    submit_ref = _ref_by(snap["refs"], role="button", name="Submit")
    email_ref = _ref_by(snap["refs"], role="textbox", name="Email")
    assert submit_ref and email_ref

    # Fill email via ref.
    await sc.send(
        {
            "id": 10,
            "cmd": "page.fill",
            "args": {"selector": f"@{email_ref}", "value": "test@example.com"},
        }
    )
    fill_resp = await sc.read_frame(timeout=30.0)
    assert fill_resp["ok"] is True, fill_resp

    # Click submit via ref — should update status text.
    await sc.send(
        {
            "id": 11,
            "cmd": "page.click",
            "args": {"selector": f"@{submit_ref}"},
        }
    )
    click_resp = await sc.read_frame(timeout=30.0)
    assert click_resp["ok"] is True, click_resp

    # Verify via get-text on the #status paragraph using a CSS selector.
    await sc.send(
        {"id": 12, "cmd": "page.getText", "args": {"selector": "#status"}}
    )
    text_resp = await sc.read_frame(timeout=10.0)
    assert text_resp["ok"] is True, text_resp
    assert text_resp["result"]["text"] == "Submitted"


async def test_click_by_css_selector(camoufox_sidecar: Sidecar) -> None:
    """#4 happy path (click by selector): no snapshot required."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    await sc.send({"id": 3, "cmd": "page.click", "args": {"selector": "#submit"}})
    resp = await sc.read_frame(timeout=30.0)
    assert resp["ok"] is True, resp

    await sc.send({"id": 4, "cmd": "page.getText", "args": {"selector": "#status"}})
    text_resp = await sc.read_frame(timeout=10.0)
    assert text_resp["result"]["text"] == "Submitted"


async def test_get_text_by_ref_returns_visible_text(camoufox_sidecar: Sidecar) -> None:
    """#5 happy path: get text on a ref returns the visible text."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)
    snap = await _snapshot(sc, 3)
    heading_ref = _ref_by(snap["refs"], role="heading", name="Contact Form")
    assert heading_ref

    await sc.send(
        {"id": 4, "cmd": "page.getText", "args": {"selector": f"@{heading_ref}"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True, resp
    assert resp["result"]["text"] == "Contact Form"


async def test_stale_ref_after_navigation_returns_ref_stale(
    camoufox_sidecar: Sidecar,
) -> None:
    """#6 edge case: refs from a snapshot before navigate return ref-stale."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)
    snap = await _snapshot(sc, 3)
    some_ref = next(iter(snap["refs"].keys()))

    # Navigate away to a different page (data:text/html,<html><body>blank</body></html> is a safe no-DOM target).
    await sc.send(
        {"id": 10, "cmd": "page.goto", "args": {"url": "data:text/html,<html><body>blank</body></html>"}}
    )
    nav_resp = await sc.read_frame(timeout=30.0)
    assert nav_resp["ok"] is True, nav_resp

    # The sidecar invalidates refs on goto (and on frame.navigated); let the
    # event loop drain briefly so the framenavigated callback lands.
    await asyncio.sleep(0.2)

    await sc.send(
        {"id": 11, "cmd": "page.click", "args": {"selector": f"@{some_ref}"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "ref-stale", resp


async def test_ambiguous_selector_returns_structured_error(
    camoufox_sidecar: Sidecar,
) -> None:
    """#7 edge case: selectors matching multiple elements fail with ambiguous-selector."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    # ``input`` matches all of name/email/subscribe → 3 elements on the fixture.
    await sc.send(
        {"id": 3, "cmd": "page.click", "args": {"selector": "input"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False
    assert resp["error"]["code"] == "ambiguous-selector", resp


async def test_click_at_fires_dom_sentinel(camoufox_sidecar: Sidecar) -> None:
    """Click-at drives a DOM mutation the sidecar can observe via getText."""
    sc = camoufox_sidecar
    fixture = (
        "data:text/html;charset=utf-8,"
        "<html><body style='margin:0;padding:0;'>"
        "<p id='status'>idle</p>"
        "<button id='b' style='position:absolute;left:100px;top:200px;"
        "width:80px;height:40px;' "
        "onclick=\"document.getElementById('status').textContent='clicked'\">"
        "hit</button>"
        "</body></html>"
    )
    await _launch_and_goto(sc, fixture)

    await sc.send(
        {"id": 10, "cmd": "page.click", "args": {"at": [140, 220]}}
    )
    click_resp = await sc.read_frame(timeout=30.0)
    assert click_resp["ok"] is True, click_resp
    assert click_resp["result"]["clicked"] == {"x": 140.0, "y": 220.0}

    await sc.send(
        {"id": 11, "cmd": "page.getText", "args": {"selector": "#status"}}
    )
    text_resp = await sc.read_frame(timeout=10.0)
    assert text_resp["ok"] is True, text_resp
    assert text_resp["result"]["text"] == "clicked"


async def test_click_at_rejects_both_selector_and_at(
    camoufox_sidecar: Sidecar,
) -> None:
    """Passing both selector and at is a structured invalid-args error."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    await sc.send(
        {
            "id": 10,
            "cmd": "page.click",
            "args": {"selector": "#submit", "at": [1, 2]},
        }
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "invalid-args", resp


async def test_click_at_rejects_non_numeric_coords(
    camoufox_sidecar: Sidecar,
) -> None:
    """Non-numeric coords surface invalid-args rather than crashing Playwright."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    await sc.send(
        {"id": 10, "cmd": "page.click", "args": {"at": ["x", "y"]}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "invalid-args", resp


async def test_selector_not_found_returns_structured_error(
    camoufox_sidecar: Sidecar,
) -> None:
    """#8 error path: acting on a detached/missing element surfaces a code."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    await sc.send(
        {
            "id": 3,
            "cmd": "page.click",
            "args": {"selector": "#does-not-exist-42"},
        }
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False
    # Playwright surfaces a timeout ("waiting for locator...") when no element
    # matches. The sidecar either detects zero count (selector-not-found) or
    # the underlying action times out — both are acceptable stable codes.
    assert resp["error"]["code"] in {"selector-not-found", "timeout", "action-failed"}


async def test_snapshot_click_snapshot_click_across_navigation(
    camoufox_sidecar: Sidecar,
) -> None:
    """#9 integration: snapshot → click → re-snapshot picks up fresh refs."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    # First snapshot + click submit (updates DOM in-place, no navigation).
    snap1 = await _snapshot(sc, 3)
    submit_ref_1 = _ref_by(snap1["refs"], role="button", name="Submit")
    assert submit_ref_1
    await sc.send(
        {"id": 4, "cmd": "page.click", "args": {"selector": f"@{submit_ref_1}"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True

    # Full navigation to a different page then back — refs invalidated.
    await sc.send({"id": 5, "cmd": "page.goto", "args": {"url": "data:text/html,<html><body>blank</body></html>"}})
    await sc.read_frame(timeout=30.0)
    await sc.send({"id": 6, "cmd": "page.goto", "args": {"url": FIXTURE_URL}})
    await sc.read_frame(timeout=30.0)

    # Second snapshot — refs must resolve on the reloaded page.
    snap2 = await _snapshot(sc, 7)
    submit_ref_2 = _ref_by(snap2["refs"], role="button", name="Submit")
    assert submit_ref_2
    await sc.send(
        {"id": 8, "cmd": "page.click", "args": {"selector": f"@{submit_ref_2}"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True, resp


def _ref_by(refs: dict, *, role: str, name: str) -> str | None:
    for ref_id, entry in refs.items():
        if entry["role"] == role and entry["name"].strip() == name:
            return ref_id
    return None


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


async def test_interactive_only_snapshot_then_click_by_ref(
    camoufox_sidecar: Sidecar,
) -> None:
    """Regression: ``snapshot -i`` renumbers refs to ``e1..eM`` on the agent
    side while the JS walker's ``data-__ab-ref`` attributes still hold the
    pre-filter counter. The handle-resolution loop must query the DOM by the
    original attribute and cache handles under the renumbered ref, so a
    subsequent ``click @eN`` actually hits the right element.

    A prior fix introduced a ``NameError: name 'ref_id' is not defined`` in
    this loop which broke every ``snapshot`` call. This test locks in that
    (a) interactive_only snapshot doesn't raise, and (b) the renumbered ref
    actually clicks the underlying element.
    """
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    # Interactive-only snapshot should not raise and should renumber refs.
    result = await _snapshot(sc, 3, interactive=True)
    refs = result["refs"]
    # Fixture has no heading filtered out (h1 is a heading role, non-interactive),
    # so e1..e5 should all be interactive roles.
    assert all(k.startswith("e") for k in refs.keys())
    assert not any(r["role"] == "heading" for r in refs.values()), refs

    # The submit button's renumbered ref should still click the real element.
    submit_ref = _ref_by(refs, role="button", name="Submit")
    assert submit_ref, refs
    await sc.send(
        {"id": 20, "cmd": "page.click", "args": {"selector": f"@{submit_ref}"}}
    )
    click_resp = await sc.read_frame(timeout=30.0)
    assert click_resp["ok"] is True, click_resp

    await sc.send(
        {"id": 21, "cmd": "page.getText", "args": {"selector": "#status"}}
    )
    text_resp = await sc.read_frame(timeout=10.0)
    assert text_resp["result"]["text"] == "Submitted"


# ---------------------------------------------------------------------------
# Scroll — Unit 5 extension for Chrome-parity
# ---------------------------------------------------------------------------


SCROLL_PAGE_URL = (
    "data:text/html,"
    "<html><body style='margin:0;padding:0;'>"
    "<div id='top' style='height:40px;background:#f88;'>top</div>"
    "<div style='height:3000px;background:#eee;'></div>"
    "<div id='bot' style='height:40px;background:#8f8;'>bottom</div>"
    "</body></html>"
)


async def test_scroll_window_by_y_succeeds(camoufox_sidecar: Sidecar) -> None:
    """page.scroll without a selector scrolls the window by (x, y)."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, SCROLL_PAGE_URL)

    await sc.send(
        {"id": 10, "cmd": "page.scroll", "args": {"y": 1500}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True, resp
    assert resp["result"]["scrolled"] is True


async def test_scroll_window_negative_y_scrolls_up(camoufox_sidecar: Sidecar) -> None:
    """Negative y scrolls up (after scrolling down first so there's room)."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, SCROLL_PAGE_URL)

    # Scroll down 2000, then scroll up 1000 — both should succeed.
    await sc.send({"id": 10, "cmd": "page.scroll", "args": {"y": 2000}})
    down_resp = await sc.read_frame(timeout=10.0)
    assert down_resp["ok"] is True, down_resp

    await sc.send({"id": 11, "cmd": "page.scroll", "args": {"y": -1000}})
    up_resp = await sc.read_frame(timeout=10.0)
    assert up_resp["ok"] is True, up_resp


async def test_scroll_by_css_selector_with_missing_element(
    camoufox_sidecar: Sidecar,
) -> None:
    """Selector that doesn't match surfaces ``selector-not-found``."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, SCROLL_PAGE_URL)

    await sc.send(
        {
            "id": 10,
            "cmd": "page.scroll",
            "args": {"selector": "#does-not-exist-42", "y": 100},
        }
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "selector-not-found", resp


async def test_scroll_into_view_by_css_selector(camoufox_sidecar: Sidecar) -> None:
    """page.scrollIntoView centres the matched element in the viewport."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, SCROLL_PAGE_URL)

    await sc.send(
        {"id": 10, "cmd": "page.scrollIntoView", "args": {"selector": "#bot"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True, resp
    assert resp["result"]["scrolled"] == "#bot"


async def test_scroll_into_view_by_ref(camoufox_sidecar: Sidecar) -> None:
    """page.scrollIntoView accepts a ``@eN`` ref after a snapshot."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)
    snap = await _snapshot(sc, 3)
    submit_ref = _ref_by(snap["refs"], role="button", name="Submit")
    assert submit_ref, snap

    await sc.send(
        {
            "id": 10,
            "cmd": "page.scrollIntoView",
            "args": {"selector": f"@{submit_ref}"},
        }
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is True, resp
    assert resp["result"]["scrolled"] == f"@{submit_ref}"


async def test_scroll_into_view_ambiguous_selector_errors(
    camoufox_sidecar: Sidecar,
) -> None:
    """Selector matching multiple elements surfaces ``ambiguous-selector``."""
    sc = camoufox_sidecar
    await _launch_and_goto(sc, FIXTURE_URL)

    # ``input`` matches three elements on the fixture.
    await sc.send(
        {"id": 10, "cmd": "page.scrollIntoView", "args": {"selector": "input"}}
    )
    resp = await sc.read_frame(timeout=10.0)
    assert resp["ok"] is False, resp
    assert resp["error"]["code"] == "ambiguous-selector", resp
