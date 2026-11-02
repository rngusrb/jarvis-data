from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.brain.gate import Gate
from src.core.models import Insight, Severity

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)


def _insight(severity: Severity, trigger: str = "sleep_drop") -> Insight:
    return Insight(trigger=trigger, summary="테스트", severity=severity, at=NOW)


def test_사소한_신호는_LLM까지_가지_않는다() -> None:
    assert Gate().allows(_insight(Severity.INFO), NOW) is False


def test_주목할만한_신호는_통과한다() -> None:
    assert Gate().allows(_insight(Severity.NOTABLE), NOW) is True


def test_쿨다운_안에는_다시_말하지_않는다() -> None:
    gate = Gate()
    insight = _insight(Severity.URGENT)
    gate.log.record(insight.trigger, NOW, "어젯밤 잘 못 잤네")
    assert gate.allows(insight, NOW + timedelta(hours=1)) is False


def test_쿨다운이_지나면_다시_말할_수_있다() -> None:
    gate = Gate()
    insight = _insight(Severity.URGENT)
    gate.log.record(insight.trigger, NOW, "어젯밤 잘 못 잤네")
    assert gate.allows(insight, NOW + timedelta(hours=7)) is True


def test_쿨다운은_트리거별로_따로_돈다() -> None:
    gate = Gate()
    gate.log.record("sleep_drop", NOW, "어젯밤 잘 못 잤네")
    other = _insight(Severity.URGENT, "heart_rate_spike")
    assert gate.allows(other, NOW + timedelta(minutes=1)) is True


def test_트리거별로_쿨다운을_다르게_줄_수_있다() -> None:
    """수집 중단처럼 상태가 계속 유지되는 신호는 기본 쿨다운이면 하루 네 번 울린다."""
    gate = Gate(cooldown_overrides={"collection": timedelta(days=1)})
    insight = _insight(Severity.URGENT, "collection")
    gate.log.record(insight.trigger, NOW, "데이터가 안 들어와")

    # 기본 쿨다운(6시간)이었다면 통과했을 시점
    assert gate.allows(insight, NOW + timedelta(hours=7)) is False
    assert gate.allows(insight, NOW + timedelta(hours=25)) is True


def test_오버라이드가_없는_트리거는_기본값을_쓴다() -> None:
    gate = Gate(cooldown_overrides={"collection": timedelta(days=1)})
    insight = _insight(Severity.URGENT, "sleep_drop")
    gate.log.record(insight.trigger, NOW, "어젯밤 잘 못 잤네")
    assert gate.allows(insight, NOW + timedelta(hours=7)) is True


def test_같은_신호는_쿨다운이_지나도_다시_묻지_않는다() -> None:
    # 같은 밤은 시간이 지나도 같은 밤이다. 쿨다운만 보면 아침에 들어온 밤을 6시간마다 세 번
    # 물었다(JAR-018 리뷰 재현).
    gate = Gate()
    insight = _insight(Severity.NOTABLE)
    gate.log.record(insight.trigger, NOW, "말 안 걸기로 함", spoken=False, about=insight.at)
    assert gate.allows(insight, NOW + timedelta(hours=7)) is False
    assert gate.allows(insight, NOW + timedelta(days=3)) is False


def test_다른_밤이면_쿨다운만_본다() -> None:
    gate = Gate()
    gate.log.record("sleep_drop", NOW, "어젯밤 잘 못 잤네", about=NOW - timedelta(days=1))
    tonight = _insight(Severity.NOTABLE)
    assert gate.allows(tonight, NOW + timedelta(hours=1)) is False
    assert gate.allows(tonight, NOW + timedelta(hours=7)) is True


def test_무엇에_대한_판단인지_모르는_옛_기록은_쿨다운만_본다() -> None:
    gate = Gate()
    insight = _insight(Severity.NOTABLE)
    gate.log.record(insight.trigger, NOW, "어젯밤 잘 못 잤네")
    assert gate.allows(insight, NOW + timedelta(hours=7)) is True
