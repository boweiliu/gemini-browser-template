"""A small Gemini client: one request, one answer, certificates remembered on first use."""

import hashlib
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from gemini_browser.gemini_errors import GeminiError
from gemini_browser.gemini_store import CertificateCheck

DEFAULT_PORT = 1965
MAX_URL_BYTES = 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 15.0
TOTAL_TIMEOUT_SECONDS = 30.0
# Two status digits, a space and up to 1024 bytes of meta come before the line break.
HEADER_LIMIT = 3 + 1024
REDIRECT_STATUSES = (30, 31)

_HEADER = re.compile(r"^(\d\d)(?: (.*))?$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HOST_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"

VerifyCertificate = Callable[[str, str], CertificateCheck]


class FetchResult(FrozenModel):
    """The server's answer to one request."""

    url: str = Field(description="The address that answered, after any redirects")
    status: int = Field(description="The two-digit Gemini status code")
    meta: str = Field(
        description="The header text after the status: a MIME type, a prompt, a new address or a message"
    )
    body: bytes = Field(
        description="The page content; empty unless the status is in the 20s"
    )
    truncated: bool = Field(
        description="True when the page was longer than the browser will load"
    )
    certificate: CertificateCheck = Field(
        description="How the server's certificate compared with the saved one"
    )


def normalize_address(text: str) -> str:
    """Turn what a person typed into an absolute gemini:// address, or explain what is wrong with it."""
    cleaned = text.strip()
    if not cleaned:
        raise GeminiError(
            "Enter an address",
            "Type a gemini:// address in the bar above and press Enter.",
        )
    if "://" not in cleaned:
        cleaned = f"gemini://{cleaned}"
    parts = urlsplit(cleaned)
    if parts.scheme.lower() != "gemini":
        raise GeminiError(
            "This browser opens gemini:// addresses only",
            f"{parts.scheme}:// addresses open in a web browser or another app.",
        )
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise GeminiError(
            "That is not a valid address", f"Check the address {cleaned!r} for typos."
        ) from None
    if not host or parts.username is not None or parts.password is not None:
        raise GeminiError(
            "That is not a valid address",
            "A Gemini address looks like gemini://example.org/page.",
        )
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise GeminiError(
            "That is not a valid address", f"The site name {host!r} cannot be used."
        ) from None
    if ":" not in host and not _HOST_NAME.match(host):
        raise GeminiError(
            "That is not a valid address", f"The site name {host!r} cannot be used."
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORT:
        netloc = f"{netloc}:{port}"
    path = quote(parts.path or "/", safe=_PATH_SAFE)
    return urlunsplit(("gemini", netloc, path, parts.query, ""))


def replace_query(url: str, text: str) -> str:
    """The same address with the person's answer to an input prompt as its query."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, quote(text, safe=""), "")
    )


def without_query(url: str) -> str:
    """The address with any query and fragment removed."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def resolve_link(base_url: str, target: str) -> str:
    """Resolve a link found on a page against the page's own address."""
    target = target.strip()
    if _SCHEME.match(target):
        return target
    # urljoin only knows how to join paths for the schemes it was told about, so join as https.
    swapped = (
        f"https{base_url[len('gemini') :]}"
        if base_url.startswith("gemini://")
        else base_url
    )
    joined = urljoin(swapped, target)
    return (
        f"gemini{joined[len('https') :]}" if joined.startswith("https://") else joined
    )


def host_key_of(url: str) -> str:
    """The host and port a certificate is remembered under."""
    parts = urlsplit(url)
    return f"{parts.hostname or ''}:{parts.port or DEFAULT_PORT}"


def _require_public(host: str, address: str) -> None:
    """Refuse an address inside a private network, for a visitor: the browser must not become a way in."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise GeminiError(
            "This address is not open to visitors",
            f"{host} points inside a private network. Visitors can open public Gemini sites only.",
        )


def _connection_failure(host: str, port: int, error: OSError | None) -> GeminiError:
    if isinstance(error, ConnectionRefusedError):
        return GeminiError(
            "The server refused the connection",
            f"{host} is reachable but nothing is listening on port {port}.",
        )
    if isinstance(error, TimeoutError):
        return GeminiError(
            "The server did not answer",
            f"{host}:{port} took too long to respond. It may be down or blocked.",
        )
    return GeminiError(
        "Could not connect",
        f"{host}:{port}: {(error.strerror or error) if error is not None else 'no address to try'}",
    )


def _open_socket(host: str, port: int, allow_private_addresses: bool, trusted_hosts: frozenset[str]) -> socket.socket:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise GeminiError(
            "Can't find that server",
            f"No address was found for {host}. Check the spelling.",
        ) from None
    # Every address is checked, and the connection goes to one of those same addresses, so the name cannot
    # be re-resolved to a private one between the check and the connect.
    if not allow_private_addresses and host.lower() not in trusted_hosts:
        for address in addresses:
            _require_public(host, str(address[4][0]))
    last_error: OSError | None = None
    for family, socket_type, protocol, _canonical_name, socket_address in addresses:
        candidate = socket.socket(family, socket_type, protocol)
        candidate.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            candidate.connect(socket_address)
        except OSError as error:
            candidate.close()
            last_error = error
            continue
        return candidate
    raise _connection_failure(host, port, last_error)


def _connect(host: str, port: int, allow_private_addresses: bool, trusted_hosts: frozenset[str]) -> ssl.SSLSocket:
    # Gemini certificates are self-signed by convention, so trust is decided by the saved fingerprint,
    # not by a certificate authority.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = _open_socket(host, port, allow_private_addresses, trusted_hosts)
    raw.settimeout(READ_TIMEOUT_SECONDS)
    try:
        return context.wrap_socket(raw, server_hostname=host)
    except TimeoutError:
        raw.close()
        raise GeminiError(
            "The secure connection timed out",
            f"{host} did not finish the TLS handshake.",
        ) from None
    except OSError as error:
        raw.close()
        raise GeminiError(
            "The secure connection failed",
            f"{host}:{port} did not complete TLS: {error}",
        ) from error


def _read_response(tls: ssl.SSLSocket, deadline: float) -> tuple[int, str, bytes, bool]:
    buffer = bytearray()
    while b"\r\n" not in buffer and len(buffer) <= HEADER_LIMIT:
        chunk = tls.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
    end = buffer.find(b"\r\n")
    if end < 0 or end > HEADER_LIMIT:
        raise GeminiError(
            "The server's answer was not valid Gemini",
            "It did not start with a status line.",
        )
    match = _HEADER.match(buffer[:end].decode("utf-8", errors="replace"))
    if match is None:
        raise GeminiError(
            "The server's answer was not valid Gemini", "Its status line was malformed."
        )
    status = int(match.group(1))
    meta = match.group(2) or ""
    # Only a success carries a body; whatever followed the header of any other answer is not page content.
    body = bytearray(buffer[end + 2 :] if 20 <= status < 30 else b"")
    while 20 <= status < 30 and len(body) <= MAX_BODY_BYTES:
        if time.monotonic() > deadline:
            raise GeminiError(
                "The server is too slow", "It did not finish sending the page in time."
            )
        chunk = tls.recv(65536)
        if not chunk:
            break
        body.extend(chunk)
    truncated = len(body) > MAX_BODY_BYTES
    return status, meta, bytes(body[:MAX_BODY_BYTES]), truncated


def _request_once(
    url: str, verify: VerifyCertificate, allow_private_addresses: bool, trusted_hosts: frozenset[str]
) -> FetchResult:
    if len(url.encode("utf-8")) > MAX_URL_BYTES:
        raise GeminiError(
            "That address is too long", "Gemini addresses can be at most 1024 bytes."
        )
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or DEFAULT_PORT
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    tls = _connect(host, port, allow_private_addresses, trusted_hosts)
    try:
        certificate = tls.getpeercert(binary_form=True)
        if not certificate:
            raise GeminiError(
                "The server sent no certificate", f"{host} did not identify itself."
            )
        # Check the certificate before sending the address, so an impostor never learns what was asked for.
        check = verify(f"{host}:{port}", hashlib.sha256(certificate).hexdigest())
        tls.sendall(f"{url}\r\n".encode())
        status, meta, body, truncated = _read_response(tls, deadline)
    except TimeoutError:
        raise GeminiError(
            "The server stopped responding",
            f"{host} went quiet while sending its answer.",
        ) from None
    except OSError as error:
        raise GeminiError("The connection broke", f"{host}: {error}") from error
    finally:
        tls.close()
    return FetchResult(
        url=url,
        status=status,
        meta=meta,
        body=body,
        truncated=truncated,
        certificate=check,
    )


def fetch(
    url: str,
    verify: VerifyCertificate,
    *,
    allow_private_addresses: bool,
    trusted_hosts: frozenset[str] = frozenset(),
) -> FetchResult:
    """Load an address, following redirects to other gemini:// addresses (a redirect elsewhere is returned as is).

    Without ``allow_private_addresses`` every hop must resolve to public addresses only, except for a host named
    in ``trusted_hosts`` (lower case): the owner vouches for those, so they may resolve anywhere.
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        result = _request_once(current, verify, allow_private_addresses, trusted_hosts)
        if result.status not in REDIRECT_STATUSES:
            return result
        target = resolve_link(current, result.meta)
        if not target.lower().startswith("gemini://"):
            return result
        if hop == MAX_REDIRECTS:
            break
        current = normalize_address(target)
    raise GeminiError(
        "This page redirects too many times",
        f"The server sent {MAX_REDIRECTS} redirects in a row.",
    )
