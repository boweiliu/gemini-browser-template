import socket
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import pytest

from gemini_browser import gemini_client
from gemini_browser.fake_gemini_server import Certificate, FakeGeminiServer, answer
from gemini_browser.gemini_client import (
    MAX_REDIRECTS,
    MAX_URL_BYTES,
    host_key_of,
    normalize_address,
    replace_query,
    resolve_link,
    without_query,
)
from gemini_browser.gemini_errors import CertificateChangedError, GeminiError
from gemini_browser.gemini_store import CertificateCheck, trust_host, verify_host

# The fake servers listen on loopback, which only the owner may open: these tests are the owner's.
fetch = partial(gemini_client.fetch, allow_private_addresses=True)
ServerFactory = Callable[[], tuple[FakeGeminiServer, Certificate]]
NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def accept_anything(host_key: str, fingerprint: str) -> CertificateCheck:
    return CertificateCheck(first_seen="2026-03-01T00:00:00+00:00", is_new=True)


def remembering(directory: Path) -> Callable[[str, str], CertificateCheck]:
    return lambda host_key, fingerprint: verify_host(
        directory, host_key, fingerprint, NOW
    )


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("example.org", "gemini://example.org/"),
        ("  gemini://example.org  ", "gemini://example.org/"),
        ("GEMINI://Example.ORG/Path", "gemini://example.org/Path"),
        ("gemini://example.org:1965/x", "gemini://example.org/x"),
        ("gemini://example.org:1966/x", "gemini://example.org:1966/x"),
        ("gemini://example.org/a b", "gemini://example.org/a%20b"),
        ("gemini://example.org/a%20b", "gemini://example.org/a%20b"),
        ("gemini://example.org/p?q=1&r=2", "gemini://example.org/p?q=1&r=2"),
        ("gemini://example.org/p#frag", "gemini://example.org/p"),
        ("gemini://bücher.example/", "gemini://xn--bcher-kva.example/"),
        ("gemini://[::1]:1966/", "gemini://[::1]:1966/"),
        ("127.0.0.1:1966/x", "gemini://127.0.0.1:1966/x"),
    ],
)
def test_normalize_address_turns_what_was_typed_into_an_absolute_address(
    typed: str, expected: str
) -> None:
    assert normalize_address(typed) == expected


@pytest.mark.parametrize(
    ("typed", "title"),
    [
        ("", "Enter an address"),
        ("   ", "Enter an address"),
        ("https://example.org/", "This browser opens gemini:// addresses only"),
        ("mailto://x@example.org", "This browser opens gemini:// addresses only"),
        ("gemini://", "That is not a valid address"),
        ("gemini://user:pass@example.org/", "That is not a valid address"),
        ("gemini://user@example.org/", "That is not a valid address"),
        ("gemini://example.org:notaport/", "That is not a valid address"),
        ("gemini://example.org:99999/", "That is not a valid address"),
        ("gemini://exa mple.org/", "That is not a valid address"),
    ],
)
def test_normalize_address_explains_what_is_wrong_with_a_bad_address(
    typed: str, title: str
) -> None:
    with pytest.raises(GeminiError) as raised:
        normalize_address(typed)
    assert raised.value.title == title
    assert raised.value.detail


def test_replace_query_encodes_the_answer_and_drops_any_old_query_and_fragment() -> (
    None
):
    assert (
        replace_query("gemini://example.org/ask?old=1#f", "a b&c=d/é")
        == "gemini://example.org/ask?a%20b%26c%3Dd%2F%C3%A9"
    )
    assert replace_query("gemini://example.org/ask", "") == "gemini://example.org/ask"


def test_without_query_removes_query_and_fragment() -> None:
    assert (
        without_query("gemini://example.org:1966/ask?x=1#f")
        == "gemini://example.org:1966/ask"
    )


@pytest.mark.parametrize(
    ("base", "target", "expected"),
    [
        ("gemini://example.org/a/b.gmi", "c.gmi", "gemini://example.org/a/c.gmi"),
        ("gemini://example.org/a/b.gmi", "../c.gmi", "gemini://example.org/c.gmi"),
        ("gemini://example.org/a/b.gmi", "/c.gmi", "gemini://example.org/c.gmi"),
        ("gemini://example.org/a/b.gmi", "//other.net/x", "gemini://other.net/x"),
        ("gemini://example.org/a/b.gmi", "?q=1", "gemini://example.org/a/b.gmi?q=1"),
        ("gemini://example.org:1966/a/", "b", "gemini://example.org:1966/a/b"),
        ("gemini://example.org/a", "gemini://other.net/", "gemini://other.net/"),
        ("gemini://example.org/a", "https://web.example/x", "https://web.example/x"),
        ("gemini://example.org/a", "mailto:me@example.org", "mailto:me@example.org"),
        ("gemini://example.org/a", "  c  ", "gemini://example.org/c"),
    ],
)
def test_resolve_link_joins_against_the_page_address(
    base: str, target: str, expected: str
) -> None:
    assert resolve_link(base, target) == expected


def test_host_key_names_host_and_port_with_the_default_port_filled_in() -> None:
    assert host_key_of("gemini://example.org/x") == "example.org:1965"
    assert host_key_of("gemini://example.org:1966/x") == "example.org:1966"


def test_fetch_returns_status_meta_and_body_of_a_page(gemini: FakeGeminiServer) -> None:
    gemini.respond("/hello", 20, "text/gemini; charset=utf-8", b"# Hi\nthere\n")
    result = fetch(gemini.url("/hello"), accept_anything)
    assert (result.url, result.status, result.meta, result.body) == (
        gemini.url("/hello"),
        20,
        "text/gemini; charset=utf-8",
        b"# Hi\nthere\n",
    )
    assert result.truncated is False
    assert gemini.requested() == [gemini.url("/hello")]


def test_fetch_sends_exactly_the_address_it_was_given_including_the_query(
    gemini: FakeGeminiServer,
) -> None:
    gemini.respond("/q", 20, "text/plain", b"ok", query="a%20b")
    fetch(gemini.url("/q", "a%20b"), accept_anything)
    assert gemini.requested() == [gemini.url("/q", "a%20b")]


def test_fetch_keeps_the_header_of_an_answer_without_a_body(
    gemini: FakeGeminiServer,
) -> None:
    gemini.respond("/ask", 10, "What is your name?")
    result = fetch(gemini.url("/ask"), accept_anything)
    assert (result.status, result.meta, result.body) == (10, "What is your name?", b"")


def test_a_status_line_without_meta_is_accepted(gemini: FakeGeminiServer) -> None:
    gemini.respond_raw(gemini.url("/x"), b"20\r\nbody")
    result = fetch(gemini.url("/x"), accept_anything)
    assert (result.status, result.meta, result.body) == (20, "", b"body")


def test_an_error_answer_never_carries_a_body(gemini: FakeGeminiServer) -> None:
    gemini.respond("/gone", 52, "Gone", b"ignored")
    result = fetch(gemini.url("/gone"), accept_anything)
    assert (result.status, result.body) == (52, b"")


def test_a_relative_redirect_is_followed_to_the_new_address(
    gemini: FakeGeminiServer,
) -> None:
    gemini.respond("/old", 31, "/new")
    gemini.respond("/new", 20, "text/plain", b"arrived")
    result = fetch(gemini.url("/old"), accept_anything)
    assert result.url == gemini.url("/new")
    assert result.body == b"arrived"
    assert gemini.requested() == [gemini.url("/old"), gemini.url("/new")]


def test_a_redirect_to_another_host_is_followed_and_checks_that_hosts_certificate(
    make_server: ServerFactory, tmp_path: Path
) -> None:
    first, _ = make_server()
    second, second_certificate = make_server()
    first.respond("/go", 30, second.url("/dest"))
    second.respond("/dest", 20, "text/plain", b"over here")
    result = fetch(first.url("/go"), remembering(tmp_path / "state"))
    assert result.url == second.url("/dest")
    assert result.body == b"over here"
    assert result.certificate.is_new is True
    saved = (tmp_path / "state" / "known_hosts.json").read_text()
    assert f"127.0.0.1:{first.port}" in saved
    assert f"127.0.0.1:{second.port}" in saved
    assert second_certificate.fingerprint in saved


def test_a_redirect_to_a_web_address_is_returned_not_followed(
    gemini: FakeGeminiServer,
) -> None:
    gemini.respond("/away", 31, "https://example.com/page")
    result = fetch(gemini.url("/away"), accept_anything)
    assert (result.status, result.meta) == (31, "https://example.com/page")
    assert gemini.requested() == [gemini.url("/away")]


def test_a_chain_of_exactly_the_allowed_number_of_redirects_still_arrives(
    gemini: FakeGeminiServer,
) -> None:
    for number in range(MAX_REDIRECTS):
        gemini.respond(f"/hop{number}", 30, f"/hop{number + 1}")
    gemini.respond(f"/hop{MAX_REDIRECTS}", 20, "text/plain", b"end")
    assert fetch(gemini.url("/hop0"), accept_anything).body == b"end"


def test_one_redirect_more_than_allowed_is_refused_with_an_explanation(
    gemini: FakeGeminiServer,
) -> None:
    for number in range(MAX_REDIRECTS + 1):
        gemini.respond(f"/hop{number}", 30, f"/hop{number + 1}")
    gemini.respond(f"/hop{MAX_REDIRECTS + 1}", 20, "text/plain", b"never reached")
    with pytest.raises(GeminiError) as raised:
        fetch(gemini.url("/hop0"), accept_anything)
    assert raised.value.title == "This page redirects too many times"
    assert len(gemini.requested()) == MAX_REDIRECTS + 1


def test_a_redirect_loop_is_cut_off(gemini: FakeGeminiServer) -> None:
    gemini.respond("/loop", 30, "/loop")
    with pytest.raises(GeminiError) as raised:
        fetch(gemini.url("/loop"), accept_anything)
    assert raised.value.title == "This page redirects too many times"


def test_a_different_certificate_is_refused_before_the_address_is_sent(
    gemini: FakeGeminiServer, certificate_spare: Certificate, tmp_path: Path
) -> None:
    gemini.respond("/secret-path", 20, "text/plain", b"page")
    verify = remembering(tmp_path)
    fetch(gemini.url("/secret-path"), verify)
    assert gemini.requested() == [gemini.url("/secret-path")]

    gemini.present_certificate(certificate_spare)
    with pytest.raises(CertificateChangedError) as raised:
        fetch(gemini.url("/secret-path", "password=hunter2"), verify)
    assert raised.value.new_fingerprint == certificate_spare.fingerprint
    # The impostor was never told what was asked for: the server logged no second request.
    assert gemini.requested() == [gemini.url("/secret-path")]


def test_a_certificate_the_person_chose_to_trust_is_accepted_afterwards(
    gemini: FakeGeminiServer, certificate_spare: Certificate, tmp_path: Path
) -> None:
    gemini.respond("/p", 20, "text/plain", b"page")
    verify = remembering(tmp_path)
    fetch(gemini.url("/p"), verify)
    gemini.present_certificate(certificate_spare)
    with pytest.raises(CertificateChangedError):
        fetch(gemini.url("/p"), verify)
    trust_host(tmp_path, f"127.0.0.1:{gemini.port}", certificate_spare.fingerprint, NOW)
    assert fetch(gemini.url("/p"), verify).body == b"page"


def test_a_redirect_to_a_host_with_a_changed_certificate_is_refused(
    make_server: ServerFactory, certificate_spare: Certificate, tmp_path: Path
) -> None:
    first, _ = make_server()
    second, _ = make_server()
    verify = remembering(tmp_path)
    second.respond("/dest", 20, "text/plain", b"fine")
    fetch(second.url("/dest"), verify)
    second.present_certificate(certificate_spare)
    first.respond("/go", 30, second.url("/dest"))
    with pytest.raises(CertificateChangedError) as raised:
        fetch(first.url("/go"), verify)
    assert raised.value.host_key == f"127.0.0.1:{second.port}"
    assert second.requested() == [second.url("/dest")]


def test_an_address_over_the_length_limit_is_refused_without_connecting(
    gemini: FakeGeminiServer,
) -> None:
    long_path = "/" + "a" * MAX_URL_BYTES
    with pytest.raises(GeminiError) as raised:
        fetch(gemini.url(long_path), accept_anything)
    assert raised.value.title == "That address is too long"
    assert gemini.requested() == []


def test_an_address_of_exactly_the_length_limit_is_sent(
    gemini: FakeGeminiServer,
) -> None:
    prefix = gemini.url("/")
    url = prefix + "a" * (MAX_URL_BYTES - len(prefix.encode()))
    assert len(url.encode()) == MAX_URL_BYTES
    assert fetch(url, accept_anything).status == 51
    assert gemini.requested() == [url]


def test_the_length_limit_counts_bytes_not_characters(gemini: FakeGeminiServer) -> None:
    url = gemini.url("/" + "é" * 600)
    assert len(url) < MAX_URL_BYTES < len(url.encode())
    with pytest.raises(GeminiError) as raised:
        fetch(url, accept_anything)
    assert raised.value.title == "That address is too long"


def test_a_page_longer_than_the_limit_is_cut_and_marked_truncated(
    gemini: FakeGeminiServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_client, "MAX_BODY_BYTES", 1000)
    gemini.respond("/big", 20, "text/plain", b"x" * 5000)
    result = fetch(gemini.url("/big"), accept_anything)
    assert len(result.body) == 1000
    assert result.truncated is True


def test_a_page_exactly_at_the_limit_is_not_marked_truncated(
    gemini: FakeGeminiServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gemini_client, "MAX_BODY_BYTES", 1000)
    gemini.respond("/edge", 20, "text/plain", b"x" * 1000)
    result = fetch(gemini.url("/edge"), accept_anything)
    assert (len(result.body), result.truncated) == (1000, False)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"no line break at all",
        b"hello there\r\n",
        b"2\r\n",
        b"200 too many digits\r\n",
        b"20" + b"x" * 2000,
    ],
    ids=[
        "silence",
        "no-line-break",
        "not-a-status",
        "one-digit",
        "three-digits",
        "header-too-long",
    ],
)
def test_an_answer_that_is_not_gemini_is_reported_as_such(
    gemini: FakeGeminiServer, payload: bytes
) -> None:
    gemini.respond_raw(gemini.url("/x"), payload)
    with pytest.raises(GeminiError) as raised:
        fetch(gemini.url("/x"), accept_anything)
    assert raised.value.title == "The server's answer was not valid Gemini"


def test_a_server_that_is_not_listening_is_reported_as_refusing_the_connection() -> (
    None
):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    with pytest.raises(GeminiError) as raised:
        fetch(f"gemini://127.0.0.1:{port}/", accept_anything)
    assert raised.value.title == "The server refused the connection"
    assert str(port) in raised.value.detail


def test_a_name_that_does_not_resolve_is_reported_as_an_unknown_server() -> None:
    with pytest.raises(GeminiError) as raised:
        fetch("gemini://no-such-host.invalid/", accept_anything)
    assert raised.value.title == "Can't find that server"


def test_a_server_that_does_not_speak_tls_is_reported_as_a_failed_secure_connection() -> (
    None
):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:

        def reply_in_plain_text() -> None:
            connection, _ = listener.accept()
            connection.recv(1024)
            connection.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
            connection.close()

        thread = threading.Thread(target=reply_in_plain_text, daemon=True)
        thread.start()
        with pytest.raises(GeminiError) as raised:
            fetch(f"gemini://127.0.0.1:{port}/", accept_anything)
        thread.join(timeout=5)
    finally:
        listener.close()
    assert raised.value.title == "The secure connection failed"


def test_the_helper_answer_builds_a_header_line_and_body() -> None:
    assert answer(20, "text/plain", b"x") == b"20 text/plain\r\nx"


# Visitors open public addresses only


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1%eth0", "fc00::1", "::ffff:10.0.0.1"],
)
def test_private_and_local_addresses_are_refused_for_a_visitor(address: str) -> None:
    with pytest.raises(GeminiError) as refused:
        gemini_client._require_public("example.org", address)
    assert refused.value.title == "This address is not open to visitors"


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed_for_a_visitor(address: str) -> None:
    gemini_client._require_public("example.org", address)


def test_a_visitor_is_refused_a_server_on_loopback_before_anything_is_sent(
    make_server: ServerFactory,
) -> None:
    server, _certificate = make_server()
    server.respond("/", 20, "text/gemini", b"# Hello\n")
    with pytest.raises(GeminiError) as refused:
        gemini_client.fetch(server.url("/"), accept_anything, allow_private_addresses=False)
    assert refused.value.title == "This address is not open to visitors"
    assert server.requested() == []


def test_a_visitor_is_refused_an_address_written_as_a_private_number_without_any_lookup() -> None:
    with pytest.raises(GeminiError) as refused:
        gemini_client.fetch("gemini://192.168.1.1/", accept_anything, allow_private_addresses=False)
    assert refused.value.title == "This address is not open to visitors"


def test_a_visitor_is_refused_when_any_one_of_a_names_addresses_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mixed = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 1965)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 1965)),
    ]
    monkeypatch.setattr(gemini_client.socket, "getaddrinfo", lambda *args, **kwargs: mixed)
    with pytest.raises(GeminiError) as refused:
        gemini_client.fetch("gemini://rebinding.example/", accept_anything, allow_private_addresses=False)
    assert refused.value.title == "This address is not open to visitors"


def test_a_visitor_may_open_a_host_the_owner_vouches_for_even_though_it_is_private(
    make_server: ServerFactory,
) -> None:
    server, _certificate = make_server()
    server.respond("/", 20, "text/gemini", b"# Hello\n")
    result = gemini_client.fetch(
        server.url("/"),
        accept_anything,
        allow_private_addresses=False,
        trusted_hosts=frozenset({"127.0.0.1"}),
    )
    assert result.body == b"# Hello\n"


def test_vouching_for_one_host_does_not_open_another_private_one(
    make_server: ServerFactory,
) -> None:
    server, _certificate = make_server()
    server.respond("/", 20, "text/gemini", b"# Hello\n")
    other_name = server.url("/").replace("127.0.0.1", "localhost")
    with pytest.raises(GeminiError) as refused:
        gemini_client.fetch(
            other_name,
            accept_anything,
            allow_private_addresses=False,
            trusted_hosts=frozenset({"127.0.0.1"}),
        )
    assert refused.value.title == "This address is not open to visitors"
    assert server.requested() == []


def test_a_redirect_from_a_vouched_host_to_an_unvouched_private_one_is_refused(
    make_server: ServerFactory,
) -> None:
    first, _first_certificate = make_server()
    second, _second_certificate = make_server()
    second.respond("/", 20, "text/gemini", b"# Behind\n")
    first.respond("/go", 30, second.url("/").replace("127.0.0.1", "localhost"))
    with pytest.raises(GeminiError) as refused:
        gemini_client.fetch(
            first.url("/go"),
            accept_anything,
            allow_private_addresses=False,
            trusted_hosts=frozenset({"127.0.0.1"}),
        )
    assert refused.value.title == "This address is not open to visitors"
    assert second.requested() == []
