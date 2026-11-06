"""문제를 일으킨 사이트 기억(30일) — 적기, 잊기, 쓰러진 자리 되찾기."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from src.brain.badsites import KEEP, BadSites

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _sites(tmp_path: Path, clock: List[datetime]) -> BadSites:
    return BadSites(tmp_path / "data" / "browser_bad_sites.json", now=lambda: clock[0])


def test_a_site_is_remembered_for_thirty_days(tmp_path: Path) -> None:
    clock = [NOW]
    bad = _sites(tmp_path, clock)
    assert bad.why("evil.example") is None
    bad.add("Evil.Example.", "웹소켓을 쏟아냈다")
    assert bad.why("evil.example") == "2026-11-06에 웹소켓을 쏟아냈다"
    assert bad.hosts() == frozenset({"evil.example"})
    assert BadSites(bad.path, now=lambda: clock[0]).why("EVIL.example") is not None  # 파일에 남는다
    clock[0] = NOW + timedelta(days=30, minutes=1)
    assert bad.why("evil.example") is None and bad.hosts() == frozenset()


def test_falling_over_while_looking_blames_that_site_on_the_next_start(tmp_path: Path) -> None:
    """보는 동안 표시를 남기고 깨끗이 끝나면 지운다 — 남아 있으면 그 사이트를 보다 쓰러진 것이다."""
    clock = [NOW]
    bad = _sites(tmp_path, clock)
    bad.visiting("fine.example")
    bad.left()
    assert not bad.marker.exists() and bad.recover() is None
    bad.visiting("fine.example")
    bad.visiting("heavy.example")  # 손이 옮겨 갔다 — 마지막 사이트가 남는다
    assert bad.marker.read_text() == "heavy.example"
    # 여기서 자비스가 쓰러졌다(left 없음) — 다시 켜진다.
    again = _sites(tmp_path, clock)
    assert again.recover() == "heavy.example"
    assert again.why("heavy.example") == "2026-11-06에 자비스가 이 사이트를 보다 쓰러졌다"
    assert again.why("fine.example") is None and not again.marker.exists()
    assert again.recover() is None


def test_a_broken_list_is_said_and_read_as_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [NOW]
    bad = _sites(tmp_path, clock)
    bad.path.parent.mkdir(parents=True)
    bad.path.write_text("{깨짐")
    assert bad.why("evil.example") is None and "못 읽었다" in caplog.text
    bad.add("evil.example", "탭이 죽었다")  # 깨진 파일 위에 다시 쓴다
    assert json.loads(bad.path.read_text())["evil.example"]["why"] == "탭이 죽었다"


def test_the_list_keeps_only_the_newest(tmp_path: Path) -> None:
    clock = [NOW]
    bad = _sites(tmp_path, clock)
    data = {
        f"s{i}.example": {"at": (NOW - timedelta(minutes=i)).isoformat(), "why": "x"}
        for i in range(KEEP)
    }
    bad.path.parent.mkdir(parents=True)
    bad.path.write_text(json.dumps(data))
    bad.add("new.example", "y")
    hosts = bad.hosts()
    assert len(hosts) == KEEP and "new.example" in hosts and f"s{KEEP - 1}.example" not in hosts
