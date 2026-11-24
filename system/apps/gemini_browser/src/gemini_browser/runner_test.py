import base64
import json
import socket
from pathlib import Path
from urllib.parse import quote

import pytest
from app_schema.testing import (
    assert_api_schema_describes_app,
    assert_request_guard_refuses_unknown_provenance,
)
from flask.testing import FlaskClient
from loguru import logger
from werkzeug.test import TestResponse

from gemini_browser import gemini_client, runner
from gemini_browser.fake_gemini_server import Certificate, FakeGeminiServer
from gemini_browser.gemini_store import KNOWN_HOSTS_FILE, VISITS_FILE

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def open_page(client: FlaskClient, address: str, **extra: str) -> TestResponse:
    return client.get("/", query_string={"url": address, **extra})


def text_of(response: TestResponse) -> str:
    return response.get_data(as_text=True)


def unused_port() -> int:
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()[1]


def saved_visits(data_dir: Path) -> list[dict[str, str]]:
    path = data_dir / VISITS_FILE
    return json.loads(path.read_text()) if path.exists() else []


def test_the_app_describes_its_api_the_way_its_manifest_says(
    client: FlaskClient,
) -> None:
    assert_api_schema_describes_app(
        client, Path(__file__).resolve().parents[2] / "app.toml"
    )


def test_the_request_guard_is_ahead_of_the_routes(client: FlaskClient) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/health")


def test_the_request_guard_also_refuses_a_page_of_another_origin_that_posts_a_trust(
    client: FlaskClient, data_dir: Path
) -> None:
    response = client.post(
        "/trust",
        data={
            "host_key": "example.org:1965",
            "fingerprint": "a" * 64,
            "return_url": "gemini://example.org/",
        },
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"},
    )
    assert response.status_code == 403
    assert not (data_dir / KNOWN_HOSTS_FILE).exists()


def test_health_says_ok(client: FlaskClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


# Whose profile it is

OWNER = {"X-Imbue-Identity": json.dumps({"owner": True, "user_id": "u-owner", "email": "owner@example.org"})}


def visitor_headers(user_id: str) -> dict[str, str]:
    identity = {"owner": False, "user_id": user_id, "email": f"{user_id}@example.org"}
    return {"X-Imbue-Identity": json.dumps(identity)}


VISITOR = visitor_headers("u-visitor")


@pytest.fixture
def public_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a visitor reach the fake servers, which listen on loopback: the visitor rule has its own tests."""
    monkeypatch.setattr(gemini_client, "_require_public", lambda host, address: None)


def visitor_profiles(data_dir: Path) -> list[Path]:
    root = data_dir / runner.VISITOR_PROFILES_DIR
    return sorted(root.iterdir()) if root.exists() else []


def test_a_visitor_cannot_use_the_browser_to_reach_a_private_address_and_nothing_is_contacted(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    page = text_of(open_page_as(client, VISITOR, gemini.url("/")))
    assert "This address is not open to visitors" in page
    assert "Hello" not in page
    download = client.get("/download", query_string={"url": gemini.url("/")}, headers=VISITOR)
    assert download.status_code == 502
    assert "not open to visitors" in text_of(download)
    assert gemini.requested() == []


def open_page_as(client: FlaskClient, headers: dict[str, str], address: str, **extra: str) -> TestResponse:
    return client.get("/", query_string={"url": address, **extra}, headers=headers)


def test_a_visitor_opens_a_site_the_owner_vouches_for_and_it_is_saved_in_the_visitors_own_profile(
    client: FlaskClient,
    gemini: FakeGeminiServer,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    monkeypatch.setattr(runner, "VISITOR_HOSTS", frozenset({"127.0.0.1"}))
    page = text_of(open_page_as(client, VISITOR, gemini.url("/")))
    assert "Hello" in page and "not open to visitors" not in page
    assert not (data_dir / VISITS_FILE).exists()
    (profile,) = visitor_profiles(data_dir)
    assert (profile / VISITS_FILE).exists()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", frozenset()),
        ("  ,, ", frozenset()),
        ("Gemini.Example.org", frozenset({"gemini.example.org"})),
        (" a.example , B.example,a.example ", frozenset({"a.example", "b.example"})),
    ],
)
def test_the_vouched_hosts_are_read_from_a_comma_separated_list(text: str, expected: frozenset[str]) -> None:
    assert runner.parse_visitor_hosts(text) == expected


def test_the_owner_browses_private_addresses_with_or_without_an_account_in_the_identity_header(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    for headers in (OWNER, {}):
        response = open_page_as(client, headers, gemini.url("/"))
        assert response.status_code == 200
        assert "Hello" in text_of(response)


def test_the_owner_keeps_the_app_data_directory_and_each_visitor_gets_a_directory_of_their_own(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path, public_servers: None
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    for headers in (OWNER, VISITOR, visitor_headers("u-other")):
        open_page_as(client, headers, gemini.url("/"))
    assert (data_dir / VISITS_FILE).exists()
    assert (data_dir / KNOWN_HOSTS_FILE).exists()
    profiles = visitor_profiles(data_dir)
    assert len(profiles) == 2
    for profile in profiles:
        assert (profile / VISITS_FILE).exists()
        assert (profile / KNOWN_HOSTS_FILE).exists()
    assert not (data_dir / "u-visitor").exists()


def test_the_start_page_shows_only_the_requesters_own_visited_pages(
    client: FlaskClient, gemini: FakeGeminiServer, public_servers: None
) -> None:
    gemini.respond("/alpha", 20, "text/gemini", b"# Alpha\n")
    gemini.respond("/beta", 20, "text/gemini", b"# Beta\n")
    open_page_as(client, OWNER, gemini.url("/alpha"))
    open_page_as(client, VISITOR, gemini.url("/beta"))
    owner_page = text_of(client.get("/", headers=OWNER))
    visitor_page = text_of(client.get("/", headers=VISITOR))
    stranger_page = text_of(client.get("/", headers=visitor_headers("u-stranger")))
    assert "Alpha" in owner_page and "Beta" not in owner_page
    assert "Beta" in visitor_page and "Alpha" not in visitor_page
    assert "Places to start" in stranger_page
    assert "Alpha" not in stranger_page and "Beta" not in stranger_page


def test_each_requester_saves_certificates_apart_and_trusting_one_changes_only_their_own(
    client: FlaskClient,
    gemini: FakeGeminiServer,
    certificate_spare: Certificate,
    public_servers: None,
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    for headers in (OWNER, VISITOR):
        assert "first visit, certificate saved" in text_of(open_page_as(client, headers, gemini.url("/")))
    gemini.present_certificate(certificate_spare)
    for headers in (OWNER, VISITOR):
        assert "certificate has changed" in text_of(open_page_as(client, headers, gemini.url("/")))
    fields = {
        "host_key": f"127.0.0.1:{gemini.port}",
        "fingerprint": certificate_spare.fingerprint,
        "return_url": gemini.url("/"),
    }
    assert client.post("/trust", data=fields, headers=VISITOR).status_code == 303
    assert "Hello" in text_of(open_page_as(client, VISITOR, gemini.url("/")))
    assert "certificate has changed" in text_of(open_page_as(client, OWNER, gemini.url("/")))
    # A requester who had saved nothing simply meets the site's current certificate for the first time.
    assert "Hello" in text_of(open_page_as(client, visitor_headers("u-other"), gemini.url("/")))


def test_an_account_id_that_is_not_filename_safe_is_refused_and_creates_nothing(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path, public_servers: None
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    response = open_page_as(client, visitor_headers("../../../etc/passwd"), gemini.url("/"))
    assert response.status_code == 400
    assert visitor_profiles(data_dir) == []
    assert gemini.requested() == []


def test_a_visitor_directory_stays_inside_the_visitor_profiles(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path, public_servers: None
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hello\n")
    open_page_as(client, visitor_headers("a.b-c_d"), gemini.url("/"))
    (profile,) = visitor_profiles(data_dir)
    assert profile.resolve().parent == (data_dir / runner.VISITOR_PROFILES_DIR).resolve()


def test_a_visitor_can_still_reach_the_health_probe_and_the_api_document(
    client: FlaskClient,
) -> None:
    assert client.get("/health", headers=VISITOR).status_code == 200
    assert client.get(runner.DEFAULT_API_SCHEMA_PATH, headers=VISITOR).status_code == 200


# The start page


def test_the_start_page_offers_places_to_start_until_something_is_visited(
    client: FlaskClient,
) -> None:
    body = text_of(client.get("/"))
    assert "Places to start" in body
    for _title, address in runner.STARTING_POINTS:
        assert f'href="/?url={quote(address, safe="")}"' in body
    assert "Recently visited" not in body
    assert "Nothing loaded yet" in body


def test_the_start_page_lists_visited_pages_newest_first_instead_of_the_starting_points(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/a", 20, "text/gemini", b"# Alpha\n")
    gemini.respond("/b", 20, "text/gemini", b"# Beta\n")
    open_page(client, gemini.url("/a"))
    open_page(client, gemini.url("/b"))
    body = text_of(client.get("/"))
    assert "Recently visited" in body
    assert "Places to start" not in body
    assert body.index("Beta") < body.index("Alpha")
    assert f'href="/?url={quote(gemini.url("/a"), safe="")}"' in body


def test_the_start_page_shows_only_the_most_recent_visits(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    total = runner.RECENT_VISIT_COUNT + 3
    for number in range(total):
        gemini.respond(
            f"/p{number}", 20, "text/gemini", f"# Page number {number}\n".encode()
        )
        open_page(client, gemini.url(f"/p{number}"))
    body = text_of(client.get("/"))
    assert body.count('<span class="arrow">') == runner.RECENT_VISIT_COUNT
    assert f"Page number {total - 1}<" in body
    assert "Page number 0<" not in body


def test_a_visited_page_title_is_escaped_on_the_start_page(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/x", 20, "text/gemini", b"# <script>alert(1)</script>\n")
    open_page(client, gemini.url("/x"))
    body = text_of(client.get("/"))
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# Pages


def test_a_gemtext_page_is_rendered_with_its_title_source_and_certificate_note(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond(
        "/",
        20,
        "text/gemini; charset=utf-8",
        b"# Welcome\nHello <b>there</b>\n=> /next Next page\n",
    )
    response = open_page(client, gemini.url("/"))
    body = text_of(response)
    assert response.status_code == 200
    assert "<title>Welcome - Gemini browser</title>" in body
    assert "<h1>Welcome</h1>" in body
    assert "<p>Hello &lt;b&gt;there&lt;/b&gt;</p>" in body
    assert f'href="/?url={quote(gemini.url("/next"), safe="")}"' in body
    assert (
        '<details class="source"><summary>View source</summary><pre># Welcome\nHello &lt;b&gt;there&lt;/b&gt;\n=&gt; /next Next page\n</pre></details>'
        in body
    )
    assert "20 text/gemini" in body
    assert "first visit, certificate saved" in body
    assert f'value="{gemini.url("/")}"' in body
    assert "Open in another Gemini client" in body


def test_a_second_visit_says_the_certificate_has_been_trusted_since_the_first(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Hi\n")
    open_page(client, gemini.url("/"))
    body = text_of(open_page(client, gemini.url("/")))
    assert "certificate trusted since" in body
    assert "first visit" not in body


def test_an_address_typed_without_the_scheme_still_opens(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/", 20, "text/gemini", b"# Bare\n")
    body = text_of(open_page(client, gemini.url("/").removeprefix("gemini://")))
    assert "<h1>Bare</h1>" in body


def test_a_page_with_no_meta_is_read_as_gemtext(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond_raw(gemini.url("/"), b"20\r\n# Plain header\n")
    assert "<h1>Plain header</h1>" in text_of(open_page(client, gemini.url("/")))


def test_a_page_without_a_heading_is_titled_by_its_address(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/no-heading", 20, "text/gemini", b"just text\n")
    open_page(client, gemini.url("/no-heading"))
    assert saved_visits(runner.DATA_DIR)[0]["title"] == gemini.url("/no-heading")


def test_a_plain_text_page_is_shown_as_escaped_preformatted_text(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/t.txt", 20, "text/plain", b"# not a heading\n<i>x</i>\n")
    body = text_of(open_page(client, gemini.url("/t.txt")))
    assert '<pre class="plain"># not a heading\n&lt;i&gt;x&lt;/i&gt;\n</pre>' in body
    assert "<h1>" not in body


def test_an_html_page_is_shown_as_text_and_never_run(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/h.html", 20, "text/html", b"<script>alert(1)</script>")
    body = text_of(open_page(client, gemini.url("/h.html")))
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_the_page_charset_decides_how_bytes_become_text(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond(
        "/latin", 20, "text/plain; charset=ISO-8859-1", "café".encode("latin-1")
    )
    assert "café" in text_of(open_page(client, gemini.url("/latin")))


def test_an_unknown_charset_falls_back_to_utf8(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/odd", 20, 'text/plain; charset="no-such-charset"', "café".encode())
    assert "café" in text_of(open_page(client, gemini.url("/odd")))


def test_bytes_that_are_not_valid_text_are_replaced_not_fatal(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/bad", 20, "text/plain", b"ok \xff\xfe end")
    response = open_page(client, gemini.url("/bad"))
    assert response.status_code == 200
    assert "ok �� end" in text_of(response)


def test_a_page_cut_short_by_the_size_limit_says_so(
    client: FlaskClient, gemini: FakeGeminiServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_client, "MAX_BODY_BYTES", 500)
    gemini.respond("/big", 20, "text/plain", b"x" * 3000)
    body = text_of(open_page(client, gemini.url("/big")))
    assert "longer than 8 MB; the rest is not shown" in body
    assert body.count("x" * 500) == 2  # the page and its source panel
    assert "x" * 501 not in body


def test_a_page_within_the_limit_has_no_cut_note(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/small", 20, "text/plain", b"small")
    assert "longer than 8 MB" not in text_of(open_page(client, gemini.url("/small")))


def test_an_image_is_shown_inline_as_a_data_address_and_has_no_source_panel(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/pic.png", 20, "image/png", PNG)
    body = text_of(open_page(client, gemini.url("/pic.png")))
    assert f'src="data:image/png;base64,{base64.b64encode(PNG).decode()}"' in body
    assert 'class="picture"' in body
    assert "View source" not in body
    assert "20 image/png" in body


def test_an_svg_is_shown_through_an_image_tag_so_its_scripts_cannot_run(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond(
        "/pic.svg",
        20,
        "image/svg+xml",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
    )
    body = text_of(open_page(client, gemini.url("/pic.svg")))
    assert "<svg" not in body
    assert 'src="data:image/svg+xml;base64,' in body


def test_a_file_that_is_not_a_page_offers_a_download_instead_of_showing_it(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/files/archive.zip", 20, "application/zip", b"PK" + b"\x00" * 3000)
    body = text_of(open_page(client, gemini.url("/files/archive.zip")))
    assert "This is a file, not a page" in body
    assert "application/zip, about 2 KB" in body
    assert (
        f'href="/download?url={quote(gemini.url("/files/archive.zip"), safe="")}"'
        in body
    )
    assert "View source" not in body


def test_a_tiny_file_is_described_as_at_least_one_kilobyte(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/tiny.bin", 20, "application/octet-stream", b"abc")
    assert "about 1 KB" in text_of(open_page(client, gemini.url("/tiny.bin")))


def test_the_download_sends_the_file_as_an_attachment(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    payload = bytes(range(256)) * 4
    gemini.respond("/files/archive.zip", 20, "application/zip", payload)
    response = client.get(
        "/download", query_string={"url": gemini.url("/files/archive.zip")}
    )
    assert response.status_code == 200
    assert response.data == payload
    assert (
        response.headers["Content-Disposition"] == 'attachment; filename="archive.zip"'
    )
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_the_download_never_shows_a_page_even_when_the_file_is_html(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/page.html", 20, "text/html", b"<script>alert(1)</script>")
    response = client.get("/download", query_string={"url": gemini.url("/page.html")})
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.headers["Content-Disposition"].startswith("attachment")


def test_the_download_filename_cannot_inject_header_text(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/a%22b%0D%0AX-Evil:%201.txt", 20, "text/plain", b"x")
    response = client.get(
        "/download", query_string={"url": gemini.url("/a%22b%0D%0AX-Evil:%201.txt")}
    )
    disposition = response.headers["Content-Disposition"]
    assert disposition == 'attachment; filename="a_22b_0D_0AX-Evil__201.txt"'
    assert "X-Evil" not in response.headers


def test_a_download_from_the_site_root_is_named_download(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/", 20, "application/zip", b"x")
    response = client.get("/download", query_string={"url": gemini.url("/")})
    assert response.headers["Content-Disposition"] == 'attachment; filename="download"'


def test_a_download_the_server_refuses_is_a_404_with_the_servers_words(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/locked", 51, "Not found")
    response = client.get("/download", query_string={"url": gemini.url("/locked")})
    assert response.status_code == 404
    assert "51 Not found" in text_of(response)


def test_a_download_that_cannot_connect_is_a_502_that_explains(
    client: FlaskClient,
) -> None:
    response = client.get(
        "/download", query_string={"url": f"gemini://127.0.0.1:{unused_port()}/f"}
    )
    assert response.status_code == 502
    assert "The server refused the connection" in text_of(response)


def test_a_download_of_a_bad_address_is_a_502(client: FlaskClient) -> None:
    for address in ("", "https://example.org/f"):
        assert client.get("/download", query_string={"url": address}).status_code == 502


# Prompts


def test_an_input_prompt_asks_the_question_and_sends_the_answer_as_part_of_the_address(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/ask", 10, "What is <your> name?")
    gemini.respond(
        "/ask",
        20,
        "text/gemini",
        b"# Hello Ada\n",
        query="Ada%20Lovelace%20%26%20%C3%A9",
    )
    prompt = text_of(open_page(client, gemini.url("/ask")))
    assert "What is &lt;your&gt; name?" in prompt
    assert '<form action="/" method="get">' in prompt
    assert f'<input type="hidden" name="url" value="{gemini.url("/ask")}">' in prompt
    assert 'type="text"' in prompt
    assert "10 input requested" in prompt

    answered = text_of(open_page(client, gemini.url("/ask"), input="Ada Lovelace & é"))
    assert "<h1>Hello Ada</h1>" in answered
    assert gemini.requested()[-1] == gemini.url("/ask", "Ada%20Lovelace%20%26%20%C3%A9")


def test_a_prompt_with_no_question_still_has_something_to_read(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond_raw(gemini.url("/ask"), b"10\r\n")
    assert "The site asked for input." in text_of(open_page(client, gemini.url("/ask")))


def test_a_prompt_page_for_an_address_that_already_has_a_query_asks_again_at_the_plain_address(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/ask", 10, "Again?", query="first")
    body = text_of(open_page(client, gemini.url("/ask", "first")))
    assert f'name="url" value="{gemini.url("/ask")}"' in body
    assert "first" not in body.split('name="url"', 1)[1].split("</form>", 1)[0]


def test_a_secret_prompt_uses_a_password_field_and_posts_so_the_answer_stays_out_of_the_address(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/login", 11, "Password?")
    body = text_of(open_page(client, gemini.url("/login")))
    assert '<form action="/" method="post">' in body
    assert 'name="input" type="password"' in body
    assert "11 input requested" in body


def test_a_posted_secret_reaches_the_server_and_is_never_recorded_in_history(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond(
        "/login", 20, "text/gemini", b"# Welcome back\n", query="s3cret%20pw"
    )
    response = client.post(
        "/", data={"url": gemini.url("/login"), "input": "s3cret pw"}
    )
    assert "<h1>Welcome back</h1>" in text_of(response)
    assert gemini.requested() == [gemini.url("/login", "s3cret%20pw")]
    assert saved_visits(runner.DATA_DIR) == []


def test_a_posted_answer_that_prompts_again_shows_the_plain_address_not_the_secret(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/login", 11, "Wrong. Password?", query="guess")
    body = text_of(
        client.post("/", data={"url": gemini.url("/login"), "input": "guess"})
    )
    assert "Wrong. Password?" in body
    assert "guess" not in body.replace("Wrong. Password?", "")


def test_a_posted_secret_stays_out_of_the_error_page_when_the_server_cannot_be_reached(
    client: FlaskClient,
) -> None:
    address = f"gemini://127.0.0.1:{unused_port()}/login"
    body = text_of(client.post("/", data={"url": address, "input": "s3cret"}))
    assert "The server refused the connection" in body
    assert "s3cret" not in body


def test_a_posted_secret_stays_out_of_the_certificate_changed_page_and_its_trust_form(
    client: FlaskClient, gemini: FakeGeminiServer, certificate_spare: Certificate
) -> None:
    gemini.respond("/login", 11, "Password?")
    open_page(client, gemini.url("/login"))
    gemini.present_certificate(certificate_spare)
    body = text_of(
        client.post("/", data={"url": gemini.url("/login"), "input": "s3cret"})
    )
    assert "This site&#x27;s certificate has changed" in body
    assert "s3cret" not in body
    assert f'name="return_url" value="{gemini.url("/login")}"' in body
    assert gemini.requested() == [gemini.url("/login")]


def test_a_posted_answer_that_is_too_long_is_refused(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    body = text_of(
        client.post("/", data={"url": gemini.url("/login"), "input": "x" * 2000})
    )
    assert "That address is too long" in body
    assert "x" * 100 not in body
    assert gemini.requested() == []


def test_an_answer_that_makes_the_address_too_long_is_refused_without_contacting_the_server(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/ask", 10, "Q?")
    body = text_of(open_page(client, gemini.url("/ask"), input="y" * 2000))
    assert "That address is too long" in body
    assert "Gemini addresses can be at most 1024 bytes." in body
    assert gemini.requested() == []


# Errors and odd answers


@pytest.mark.parametrize("address", ["", "   "])
def test_an_empty_address_shows_the_start_page(
    client: FlaskClient, address: str
) -> None:
    assert "Type a gemini:// address above" in text_of(
        client.get("/", query_string={"url": address})
    )


def test_a_web_address_is_explained_not_fetched(client: FlaskClient) -> None:
    body = text_of(open_page(client, "https://example.org/"))
    assert "This browser opens gemini:// addresses only" in body
    assert "Could not load the page" in body


def test_an_address_that_cannot_be_reached_says_why_and_offers_to_try_again(
    client: FlaskClient,
) -> None:
    port = unused_port()
    address = f"gemini://127.0.0.1:{port}/deep/page"
    body = text_of(open_page(client, address))
    assert "The server refused the connection" in body
    assert f'href="/?url={quote(address, safe="")}">Try again</a>' in body
    assert (
        f'href="/?url={quote(f"gemini://127.0.0.1/", safe="")}">Go to 127.0.0.1</a>'
        in body
    )
    assert "Not loaded" in body


def test_a_failure_at_a_sites_home_page_offers_no_go_to_home_link(
    client: FlaskClient,
) -> None:
    body = text_of(open_page(client, f"gemini://127.0.0.1:{unused_port()}/"))
    assert "Try again" in body
    assert "Go to" not in body


def test_failed_loads_are_not_added_to_history(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path
) -> None:
    gemini.respond("/gone", 52, "Gone")
    open_page(client, gemini.url("/gone"))
    open_page(client, f"gemini://127.0.0.1:{unused_port()}/")
    open_page(client, "https://example.org/")
    assert saved_visits(data_dir) == []


FAILURE_HEADINGS = [
    (40, "The server had a temporary problem"),
    (41, "The server is unavailable"),
    (42, "The server's program failed"),
    (43, "A gateway failed"),
    (44, "The server asks you to slow down"),
    (50, "The server cannot show this page"),
    (51, "This page was not found"),
    (52, "This page is gone"),
    (53, "The server will not fetch other sites"),
    (59, "The server did not understand the request"),
    (60, "This page needs a client certificate"),
    (61, "Your certificate is not accepted here"),
    (62, "Your certificate is not valid"),
    (45, "The server had a temporary problem"),
    (55, "The server cannot show this page"),
    (63, "This page needs a client certificate"),
]


@pytest.mark.parametrize(("status", "heading"), FAILURE_HEADINGS)
def test_each_failure_status_gets_its_own_plain_language_page(
    client: FlaskClient, gemini: FakeGeminiServer, status: int, heading: str
) -> None:
    gemini.respond("/x", status, "meta text")
    body = text_of(open_page(client, gemini.url("/x")))
    assert f"<h2>{heading.replace(chr(39), '&#x27;')}</h2>" in body
    assert f"{status} meta text" in body
    assert "Try again" in body


def test_the_servers_own_words_are_added_for_server_side_failures_but_not_for_certificate_ones(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/five", 51, "Nothing at <here>")
    gemini.respond("/six", 60, "Certificate please")
    assert "The server said: Nothing at &lt;here&gt;" in text_of(
        open_page(client, gemini.url("/five"))
    )
    assert "The server said" not in text_of(open_page(client, gemini.url("/six")))


def test_a_slow_down_answer_that_gives_seconds_says_how_long_to_wait(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/x", 44, "30")
    assert "wait 30 seconds before the next request" in text_of(
        open_page(client, gemini.url("/x"))
    )


def test_a_failure_with_no_message_uses_only_the_standard_advice(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond_raw(gemini.url("/x"), b"51\r\n")
    body = text_of(open_page(client, gemini.url("/x")))
    assert "Check the address, or go up to the site&#x27;s home page." in body
    assert "The server said" not in body


@pytest.mark.parametrize("status", [12, 70, 99])
def test_a_status_the_browser_does_not_know_gets_a_neutral_page(
    client: FlaskClient, gemini: FakeGeminiServer, status: int
) -> None:
    gemini.respond("/x", status, "odd")
    body = text_of(open_page(client, gemini.url("/x")))
    assert "The server gave an answer this browser does not understand" in body
    assert f"It answered {status} odd." in body


def test_a_redirect_to_a_non_gemini_address_is_explained_and_a_web_one_can_be_opened(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/web", 30, "https://example.com/page")
    gemini.respond("/mail", 31, "mailto:me@example.com")
    web = text_of(open_page(client, gemini.url("/web")))
    assert "This page moved to another kind of address" in web
    assert (
        'href="https://example.com/page" target="_blank" rel="noopener noreferrer"'
        in web
    )
    mail = text_of(open_page(client, gemini.url("/mail")))
    assert "mailto:me@example.com" in mail
    assert 'href="mailto:' not in mail


def test_a_redirect_to_a_javascript_address_is_never_a_link(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/js", 30, "javascript:alert(1)")
    body = text_of(open_page(client, gemini.url("/js")))
    assert 'href="javascript:' not in body
    assert "javascript:alert(1)" in body


def test_a_redirect_lands_on_the_final_page_and_records_that_address(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path
) -> None:
    gemini.respond("/old", 31, "/new")
    gemini.respond("/new", 20, "text/gemini", b"# New home\n")
    body = text_of(open_page(client, gemini.url("/old")))
    assert "<h1>New home</h1>" in body
    assert f'value="{gemini.url("/new")}"' in body
    assert [visit["url"] for visit in saved_visits(data_dir)] == [gemini.url("/new")]


def test_a_redirect_loop_shows_the_too_many_redirects_page(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/loop", 30, "/loop")
    assert "This page redirects too many times" in text_of(
        open_page(client, gemini.url("/loop"))
    )


def test_a_server_that_answers_nonsense_gets_the_not_valid_gemini_page(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond_raw(gemini.url("/x"), b"<html>hello</html>\r\n")
    assert "The server&#x27;s answer was not valid Gemini" in text_of(
        open_page(client, gemini.url("/x"))
    )


def test_everything_a_server_says_is_escaped_wherever_it_appears(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    attack = "<img src=x onerror=alert(1)>"
    gemini.respond("/prompt", 10, attack)
    gemini.respond("/fail", 51, attack)
    gemini.respond("/type", 20, f"text/plain; x={attack}", b"body")
    gemini.respond(
        "/url", 20, "text/gemini", f"=> /{quote(attack)} {attack}\n".encode()
    )
    for path in ("/prompt", "/fail", "/type", "/url"):
        body = text_of(open_page(client, gemini.url(path)))
        assert "<img" not in body, path


def test_an_address_with_markup_in_its_query_is_escaped_in_the_address_bar(
    client: FlaskClient,
) -> None:
    body = text_of(
        open_page(
            client, f'gemini://127.0.0.1:{unused_port()}/?"><script>alert(1)</script>'
        )
    )
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# History


def test_visiting_a_page_records_it_once_with_its_title(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path
) -> None:
    gemini.respond("/a", 20, "text/gemini", b"# Alpha\n")
    open_page(client, gemini.url("/a"))
    open_page(client, gemini.url("/a"))
    visits = saved_visits(data_dir)
    assert [(visit["url"], visit["title"]) for visit in visits] == [
        (gemini.url("/a"), "Alpha")
    ]


def test_images_and_files_are_recorded_under_their_address(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path
) -> None:
    gemini.respond("/p.png", 20, "image/png", PNG)
    open_page(client, gemini.url("/p.png"))
    assert saved_visits(data_dir)[0]["title"] == gemini.url("/p.png")


def test_prompts_and_redirect_pages_are_not_recorded(
    client: FlaskClient, gemini: FakeGeminiServer, data_dir: Path
) -> None:
    gemini.respond("/ask", 10, "Q?")
    gemini.respond("/web", 30, "https://example.com/")
    open_page(client, gemini.url("/ask"))
    open_page(client, gemini.url("/web"))
    assert saved_visits(data_dir) == []


# Certificates


def test_a_changed_certificate_stops_the_page_and_offers_the_choice_to_trust_it(
    client: FlaskClient,
    gemini: FakeGeminiServer,
    certificate_spare: Certificate,
    data_dir: Path,
) -> None:
    gemini.respond("/p", 20, "text/gemini", b"# Hi\n")
    open_page(client, gemini.url("/p"))
    host_key = f"127.0.0.1:{gemini.port}"
    saved_before = json.loads((data_dir / KNOWN_HOSTS_FILE).read_text())[host_key][
        "fingerprint"
    ]

    gemini.present_certificate(certificate_spare)
    response = open_page(client, gemini.url("/p"), input="private")
    body = text_of(response)
    assert "This site&#x27;s certificate has changed" in body
    assert f"{host_key} now presents a different certificate" in body
    assert f"Saved: {saved_before[:32]}..." in body
    assert f"Now: {certificate_spare.fingerprint[:32]}..." in body
    assert '<form method="post" action="/trust">' in body
    assert f'name="host_key" value="{host_key}"' in body
    assert f'name="fingerprint" value="{certificate_spare.fingerprint}"' in body
    assert "Not loaded: certificate changed" in body
    assert "<h1>Hi</h1>" not in body
    # Nothing was sent to the new certificate's holder and nothing saved yet.
    assert gemini.requested() == [gemini.url("/p")]
    assert (
        json.loads((data_dir / KNOWN_HOSTS_FILE).read_text())[host_key]["fingerprint"]
        == saved_before
    )


def test_trusting_the_new_certificate_saves_it_and_goes_back_to_the_page(
    client: FlaskClient,
    gemini: FakeGeminiServer,
    certificate_spare: Certificate,
    data_dir: Path,
) -> None:
    gemini.respond("/p", 20, "text/gemini", b"# Hi\n")
    open_page(client, gemini.url("/p"))
    gemini.present_certificate(certificate_spare)
    host_key = f"127.0.0.1:{gemini.port}"

    response = client.post(
        "/trust",
        data={
            "host_key": host_key,
            "fingerprint": certificate_spare.fingerprint,
            "return_url": gemini.url("/p"),
        },
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith(
        f"/?url={quote(gemini.url('/p'), safe='')}"
    )
    saved = json.loads((data_dir / KNOWN_HOSTS_FILE).read_text())[host_key]
    assert saved["fingerprint"] == certificate_spare.fingerprint
    assert "<h1>Hi</h1>" in text_of(open_page(client, gemini.url("/p")))


def test_the_certificate_changed_page_form_posts_values_the_trust_route_accepts(
    client: FlaskClient, gemini: FakeGeminiServer, certificate_spare: Certificate
) -> None:
    gemini.respond("/p", 20, "text/gemini", b"# Hi\n")
    open_page(client, gemini.url("/p"))
    gemini.present_certificate(certificate_spare)
    page = text_of(open_page(client, gemini.url("/p")))
    fields = {
        name: page.split(f'name="{name}" value="', 1)[1].split('"', 1)[0]
        for name in ("host_key", "fingerprint", "return_url")
    }
    assert client.post("/trust", data=fields).status_code == 303


@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        "abc",
        "A" * 64,
        "g" * 64,
        "a" * 63,
        "a" * 65,
        "a" * 64 + "\n",
        "<script>" + "a" * 56,
    ],
    ids=["empty", "short", "uppercase", "not-hex", "63", "65", "newline", "markup"],
)
def test_trusting_a_malformed_fingerprint_is_refused_and_saves_nothing(
    client: FlaskClient, data_dir: Path, fingerprint: str
) -> None:
    response = client.post(
        "/trust",
        data={
            "host_key": "example.org:1965",
            "fingerprint": fingerprint,
            "return_url": "gemini://example.org/",
        },
    )
    assert response.status_code == 400
    assert not (data_dir / KNOWN_HOSTS_FILE).exists()


@pytest.mark.parametrize(
    "host_key",
    [
        "",
        "example.org",
        "example.org:",
        "example.org:123456",
        "exa mple.org:1965",
        "a/b:1965",
        "example.org:19x",
    ],
)
def test_trusting_a_malformed_host_is_refused_and_saves_nothing(
    client: FlaskClient, data_dir: Path, host_key: str
) -> None:
    response = client.post(
        "/trust", data={"host_key": host_key, "fingerprint": "a" * 64, "return_url": ""}
    )
    assert response.status_code == 400
    assert not (data_dir / KNOWN_HOSTS_FILE).exists()


def test_trusting_with_no_fields_at_all_is_refused(
    client: FlaskClient, data_dir: Path
) -> None:
    assert client.post("/trust").status_code == 400
    assert not (data_dir / KNOWN_HOSTS_FILE).exists()


@pytest.mark.parametrize("fetch_site", ["cross-site"])
def test_a_trust_request_from_another_site_is_refused_and_saves_nothing(
    client: FlaskClient, data_dir: Path, fetch_site: str
) -> None:
    response = client.post(
        "/trust",
        data={
            "host_key": "example.org:1965",
            "fingerprint": "a" * 64,
            "return_url": "gemini://example.org/",
        },
        headers={"Sec-Fetch-Site": fetch_site},
    )
    assert response.status_code == 403
    assert not (data_dir / KNOWN_HOSTS_FILE).exists()


@pytest.mark.parametrize(
    "return_url",
    [
        "",
        "https://evil.example/",
        "javascript:alert(1)",
        "//evil.example/",
        "/relative",
    ],
)
def test_trusting_only_ever_goes_back_to_a_gemini_page_in_this_browser(
    client: FlaskClient, return_url: str
) -> None:
    response = client.post(
        "/trust",
        data={
            "host_key": "example.org:1965",
            "fingerprint": "a" * 64,
            "return_url": return_url,
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/")
    assert "evil" not in response.headers["Location"]
    assert "javascript" not in response.headers["Location"]


def test_the_trust_route_does_not_answer_a_plain_get(client: FlaskClient) -> None:
    assert client.get("/trust").status_code == 405


# Page chrome and plumbing


def test_every_page_has_the_phone_viewport_the_address_bar_and_the_shell_script(
    client: FlaskClient,
) -> None:
    body = text_of(client.get("/"))
    assert (
        "<meta name='viewport' content='width=device-width, initial-scale=1'>" in body
    )
    assert 'id="back"' in body and 'id="forward"' in body and 'id="reload"' in body
    assert 'aria-label="Gemini address"' in body
    assert '<script src="/app.js" defer></script>' in body
    assert runner.SHELL_PAGE_SCRIPT in body


def test_the_reload_button_reopens_the_current_address(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/p", 20, "text/gemini", b"# Hi\n")
    body = text_of(open_page(client, gemini.url("/p")))
    assert f'id="reload" href="/?url={quote(gemini.url("/p"), safe="")}"' in body


def test_html_pages_carry_a_content_security_policy_that_admits_only_this_pages_script(
    client: FlaskClient, gemini: FakeGeminiServer
) -> None:
    gemini.respond("/p", 20, "text/gemini", b"# Hi\n")
    policy = open_page(client, gemini.url("/p")).headers["Content-Security-Policy"]
    assert policy == runner.CONTENT_SECURITY_POLICY
    assert runner.inline_script_source(runner.SHELL_PAGE_SCRIPT_BODY) in policy
    assert "script-src 'self' 'report-sample' 'sha256-" in policy
    assert "img-src 'self' data: blob:" in policy


def test_the_back_forward_script_is_served_as_javascript_without_a_page_policy(
    client: FlaskClient,
) -> None:
    response = client.get("/app.js")
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert "history.back()" in text_of(response)
    assert "Content-Security-Policy" not in response.headers


def test_only_the_two_shell_modules_are_served_from_the_static_route(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app_contract.js").write_text("export const x = 1;")
    (tmp_path / "secret.js").write_text("nope")
    monkeypatch.setattr(runner, "SHELL_STATIC_MODULES_DIR", tmp_path)
    served = client.get("/_static/app_contract.js")
    assert (served.status_code, served.mimetype, text_of(served)) == (
        200,
        "text/javascript",
        "export const x = 1;",
    )
    assert (
        client.get("/_static/context_menu.js").status_code == 404
    )  # named, but not built here
    assert client.get("/_static/secret.js").status_code == 404
    assert client.get("/_static/..%2Fsecret.js").status_code == 404


def test_a_policy_report_for_a_blocked_resource_is_logged_as_an_error(
    client: FlaskClient,
) -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    try:
        report = {
            "csp-report": {
                "blocked-uri": "https://cdn.example/x.js",
                "document-uri": "http://app/",
                "effective-directive": "script-src",
            }
        }
        response = client.post(
            "/_csp-report",
            data=json.dumps(report),
            content_type="application/csp-report",
        )
    finally:
        logger.remove(sink)
    assert response.status_code == 204
    assert any(
        "https://cdn.example/x.js" in line and "script-src" in line for line in lines
    )


def test_a_policy_report_caused_by_a_browser_extension_is_not_logged(
    client: FlaskClient,
) -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    try:
        report = {
            "csp-report": {
                "blocked-uri": "chrome-extension://abc/x.js",
                "document-uri": "http://app/",
            }
        }
        response = client.post(
            "/_csp-report",
            data=json.dumps(report),
            content_type="application/csp-report",
        )
    finally:
        logger.remove(sink)
    assert response.status_code == 204
    assert lines == []


@pytest.mark.parametrize(
    "payload", ["", "not json", "[1]", '{"csp-report": "text"}', '{"other": {}}']
)
def test_a_policy_report_that_is_not_a_report_is_accepted_and_ignored(
    client: FlaskClient, payload: str
) -> None:
    assert (
        client.post(
            "/_csp-report", data=payload, content_type="application/csp-report"
        ).status_code
        == 204
    )
