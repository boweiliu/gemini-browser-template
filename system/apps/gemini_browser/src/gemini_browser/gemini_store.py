"""What the browser remembers between runs: the certificate each site showed first, and the pages visited."""

import os
import threading
from datetime import datetime
from pathlib import Path

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field, TypeAdapter, ValidationError

from gemini_browser.gemini_errors import CertificateChangedError

KNOWN_HOSTS_FILE = "known_hosts.json"
VISITS_FILE = "history.json"
MAX_VISITS = 50

# One lock for both files: a request thread reads, changes and rewrites a whole file.
_LOCK = threading.Lock()


class CertificateCheck(FrozenModel):
    """How a site's certificate compared with the one saved for it."""

    first_seen: str = Field(
        description="When this site's certificate was first saved, as an ISO timestamp"
    )
    is_new: bool = Field(description="True when this visit is the one that saved it")


class KnownHost(FrozenModel):
    """The certificate fingerprint saved for one host and port."""

    fingerprint: str = Field(description="SHA-256 of the certificate, in hex")
    first_seen: str = Field(description="When it was first saved, as an ISO timestamp")
    last_seen: str = Field(
        description="When a visit last matched it, as an ISO timestamp"
    )


class Visit(FrozenModel):
    """One page the person has opened."""

    url: str = Field(description="The gemini:// address of the page")
    title: str = Field(
        description="The page's first heading, or its address when it has none"
    )
    visited_at: str = Field(description="When it was last opened, as an ISO timestamp")


_KNOWN_HOSTS = TypeAdapter(dict[str, KnownHost])
_VISITS = TypeAdapter(list[Visit])


def _read_known_hosts(directory: Path) -> dict[str, KnownHost]:
    try:
        return _KNOWN_HOSTS.validate_json(
            (directory / KNOWN_HOSTS_FILE).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ValidationError):
        return {}


def _read_visits(directory: Path) -> list[Visit]:
    try:
        return _VISITS.validate_json(
            (directory / VISITS_FILE).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ValidationError):
        return []


def _write(directory: Path, name: str, payload: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{name}.tmp"
    temporary.write_bytes(payload)
    os.replace(temporary, directory / name)


def verify_host(
    directory: Path, host_key: str, fingerprint: str, now: datetime
) -> CertificateCheck:
    """Compare a site's certificate with the saved one: save it on a first visit, refuse a different one."""
    stamp = now.isoformat(timespec="seconds")
    with _LOCK:
        known = _read_known_hosts(directory)
        saved = known.get(host_key)
        if saved is None:
            known[host_key] = KnownHost(
                fingerprint=fingerprint, first_seen=stamp, last_seen=stamp
            )
            _write(directory, KNOWN_HOSTS_FILE, _KNOWN_HOSTS.dump_json(known, indent=2))
            return CertificateCheck(first_seen=stamp, is_new=True)
        if saved.fingerprint != fingerprint:
            raise CertificateChangedError(
                host_key, saved.fingerprint, fingerprint, saved.first_seen
            )
        known[host_key] = saved.model_copy(update={"last_seen": stamp})
        _write(directory, KNOWN_HOSTS_FILE, _KNOWN_HOSTS.dump_json(known, indent=2))
        return CertificateCheck(first_seen=saved.first_seen, is_new=False)


def trust_host(directory: Path, host_key: str, fingerprint: str, now: datetime) -> None:
    """Replace the saved certificate for a site with the one the person chose to trust."""
    stamp = now.isoformat(timespec="seconds")
    with _LOCK:
        known = _read_known_hosts(directory)
        known[host_key] = KnownHost(
            fingerprint=fingerprint, first_seen=stamp, last_seen=stamp
        )
        _write(directory, KNOWN_HOSTS_FILE, _KNOWN_HOSTS.dump_json(known, indent=2))


def record_visit(directory: Path, url: str, title: str, now: datetime) -> None:
    """Put a page at the front of the visited list, once, keeping the most recent ones."""
    with _LOCK:
        others = [visit for visit in _read_visits(directory) if visit.url != url]
        visits = [
            Visit(url=url, title=title, visited_at=now.isoformat(timespec="seconds")),
            *others,
        ][:MAX_VISITS]
        _write(directory, VISITS_FILE, _VISITS.dump_json(visits, indent=2))


def recent_visits(directory: Path, limit: int) -> list[Visit]:
    """The most recently opened pages, newest first."""
    with _LOCK:
        return _read_visits(directory)[:limit]
