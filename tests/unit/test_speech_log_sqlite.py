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


def test_skip_survives_restart_and_holds_the_cooldown(tmp_path: Path) -> None:
    """SKIP 판정도 재시작을 넘겨야 쿨다운이 유지된다."""
    db = tmp_path / "jarvis.db"
    SQLiteSpeechLog(db).record("collection", NOW, "말 안 걸기로 함", spoken=False)

    reborn = SQLiteSpeechLog(db)
    last = reborn.last("collection")
    assert last is not None
    assert last.spoken is False
    # 게이트는 이걸 보고 막아야 한다.
    insight = Insight(
        trigger="collection",
        summary="위치가 끊김",
        severity=Severity.URGENT,
        at=NOW,
    )
    gate = Gate(log=reborn, cooldown=timedelta(hours=6))
    assert not gate.allows(insight, NOW + timedelta(hours=1))
    assert gate.allows(insight, NOW + timedelta(hours=7))


def test_since_hides_skips(tmp_path: Path) -> None:
    """맥락 제공자는 실제로 한 말만 봐야 한다."""
    log = SQLiteSpeechLog(tmp_path / "jarvis.db")
    log.record("a", NOW - timedelta(hours=2), "실제로 한 말")
    log.record("b", NOW - timedelta(hours=1), "말 안 걸기로 함", spoken=False)
    assert [r.text for r in log.since(NOW - timedelta(hours=6))] == ["실제로 한 말"]


def test_old_db_without_spoken_column_is_migrated(tmp_path: Path) -> None:
    """이미 만들어진 DB에는 spoken 이 없다. 조용히 빼먹으면 게이트가 다시 샌다."""
    import sqlite3

    db = tmp_path / "jarvis.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE speech (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "trigger TEXT NOT NULL, at TEXT NOT NULL, text TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO speech (trigger, at, text) VALUES (?, ?, ?)",
        ("old", NOW.isoformat(), "옛 기록"),
    )
    conn.commit()
    conn.close()

    log = SQLiteSpeechLog(db)
    last = log.last("old")
    assert last is not None
    assert last.text == "옛 기록"
    # 옛 기록은 실제로 한 말로 본다 — 그때는 SKIP 을 저장하지 않았다.
    assert last.spoken is True


def test_시간대가_다른_기록도_순간으로_고른다(tmp_path: Path) -> None:
    """리뷰 재현: 한국 시각으로 남긴 한 줄이 섞이면 글자 비교로는 더 늦은 줄로 보인다.

    10:00+09:00(= 01:00Z) 뒤에 02:00Z 를 남겼는데 last() 가 앞 줄을 골라, 6시간 쿨다운이
    실제 마지막 말 5.5시간 만에 풀렸다.
    """
    log = SQLiteSpeechLog(tmp_path / "t.db")
    kst = timezone(timedelta(hours=9))
    log.record("collection", datetime(2026, 10, 30, 10, 0, tzinfo=kst), "먼저")
    log.record("collection", datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc), "나중")

    last = log.last("collection")
    assert last is not None and last.text == "나중"
    assert [r.text for r in log.since(datetime(2026, 10, 30, 1, 30, tzinfo=timezone.utc))] == [
        "나중"
    ]


def test_한국_시각으로_물어도_같은_순간부터_돌려준다(tmp_path: Path) -> None:
    log = SQLiteSpeechLog(tmp_path / "t.db")
    log.record("collection", datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc), "나중")

    # 한국 10:30 = UTC 01:30. 글자로 묻으면 "…T10:30+09:00" 이 "…T02:00+00:00" 보다 뒤라 빠졌다.
    since = log.since(datetime(2026, 10, 30, 10, 30, tzinfo=timezone(timedelta(hours=9))))

    assert [r.text for r in since] == ["나중"]


def test_무엇에_대한_판단인지도_재시작을_넘긴다(tmp_path: Path) -> None:
    db = tmp_path / "jarvis.db"
    night = datetime(2026, 8, 27, tzinfo=timezone(timedelta(hours=9)))
    SQLiteSpeechLog(db).record("sleep_drop", NOW, "말 안 걸기로 함", spoken=False, about=night)

    last = SQLiteSpeechLog(db).last("sleep_drop")
    assert last is not None and last.about == night
    insight = Insight(trigger="sleep_drop", summary="짧게 잠", severity=Severity.NOTABLE, at=night)
    assert not Gate(log=SQLiteSpeechLog(db)).allows(insight, NOW + timedelta(hours=7))


def test_about_칸이_없는_DB도_옮겨진다(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "jarvis.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE speech (id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, "
        "at TEXT NOT NULL, text TEXT NOT NULL, spoken INTEGER NOT NULL DEFAULT 1);"
    )
    conn.execute(
        "INSERT INTO speech (trigger, at, text, spoken) VALUES (?, ?, ?, 0)",
        ("sleep_drop", NOW.isoformat(), "옛 판단"),
    )
    conn.commit()
    conn.close()

    log = SQLiteSpeechLog(db)
    old = log.last("sleep_drop")
    assert old is not None and old.about is None
    log.record("sleep_drop", NOW + timedelta(hours=1), "새 판단", spoken=False, about=NOW)
    new = log.last("sleep_drop")
    assert new is not None and new.about == NOW


def test_reactions_land_on_the_message_with_that_token(tmp_path: Path) -> None:
    """👍👎 버튼은 표를 들고 온다. 나중에 누른 게 이긴다."""
    log = SQLiteSpeechLog(tmp_path / "jarvis.db")
    log.record("topic:3", NOW, "요즘 자격증 보네", token="ab12cd34")
    log.record("late_sleep", NOW, "어젯밤 늦게 잤네", token="ffff0000")

    assert log.rate("ab12cd34", 1) is not None
    rated = log.rate("ab12cd34", -1)
    assert rated is not None and rated.trigger == "topic:3" and rated.feedback == -1
    assert log.rate("없는표", 1) is None
    assert [(r.trigger, r.feedback) for r in log.rated(NOW - timedelta(days=1))] == [
        ("topic:3", -1)
    ]


def test_judged_looks_at_every_record_and_family_at_spoken_ones(tmp_path: Path) -> None:
    log = SQLiteSpeechLog(tmp_path / "jarvis.db")
    first = NOW - timedelta(days=2)
    log.record("topic:1", NOW, "A", about=first)
    log.record("topic:1", NOW + timedelta(hours=1), "말 안 걸기로 함", spoken=False, about=NOW)
    log.record("topic:2", NOW + timedelta(hours=2), "말 안 걸기로 함", spoken=False, about=NOW)

    assert log.judged("topic:1", first) and log.judged("topic:1", NOW)
    assert not log.judged("topic:2", first)
    said = log.last_spoken_in("topic")
    assert said is not None and said.text == "A"


def test_an_old_table_gets_the_new_columns(tmp_path: Path) -> None:
    """이미 만들어진 DB 에는 표·반응 칸이 없다. 조용히 빼먹으면 버튼이 아무 일도 안 한다."""
    import sqlite3

    db = tmp_path / "jarvis.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE speech (id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL,"
        " at TEXT NOT NULL, text TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO speech (trigger, at, text) VALUES ('x', ?, '옛 말')", (NOW.isoformat(),)
    )
    conn.commit()
    conn.close()

    log = SQLiteSpeechLog(db)
    last = log.last("x")
    assert last is not None and last.spoken and last.token is None and last.feedback == 0
