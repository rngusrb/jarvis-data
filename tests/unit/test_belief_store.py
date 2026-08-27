"""믿음 저장소와 수명주기.

이 파일이 지키는 건 하나다 — **늘어나는 만큼 줄어드는가.**
생성만 있고 회수가 없으면 몇 달 뒤 3년 전 관심사가 프롬프트에 끼어든다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.beliefs import FADE_AFTER, FORGET_AFTER, Belief, Status
from src.storage.beliefs import MAX_EVIDENCE, SQLiteBeliefStore

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


DEFAULT_EVIDENCE = ("전세대출 금리", "버팀목 자격")


def _belief(kind: str = "관심사:주거", evidence: tuple = DEFAULT_EVIDENCE) -> Belief:
    return Belief(
        kind=kind,
        value="전세자금대출을 알아보는 중",
        confidence=0.8,
        first_seen=NOW,
        last_seen=NOW,
        evidence=evidence,
    )


def test_evidence_is_mandatory() -> None:
    """근거 없는 믿음은 검증할 수 없다. 저장소가 아니라 모델에서 막는다."""
    with pytest.raises(ValueError, match="근거 없는"):
        Belief(
            kind="관심사:주거",
            value="추측",
            confidence=0.9,
            first_seen=NOW,
            last_seen=NOW,
            evidence=(),
        )


def test_first_sighting_is_only_a_candidate(tmp_path: Path) -> None:
    """한 번 검색해본 것까지 관심사라고 하면 스쳐간 호기심을 붙들게 된다."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    stored = store.observe(_belief(), NOW)
    assert stored.status is Status.CANDIDATE
    assert store.active(NOW) == []


def test_second_sighting_confirms(tmp_path: Path) -> None:
    """반복이 확정을 만든다. 이 승격이 저장소의 핵심이다."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    store.observe(_belief(), NOW)
    later = NOW + timedelta(days=2)
    stored = store.observe(_belief(evidence=("전세 계약 특약",)), later)

    assert stored.status is Status.CONFIRMED
    assert stored.first_seen == NOW
    assert stored.last_seen == later
    assert [b.kind for b in store.active(later)] == ["관심사:주거"]


def test_evidence_merges_without_duplicates(tmp_path: Path) -> None:
    """같은 검색어를 다섯 번 했다고 근거가 다섯 개가 되면 확신이 부풀려진다."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    store.observe(_belief(evidence=("전세대출 금리",)), NOW)
    stored = store.observe(_belief(evidence=("전세대출 금리", "새 근거")), NOW + timedelta(days=1))
    assert stored.evidence == ("전세대출 금리", "새 근거")


def test_evidence_is_capped(tmp_path: Path) -> None:
    """근거를 무한정 쌓으면 프롬프트가 터진다."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    for i in range(MAX_EVIDENCE + 8):
        store.observe(_belief(evidence=(f"근거 {i}",)), NOW + timedelta(days=i))
    stored = store.get("관심사:주거")
    assert stored is not None and len(stored.evidence) == MAX_EVIDENCE
    # 잘린 건 오래된 쪽이어야 한다.
    assert stored.evidence[-1] == f"근거 {MAX_EVIDENCE + 7}"


def test_confirmed_belief_fades_when_quiet(tmp_path: Path) -> None:
    """관심사는 해소되면 흔적이 끊긴다. 이사를 가버리면 전세 검색이 멈춘다."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    store.observe(_belief(), NOW)
    store.observe(_belief(), NOW + timedelta(days=1))

    quiet = NOW + timedelta(days=1) + FADE_AFTER
    assert store.active(quiet) == []
    stored = store.get("관심사:주거")
    assert stored is not None and stored.aged(quiet) is Status.FADING


def test_forget_stale_removes_the_truly_dead(tmp_path: Path) -> None:
    store = SQLiteBeliefStore(tmp_path / "t.db")
    store.observe(_belief(), NOW)
    store.observe(_belief(kind="관심사:야구"), NOW)

    barely = NOW + FADE_AFTER
    assert store.forget_stale(barely) == []

    long_gone = NOW + FORGET_AFTER
    assert sorted(store.forget_stale(long_gone)) == ["관심사:야구", "관심사:주거"]
    assert store.all() == []


def test_value_grows_with_new_evidence(tmp_path: Path) -> None:
    """관심사는 자란다 — "알아보는 중"에서 "계약 직전"으로."""
    store = SQLiteBeliefStore(tmp_path / "t.db")
    store.observe(_belief(), NOW)
    grown = Belief(
        kind="관심사:주거",
        value="전세 계약 직전",
        confidence=0.9,
        first_seen=NOW,
        last_seen=NOW,
        evidence=("전세 특약사항",),
    )
    stored = store.observe(grown, NOW + timedelta(days=3))
    assert stored.value == "전세 계약 직전"
    assert stored.confidence == 0.9
