"""발화 기억이 재시작을 넘겨 살아남는지.

주 단위 쿨다운은 기억이 프로세스보다 오래 살아야만 성립한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.brain.gate import Gate
from src.core.models import Insight, Severity
from src.storage.speech import SQLiteSpeechLog

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def test_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "jarvis.db"
    SQLiteSpeechLog(db).record("chronic_short_sleep", NOW, "요즘 잠이 부족해요")

    # 프로세스가 죽고 다시 뜬 상황 — 새 인스턴스가 같은 파일을 연다.
    reborn = SQLiteSpeechLog(db)
    last = reborn.last("chronic_short_sleep")
    assert last is not None
    assert last.text == "요즘 잠이 부족해요"
    assert last.at == NOW


def test_cooldown_holds_across_restart(tmp_path: Path) -> None:
    """이게 이 클래스가 존재하는 이유다."""
    db = tmp_path / "jarvis.db"
    insight = Insight(
        trigger="chronic_short_sleep",
        summary="평균 3.9시간",
        severity=Severity.URGENT,
        at=NOW,
    )
    gate = Gate(
        log=SQLiteSpeechLog(db),
        cooldown_overrides={"chronic_short_sleep": timedelta(days=7)},
    )
    assert gate.allows(insight, NOW)
    gate.log.record("chronic_short_sleep", NOW, "말함")

    restarted = Gate(
        log=SQLiteSpeechLog(db),
        cooldown_overrides={"chronic_short_sleep": timedelta(days=7)},
    )
    assert not restarted.allows(insight, NOW + timedelta(days=2))
    assert restarted.allows(insight, NOW + timedelta(days=8))


def test_last_is_the_latest_not_the_newest_row(tmp_path: Path) -> None:
    """뒤늦게 들어온 과거 기록이 "마지막 발화"를 밀어내면 안 된다."""
    log = SQLiteSpeechLog(tmp_path / "jarvis.db")
    log.record("t", NOW, "최근")
    log.record("t", NOW - timedelta(days=3), "과거")
    last = log.last("t")
    assert last is not None and last.text == "최근"


def test_since_returns_in_time_order(tmp_path: Path) -> None:
    log = SQLiteSpeechLog(tmp_path / "jarvis.db")
    log.record("b", NOW, "나중")
    log.record("a", NOW - timedelta(hours=1), "먼저")
    log.record("c", NOW - timedelta(days=5), "범위 밖")
    found = log.since(NOW - timedelta(hours=6))
    assert [r.text for r in found] == ["먼저", "나중"]
