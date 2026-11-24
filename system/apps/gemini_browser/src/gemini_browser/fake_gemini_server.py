"""A Gemini server for tests: self-signed TLS on a loopback port, canned answers, and a log of what it was asked."""

import hashlib
import socket
import socketserver
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

LOOPBACK = "127.0.0.1"
NOT_FOUND = b"51 Not found\r\n"
MAX_REQUEST_LINE = 4096


class Certificate(FrozenModel):
    """A self-signed certificate on disk, with the fingerprint the browser saves for it."""

    certificate_file: Path = Field(description="PEM certificate")
    key_file: Path = Field(description="PEM private key")
    fingerprint: str = Field(
        description="SHA-256 of the certificate in DER form, in hex"
    )


def make_certificate(directory: Path, name: str) -> Certificate:
    """Write a fresh self-signed certificate and its key into ``directory``."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    certificate_file = directory / f"{name}.crt"
    key_file = directory / f"{name}.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return Certificate(
        certificate_file=certificate_file,
        key_file=key_file,
        fingerprint=hashlib.sha256(der).hexdigest(),
    )


def answer(status: int, meta: str, body: bytes = b"") -> bytes:
    """The bytes of a Gemini answer: a header line, then the body."""
    return f"{status} {meta}\r\n".encode() + body


class FakeGeminiServer:
    """Answers each request line with the canned answer registered for it, or ``51 Not found``.

    An answer registered as empty bytes makes the server hang up without a word.
    """

    def __init__(self, certificate: Certificate) -> None:
        self.requests: list[str] = []
        self._answers: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._context = self._context_for(certificate)
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((LOOPBACK, 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )

    @staticmethod
    def _context_for(certificate: Certificate) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate.certificate_file, certificate.key_file)
        return context

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, path: str = "/", query: str = "") -> str:
        suffix = f"?{query}" if query else ""
        return f"gemini://{LOOPBACK}:{self.port}{path}{suffix}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def present_certificate(self, certificate: Certificate) -> None:
        """Present another certificate from the next connection on."""
        self._context = self._context_for(certificate)

    def respond(
        self, path: str, status: int, meta: str, body: bytes = b"", query: str = ""
    ) -> None:
        """Answer a request for ``path`` (and ``query``) with this status, meta and body."""
        self.respond_raw(self.url(path, query), answer(status, meta, body))

    def respond_raw(self, request_line: str, payload: bytes) -> None:
        """Answer a request for exactly this address with exactly these bytes."""
        with self._lock:
            self._answers[request_line] = payload

    def requested(self) -> list[str]:
        with self._lock:
            return list(self.requests)

    def _handle(self, connection: socket.socket) -> None:
        try:
            tls = self._context.wrap_socket(connection, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            buffer = b""
            while b"\r\n" not in buffer and len(buffer) < MAX_REQUEST_LINE:
                chunk = tls.recv(1024)
                if not chunk:
                    return
                buffer += chunk
            line = buffer.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            with self._lock:
                self.requests.append(line)
                payload = self._answers.get(line, NOT_FOUND)
            if payload:
                tls.sendall(payload)
        except OSError:
            return
        finally:
            try:
                tls.close()
            except OSError:
                pass
