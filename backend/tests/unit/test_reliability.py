"""Phase 9 (local reliability): retries, DST-safe cutoffs, backup rotation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from fantasy_rag.ingest.retry import Retrying, retry, transient
from fantasy_rag.jobs import backup
from fantasy_rag.jobs.backup import complete_dumps, rotate
from fantasy_rag.ml.features import ET, cutoffs

PT = ZoneInfo("America/Los_Angeles")


def _status(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


def test_only_transient_errors_are_retried() -> None:
    assert transient(_status(503)) and transient(_status(429))
    assert transient(httpx.ConnectTimeout("t")) and transient(httpx.ConnectError("c"))
    assert not transient(_status(404)) and not transient(ValueError("bad data"))
    calls, waits = [], []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow")
        return "ok"

    assert retry(flaky, sleep=waits.append) == "ok" and len(calls) == 3
    assert len(waits) == 2 and waits[1] > waits[0]  # backoff grows
    with pytest.raises(httpx.HTTPStatusError):  # a 404 fails at once, no retries
        retry(lambda: (_ for _ in ()).throw(_status(404)), sleep=waits.append)
    assert len(waits) == 2


def test_loader_wrapper_retries_only_load_calls() -> None:
    class Module:
        version = "1"
        n = 0

        def load_x(self) -> int:
            Module.n += 1
            if Module.n == 1:
                raise httpx.ConnectError("blip")
            return 7

    wrapped = Retrying(Module())
    assert wrapped.version == "1"
    import fantasy_rag.ingest.retry as r

    r.BASE_DELAY, old = 0.0, r.BASE_DELAY
    try:
        assert wrapped.load_x() == 7 and Module.n == 2
    finally:
        r.BASE_DELAY = old


def test_cutoffs_stay_tuesday_noon_eastern_across_the_dst_change() -> None:
    # Week 8 (EDT) and week 9 (after DST ends Nov 1, 2026): Thursday night openers.
    sched = [{"season": 2026, "week": 8, "game_type": "REG",
              "kickoff_at": datetime(2026, 10, 29, 20, 15, tzinfo=ET)},
             {"season": 2026, "week": 9, "game_type": "REG",
              "kickoff_at": datetime(2026, 11, 5, 20, 15, tzinfo=ET)}]  # fmt: skip
    cut = cutoffs(sched)
    for week in (8, 9):
        local = cut[(2026, week)].astimezone(ET)
        assert (local.strftime("%a"), local.hour, local.minute) == ("Tue", 12, 0)
        # The 07:30 Pacific task and the 08:30 Pacific check both land before the deadline.
        tuesday = local.date()
        run = datetime(tuesday.year, tuesday.month, tuesday.day, 7, 30, tzinfo=PT)
        assert run < cut[(2026, week)] and run + timedelta(hours=1) < cut[(2026, week)]
    assert cut[(2026, 9)] - cut[(2026, 8)] == timedelta(days=7, hours=1)  # the extra DST hour


def test_backup_rotation_keeps_7_recent_and_4_older_weeks(tmp_path: Path) -> None:
    start = datetime(2026, 9, 1, 19, tzinfo=UTC)
    for day in range(40):  # one dump a day
        stamp = f"{start + timedelta(days=day):%Y%m%dT%H%M%SZ}"
        for ext in (".dump", ".roles.sql", ".manifest.json"):
            (tmp_path / f"fantasy_{stamp}{ext}").write_text("x")
    # Newer unfinished attempts (no manifest) are neither counted nor removed.
    for day in (41, 42):
        stamp = f"{start + timedelta(days=day):%Y%m%dT%H%M%SZ}"
        (tmp_path / f"fantasy_{stamp}.dump").write_text("x")
    rotate(tmp_path)
    kept = [p.name for p in complete_dumps(tmp_path)]
    assert len(kept) == 7 + 4 and len(list(tmp_path.glob("*.dump"))) == 7 + 4 + 2
    assert kept[-1].startswith("fantasy_20261010")  # newest kept
    # Companions go with their dump.
    assert len(list(tmp_path.glob("*.manifest.json"))) == len(kept)


def test_a_backup_that_fails_midway_leaves_only_complete_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "fantasy_20261001T190000Z.dump"
    for f in backup._bundle(old):
        f.write_text("old")
    (tmp_path / "fantasy_20261002T190000Z.dump").write_text("unfinished")  # no manifest

    def compose(*args: str, stdin: Any = None, stdout: Any = None) -> Any:
        if hasattr(stdout, "write"):
            stdout.write(b"dump")
        return SimpleNamespace(stdout=b"roles")

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(
        backup,
        "Settings",
        lambda: SimpleNamespace(database_url="postgresql://u:p@127.0.0.1:5433/d"),
    )
    monkeypatch.setattr(backup.psycopg, "connect", MagicMock())
    monkeypatch.setattr(backup, "manifest", lambda conn: {})
    monkeypatch.setattr(backup, "_compose", compose)

    def disk_full(*a: Any, **k: Any) -> str:
        raise OSError(28, "No space left on device")

    before = sorted(p.name for p in tmp_path.iterdir())
    with monkeypatch.context() as m:
        m.setattr(backup.json, "dumps", disk_full)  # the manifest write fails
        with pytest.raises(OSError):
            backup.run()
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing new, nothing lost
    assert complete_dumps(tmp_path) == [old]  # the unfinished dump is never selected

    new = backup.run()
    assert complete_dumps(tmp_path) == [old, new]
    assert not list(tmp_path.glob(".*.partial"))
