"""자기 점검 저장소 — 같은 DB 의 표, 앞부분만, 기간으로 읽기."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.storage.selfchecks import ANSWER, LINE, QUESTION, SelfCheck, SQLiteSelfChecks

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)


def _check(at: datetime, question: str = "q") -> SelfCheck:
    return SelfCheck(
        at=at,
        question=question,
        answer="a" * 5000,
        tools=(("browse", "https://x.example/" + "p" * 500, True, "320자"),),
        result="일부",
        blocked="브라우저로도 못 찾음",
        missing="m" * 500,
        note="n",
    )


def test_checks_come_back_in_order_within_the_window(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    store.add(_check(NOW - timedelta(days=20), "옛것"))
    store.add(_check(NOW - timedelta(hours=1), "둘째"))
    # 시간대가 다른 표기로 와도 같은 순간으로 비교한다(stamp).
    store.add(_check((NOW - timedelta(hours=2)).astimezone(timezone(timedelta(hours=9))), "첫째"))
    rows = SQLiteSelfChecks(tmp_path / "t.db").since(NOW - timedelta(days=14))
    assert [r.question for r in rows] == ["첫째", "둘째"]
    assert rows[0].at == NOW - timedelta(hours=2)


def test_only_the_front_of_long_values_is_kept(tmp_path: Path) -> None:
    store = SQLiteSelfChecks(tmp_path / "t.db")
    store.add(_check(NOW, "q" * 1000))
    [row] = store.since(NOW - timedelta(days=1))
    assert len(row.question) == QUESTION and len(row.answer) == ANSWER
    assert len(row.missing) == LINE and len(row.tools[0][1]) == LINE
    assert row.tools[0][0] == "browse" and row.tools[0][2] is True
