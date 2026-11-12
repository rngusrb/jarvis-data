"""수집 감시.

지표마다 따로 경고하던 것을 하나로 합치고, 원인을 짚게 했다. 지키는 것은 넷이다.

- 같은 기기에서 오는 것이 비슷한 때 전부 끊기면 연결을 의심한다 (10월의 실제 상황)
- 일부만 끊기면 그 수집 경로를 짚는다
- 오래될수록 조용해지지 않는다 (9월 13일 사고)
- 한 번도 안 들어온 건 잔소리하지 않는다
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.models import Severity
from src.triggers.collection import CollectionWatch, Watched

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 15, 7, 55, tzinfo=KST)

DAY_AND_HALF = timedelta(hours=36)
MORNING = "아이폰 단축어(기상)"
SLEEP = Watched("sleep_hours", "수면", MORNING, "아이폰", DAY_AND_HALF)
HEART = Watched("resting_heart_rate", "휴식기 심박", MORNING, "아이폰", DAY_AND_HALF)
PLACE = Watched("location", "위치", "아이폰 '떠날 때' 자동화", "아이폰", DAY_AND_HALF)
WEB = Watched("web_visit", "웹 방문", "mac_chrome", "맥", timedelta(days=2))
HINTS = {"아이폰": "아이폰에서 Tailscale이 켜져 있는지"}


def _watch() -> CollectionWatch:
    return CollectionWatch(sources=[SLEEP, HEART, PLACE, WEB], hints=HINTS)


def test_다_들어오고_있으면_조용하다() -> None:
    seen = {s.kind: NOW - timedelta(hours=3) for s in (SLEEP, HEART, PLACE, WEB)}
    assert _watch().check(seen, NOW) is None


def test_아이폰_것이_한꺼번에_끊기면_연결을_의심한다() -> None:
    """10월의 실제 상황. 자비스는 매일 "자동화 설정을 확인해"라고 했지만
    원인은 폰의 Tailscale이었다."""
    seen = {
        "sleep_hours": datetime(2026, 10, 2, 0, 0, tzinfo=KST),
        "resting_heart_rate": datetime(2026, 10, 2, 0, 0, tzinfo=KST),
        "location": datetime(2026, 10, 2, 18, 25, tzinfo=KST),
        "web_visit": NOW - timedelta(hours=2),
    }
    insight = _watch().check(seen, NOW)
    assert insight is not None
    assert insight.trigger == "collection"
    assert "연결 문제" in insight.summary
    assert "Tailscale" in insight.summary
    for label in ("수면", "휴식기 심박", "위치"):
        assert label in insight.summary
    assert "맥" not in insight.summary, "멀쩡한 기기는 말하지 않는다"
    assert insight.severity is Severity.URGENT


def test_끊김_기준이_긴_종류는_끊긴_뒤에_들어온_것만_연결의_근거다() -> None:
    """결제(끊김 14일)는 폰 연결이 끊겨도 한동안 "정상"이다. 그걸 근거로 "연결 문제는 아니다"라고
    하면 10월 사고의 틀린 조언이 되풀이된다(JAR-045 2차 리뷰 재현)."""
    pay = Watched(
        "card_sms", "카드 승인 문자", "아이폰 메시지 자동화", "아이폰", timedelta(days=14)
    )
    watch = CollectionWatch(sources=[SLEEP, HEART, PLACE, WEB, pay], hints=HINTS)
    seen = {
        "sleep_hours": datetime(2026, 10, 10, 0, 0, tzinfo=KST),
        "resting_heart_rate": datetime(2026, 10, 10, 0, 0, tzinfo=KST),
        "location": datetime(2026, 10, 10, 8, 0, tzinfo=KST),
        "card_sms": datetime(2026, 10, 9, 19, 0, tzinfo=KST),  # 끊기기 전에 온 것
        "web_visit": NOW - timedelta(hours=2),
    }
    insight = watch.check(seen, NOW)
    assert insight is not None and "연결 문제일 가능성이 크다" in insight.summary
    assert "정상이라" not in insight.summary
    # 끊긴 뒤에도 결제가 들어왔다면 연결은 산다 — 그 수집 경로들을 짚는다
    seen["card_sms"] = datetime(2026, 10, 14, 12, 0, tzinfo=KST)
    alive = watch.check(seen, NOW)
    assert alive is not None and "정상이라 연결 문제는 아니다" in alive.summary


def test_하나만_끊기면_그_수집_경로를_짚는다() -> None:
    seen = {
        "sleep_hours": NOW - timedelta(hours=10),
        "resting_heart_rate": NOW - timedelta(hours=10),
        "location": NOW - timedelta(hours=50),
    }
    insight = _watch().check(seen, NOW)
    assert insight is not None
    assert "연결 문제는 아니다" in insight.summary
    assert "떠날 때" in insight.summary
    assert insight.severity is Severity.NOTABLE


def test_따로따로_끊겼으면_연결이라고_단정하지_않는다() -> None:
    """수면은 워치를 안 차서 열흘 전에, 위치는 어제 끊겼다면 원인이 다르다."""
    seen = {
        "sleep_hours": NOW - timedelta(days=10),
        "resting_heart_rate": NOW - timedelta(days=10),
        "location": NOW - timedelta(days=2),
    }
    insight = _watch().check(seen, NOW)
    assert insight is not None
    assert "연결 문제일" not in insight.summary
    assert "하나씩 확인" in insight.summary


def test_웹_기록도_감시한다() -> None:
    seen = {s.kind: NOW - timedelta(hours=3) for s in (SLEEP, HEART, PLACE)}
    seen["web_visit"] = NOW - timedelta(days=3)
    insight = _watch().check(seen, NOW)
    assert insight is not None and "웹 방문" in insight.summary


def test_오래될수록_조용해지지_않는다() -> None:
    """2026-09-13 사고 재현. 14일 창을 쓰던 감시가 그날부터 침묵했다."""
    last = datetime(2026, 8, 29, 10, 24, tzinfo=KST)
    for days in (2, 14, 15, 30, 365):
        seen = {"location": last}
        insight = _watch().check(seen, last + timedelta(days=days))
        assert insight is not None, f"{days}일째에 침묵했다"


def test_한_번도_안_들어온_건_잔소리하지_않는다() -> None:
    """크롬 수집기를 아직 설치하지 않았다고 매일 경고하면 안 된다."""
    seen = {s.kind: NOW - timedelta(hours=3) for s in (SLEEP, HEART, PLACE)}
    assert _watch().check(seen, NOW) is None


def test_여러_개가_끊겨도_말은_하나다() -> None:
    seen = {s.kind: NOW - timedelta(days=5) for s in (SLEEP, HEART, PLACE, WEB)}
    insight = _watch().check(seen, NOW)
    assert insight is not None
    assert insight.summary.count("- 아이폰") == 1
    assert insight.summary.count("- 맥") == 1


def test_조사를_받침에_맞게_붙인다() -> None:
    """ "위치은"처럼 틀린 문장이 LLM에게 가면 그대로 따라 쓴다."""
    seen = {
        "sleep_hours": NOW - timedelta(hours=8),
        "resting_heart_rate": NOW - timedelta(hours=8),
        "location": NOW - timedelta(hours=60),
    }
    insight = _watch().check(seen, NOW)
    assert insight is not None
    assert "위치는" in insight.summary and "위치은" not in insight.summary
    assert "휴식기 심박은 정상" in insight.summary
