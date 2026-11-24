from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from gemini_browser import runner
from gemini_browser.fake_gemini_server import (
    Certificate,
    FakeGeminiServer,
    make_certificate,
)


@pytest.fixture
def make_server(
    tmp_path: Path,
) -> Iterator[Callable[[], tuple[FakeGeminiServer, Certificate]]]:
    """Start fake Gemini servers on loopback ports, each with its own certificate; all stop after the test."""
    started: list[FakeGeminiServer] = []

    def build() -> tuple[FakeGeminiServer, Certificate]:
        certificate = make_certificate(tmp_path, f"server{len(started)}")
        server = FakeGeminiServer(certificate)
        server.start()
        started.append(server)
        return server, certificate

    yield build
    for server in started:
        server.stop()


@pytest.fixture
def certificate_spare(tmp_path: Path) -> Certificate:
    """A certificate no server presents until a test asks one to."""
    return make_certificate(tmp_path, "spare")


@pytest.fixture
def gemini(
    make_server: Callable[[], tuple[FakeGeminiServer, Certificate]],
) -> FakeGeminiServer:
    server, _certificate = make_server()
    return server


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's data directory, a fresh one per test."""
    directory = tmp_path / "data"
    monkeypatch.setattr(runner, "DATA_DIR", directory)
    return directory


@pytest.fixture
def client(data_dir: Path) -> FlaskClient:
    return runner.app.test_client()
