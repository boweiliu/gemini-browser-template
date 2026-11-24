"""The app in a real browser: the address bar, following a link, and the page on a phone-sized touch screen."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect, sync_playwright
from werkzeug.serving import make_server

from gemini_browser import runner
from gemini_browser.fake_gemini_server import Certificate, FakeGeminiServer

pytestmark = pytest.mark.browser

FORTRESS = "/opt/fortress/tilion-fortress/tilion"
LONG_PREFORMATTED = "x" * 200
PAGE = (
    "# Capsule home\n"
    "A paragraph that is long enough to wrap on a narrow phone screen, with a number of words in it.\n"
    "=> /second A link with a fairly long label that has to wrap on a small screen\n"
    "* a list item\n"
    "> a quotation\n"
    f"```art\n{LONG_PREFORMATTED}\n```\n"
)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(executable_path=FORTRESS)
        yield launched
        launched.close()


@pytest.fixture
def served(gemini: FakeGeminiServer, data_dir: Path) -> Iterator[str]:
    """The app served on a loopback port, with a capsule behind it; yields the app's base address."""
    gemini.respond("/", 20, "text/gemini", PAGE.encode())
    gemini.respond(
        "/second", 20, "text/gemini", b"# Second page\nYou followed the link.\n"
    )
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def type_address_and_go(page: Page, address: str, *, tap: bool) -> None:
    box = page.locator("input[name=url]")
    if tap:
        box.tap()
        page.keyboard.type(address)
        page.locator("button.go").tap()
    else:
        box.fill(address)
        box.press("Enter")


def test_on_a_desktop_the_address_bar_opens_a_page_and_its_link_and_the_source_panel_work(
    browser: Browser, served: str, gemini: FakeGeminiServer
) -> None:
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    page = context.new_page()
    page.goto(served + "/")
    expect(page.get_by_text("Nothing loaded yet")).to_be_visible()

    type_address_and_go(page, gemini.url("/"), tap=False)
    expect(page.locator("main h1")).to_have_text("Capsule home")
    expect(page.locator("main blockquote")).to_have_text("a quotation")
    expect(page.locator("footer .grow")).to_contain_text(
        "first visit, certificate saved"
    )

    page.locator("details.source summary").click()
    expect(page.locator("details.source pre")).to_contain_text("# Capsule home")

    page.locator("main a.link").click()
    expect(page.locator("main h1")).to_have_text("Second page")
    expect(page.locator("input[name=url]")).to_have_value(gemini.url("/second"))
    page.locator("#back").click()
    expect(page.locator("main h1")).to_have_text("Capsule home")
    context.close()


def test_the_controls_keep_their_desktop_size_with_a_mouse(
    browser: Browser, served: str
) -> None:
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    page = context.new_page()
    page.goto(served + "/")
    assert page.locator("#back").bounding_box()["width"] == 32  # ty: ignore[not-subscriptable]
    assert (
        page.evaluate(
            "getComputedStyle(document.querySelector('input[name=url]')).fontSize"
        )
        == "15px"
    )
    context.close()


@pytest.mark.parametrize(
    "size", [(393, 852), (852, 393)], ids=["portrait", "landscape"]
)
def test_on_a_phone_a_page_fits_the_screen_and_every_control_can_be_tapped(
    browser: Browser, served: str, gemini: FakeGeminiServer, size: tuple[int, int]
) -> None:
    width, height = size
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.goto(served + "/")
    type_address_and_go(page, gemini.url("/"), tap=True)
    expect(page.locator("main h1")).to_have_text("Capsule home")

    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert (
        page.evaluate("document.querySelector('meta[name=viewport]').content")
        == "width=device-width, initial-scale=1"
    )
    for selector in ("#back", "#reload", "button.go", "input[name=url]", "main a.link"):
        box = page.locator(selector).bounding_box()
        assert box is not None
        assert box["height"] >= 44, selector
    for selector in ("#reload", "#back"):
        assert page.locator(selector).bounding_box()["width"] >= 44  # ty: ignore[not-subscriptable]
    assert (
        page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('input[name=url]')).fontSize)"
        )
        >= 16
    )
    # The long preformatted line scrolls inside its own box instead of widening the page.
    assert page.evaluate(
        "document.querySelector('main pre').scrollWidth"
    ) > page.evaluate("document.querySelector('main pre').clientWidth")

    page.locator("details.source summary").tap()
    expect(page.locator("details.source pre")).to_contain_text("# Capsule home")
    assert page.evaluate("document.documentElement.scrollWidth") <= width

    page.locator("main a.link").tap()
    expect(page.locator("main h1")).to_have_text("Second page")
    context.close()


def test_on_a_phone_the_answer_to_a_prompt_can_be_typed_and_sent(
    browser: Browser, served: str, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/ask", 10, "What is your name?")
    gemini.respond("/ask", 20, "text/gemini", b"# Hello Ada\n", query="Ada")
    context = browser.new_context(
        viewport={"width": 393, "height": 852}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.goto(served + "/?url=" + gemini.url("/ask"))
    answer = page.locator(".prompt input[name=input]")
    expect(answer).to_be_visible()
    assert (
        page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('.prompt input[name=input]')).fontSize)"
        )
        >= 16
    )
    assert page.locator(".prompt button").bounding_box()["height"] >= 44  # ty: ignore[not-subscriptable]
    answer.tap()
    page.keyboard.type("Ada")
    page.locator(".prompt button").tap()
    expect(page.locator("main h1")).to_have_text("Hello Ada")
    assert page.evaluate("document.documentElement.scrollWidth") <= 393
    context.close()


def test_on_a_phone_a_changed_certificate_can_be_trusted_with_a_tap(
    browser: Browser,
    served: str,
    gemini: FakeGeminiServer,
    certificate_spare: Certificate,
) -> None:
    context = browser.new_context(
        viewport={"width": 393, "height": 852}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.goto(served + "/?url=" + gemini.url("/second"))
    expect(page.locator("main h1")).to_have_text("Second page")

    gemini.present_certificate(certificate_spare)
    page.goto(served + "/?url=" + gemini.url("/second"))
    trust = page.get_by_role("button", name="Trust the new certificate and continue")
    expect(trust).to_be_visible()
    assert trust.bounding_box()["height"] >= 44  # ty: ignore[not-subscriptable]
    assert page.evaluate("document.documentElement.scrollWidth") <= 393
    trust.tap()
    expect(page.locator("main h1")).to_have_text("Second page")
    assert page.url == served + "/?url=" + gemini.url("/second").replace(
        ":", "%3A"
    ).replace("/", "%2F")
    context.close()
