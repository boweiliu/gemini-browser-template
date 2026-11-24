import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gemini_browser.gemini_errors import CertificateChangedError
from gemini_browser.gemini_store import (
    KNOWN_HOSTS_FILE,
    MAX_VISITS,
    VISITS_FILE,
    recent_visits,
    record_visit,
    trust_host,
    verify_host,
)

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
FIRST = "a" * 64
SECOND = "b" * 64


def test_the_first_visit_saves_the_certificate_and_says_so(tmp_path: Path) -> None:
    check = verify_host(tmp_path, "example.org:1965", FIRST, T0)
    assert check.is_new is True
    assert check.first_seen == "2026-03-01T09:00:00+00:00"
    saved = json.loads((tmp_path / KNOWN_HOSTS_FILE).read_text())
    assert saved == {
        "example.org:1965": {
            "fingerprint": FIRST,
            "first_seen": "2026-03-01T09:00:00+00:00",
            "last_seen": "2026-03-01T09:00:00+00:00",
        }
    }


def test_a_later_visit_with_the_same_certificate_keeps_first_seen_and_moves_last_seen(
    tmp_path: Path,
) -> None:
    verify_host(tmp_path, "example.org:1965", FIRST, T0)
    check = verify_host(tmp_path, "example.org:1965", FIRST, T0 + timedelta(days=3))
    assert check.is_new is False
    assert check.first_seen == "2026-03-01T09:00:00+00:00"
    saved = json.loads((tmp_path / KNOWN_HOSTS_FILE).read_text())["example.org:1965"]
    assert saved["first_seen"] == "2026-03-01T09:00:00+00:00"
    assert saved["last_seen"] == "2026-03-04T09:00:00+00:00"


def test_a_different_certificate_is_refused_and_nothing_is_overwritten(
    tmp_path: Path,
) -> None:
    verify_host(tmp_path, "example.org:1965", FIRST, T0)
    before = (tmp_path / KNOWN_HOSTS_FILE).read_text()
    with pytest.raises(CertificateChangedError) as raised:
        verify_host(tmp_path, "example.org:1965", SECOND, T0 + timedelta(days=1))
    error = raised.value
    assert (error.host_key, error.previous_fingerprint, error.new_fingerprint) == (
        "example.org:1965",
        FIRST,
        SECOND,
    )
    assert error.first_seen == "2026-03-01T09:00:00+00:00"
    assert "2026-03-01" in error.detail
    assert (tmp_path / KNOWN_HOSTS_FILE).read_text() == before


def test_each_host_and_port_is_remembered_separately(tmp_path: Path) -> None:
    verify_host(tmp_path, "example.org:1965", FIRST, T0)
    assert verify_host(tmp_path, "example.org:1966", SECOND, T0).is_new is True
    assert verify_host(tmp_path, "other.net:1965", SECOND, T0).is_new is True
    assert verify_host(tmp_path, "example.org:1965", FIRST, T0).is_new is False


def test_trusting_a_host_replaces_its_certificate_and_the_new_one_then_matches(
    tmp_path: Path,
) -> None:
    verify_host(tmp_path, "example.org:1965", FIRST, T0)
    trust_host(tmp_path, "example.org:1965", SECOND, T0 + timedelta(days=9))
    check = verify_host(tmp_path, "example.org:1965", SECOND, T0 + timedelta(days=10))
    assert check.is_new is False
    assert check.first_seen == "2026-03-10T09:00:00+00:00"
    with pytest.raises(CertificateChangedError):
        verify_host(tmp_path, "example.org:1965", FIRST, T0)


def test_trusting_a_host_never_seen_saves_it(tmp_path: Path) -> None:
    trust_host(tmp_path, "new.example:1965", FIRST, T0)
    assert verify_host(tmp_path, "new.example:1965", FIRST, T0).is_new is False


@pytest.mark.parametrize(
    "damaged", ["", "not json", "[1, 2]", '{"x": {"fingerprint": 1}}']
)
def test_a_damaged_known_hosts_file_reads_as_empty_and_is_replaced_on_the_next_visit(
    tmp_path: Path, damaged: str
) -> None:
    (tmp_path / KNOWN_HOSTS_FILE).write_text(damaged)
    assert verify_host(tmp_path, "example.org:1965", FIRST, T0).is_new is True
    assert FIRST in (tmp_path / KNOWN_HOSTS_FILE).read_text()


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    verify_host(tmp_path, "example.org:1965", FIRST, T0)
    record_visit(tmp_path, "gemini://example.org/", "Home", T0)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [KNOWN_HOSTS_FILE, VISITS_FILE]
    )


def test_the_data_directory_is_created_when_it_does_not_exist(tmp_path: Path) -> None:
    directory = tmp_path / "not" / "yet"
    record_visit(directory, "gemini://example.org/", "Home", T0)
    assert recent_visits(directory, 5)[0].title == "Home"


def test_there_are_no_visits_before_the_first_one(tmp_path: Path) -> None:
    assert recent_visits(tmp_path, 8) == []


def test_visits_come_back_newest_first(tmp_path: Path) -> None:
    for number in range(3):
        record_visit(
            tmp_path,
            f"gemini://example.org/{number}",
            f"Page {number}",
            T0 + timedelta(minutes=number),
        )
    assert [visit.title for visit in recent_visits(tmp_path, 10)] == [
        "Page 2",
        "Page 1",
        "Page 0",
    ]


def test_opening_a_page_again_moves_it_to_the_front_without_a_duplicate(
    tmp_path: Path,
) -> None:
    record_visit(tmp_path, "gemini://example.org/a", "A", T0)
    record_visit(tmp_path, "gemini://example.org/b", "B", T0 + timedelta(minutes=1))
    record_visit(
        tmp_path, "gemini://example.org/a", "A renamed", T0 + timedelta(minutes=2)
    )
    visits = recent_visits(tmp_path, 10)
    assert [(visit.url, visit.title) for visit in visits] == [
        ("gemini://example.org/a", "A renamed"),
        ("gemini://example.org/b", "B"),
    ]
    assert visits[0].visited_at == "2026-03-01T09:02:00+00:00"


def test_the_limit_on_recent_visits_trims_the_answer_but_not_the_store(
    tmp_path: Path,
) -> None:
    for number in range(5):
        record_visit(tmp_path, f"gemini://example.org/{number}", str(number), T0)
    assert [visit.title for visit in recent_visits(tmp_path, 2)] == ["4", "3"]
    assert len(recent_visits(tmp_path, 100)) == 5


def test_the_visit_list_is_capped_and_the_oldest_visits_are_the_ones_dropped(
    tmp_path: Path,
) -> None:
    total = MAX_VISITS + 7
    for number in range(total):
        record_visit(
            tmp_path,
            f"gemini://example.org/{number}",
            str(number),
            T0 + timedelta(seconds=number),
        )
    stored = json.loads((tmp_path / VISITS_FILE).read_text())
    assert len(stored) == MAX_VISITS
    assert stored[0]["title"] == str(total - 1)
    assert stored[-1]["title"] == str(total - MAX_VISITS)
    assert all(entry["url"] != "gemini://example.org/0" for entry in stored)


def test_a_damaged_history_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / VISITS_FILE).write_text("{broken")
    assert recent_visits(tmp_path, 8) == []
    record_visit(tmp_path, "gemini://example.org/", "Home", T0)
    assert [visit.title for visit in recent_visits(tmp_path, 8)] == ["Home"]


def test_visits_recorded_from_several_threads_are_all_kept(tmp_path: Path) -> None:
    def record(number: int) -> None:
        record_visit(tmp_path, f"gemini://example.org/{number}", str(number), T0)

    threads = [threading.Thread(target=record, args=(number,)) for number in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(int(visit.title) for visit in recent_visits(tmp_path, 100)) == list(
        range(20)
    )
