"""쓴 돈 — 카드 승인 문자·애플페이를 읽고, 같은 결제를 하나로, 기간 합계를 코드가 센다(JAR-045).

문자 예시는 카드사 문자에 흔한 꼴을 본뜬 것이다 — **실제 문자로 확인하기 전**(2026-11-12). 사용자가
예시를 주면 그걸로 바꾼다. 이름은 가린 꼴(홍*동)만 쓴다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import pytest

from src.core.clock import KST
from src.core.models import Trace
from src.core.traces import TraceRegistry
from src.sectors.money import TRACES, SpendingProvider
from src.sectors.money.payments import (
    BY_SMS,
    BY_WALLET,
    SMS,
    WALLET,
    Payment,
    Reading,
    from_sms,
    from_wallet,
    merge,
    read,
    summarize,
)

NOW = datetime(2026, 11, 12, 13, 0, tzinfo=KST)  # 목요일

SAMSUNG = "[Web발신]\n삼성1234승인 홍*동\n4,500원 일시불\n11/12 08:31 스타벅스\n누적1,234,567원"
SHINHAN = "[Web발신]\n신한카드(1234)승인 홍*동 12,000원(일시불)11/11 19:02 이마트 누적1,250,000원"
KB = (
    "[Web발신]\nKB국민카드1234승인\n홍*동님\n11,000원 일시불\n11/12 12:10\n김밥천국"
    "\n누적1,261,000원"
)
CANCEL = "[Web발신]\n삼성1234승인취소 홍*동\n4,500원\n11/12 08:40 스타벅스"
DECLINED = "[Web발신]\n삼성1234승인거절 홍*동\n9,000원 잔액부족\n11/12 09:00 편의점"
NOTICE = "[Web발신]\n삼성카드 11월 결제예정금액 512,340원(11/25 출금)"
OVERSEAS = "[Web발신]\n신한카드(1234)승인 홍*동 USD 12.00 11/10 03:00 AMAZON"


def _sms(text: str, at: datetime = NOW) -> Trace:
    return Trace(source=SMS, kind=SMS, text=text, at=at)


def _wallet(at: datetime, meta: Dict[str, Any], text: str = "결제") -> Trace:
    return Trace(source="wallet", kind=WALLET, text=text, at=at, meta=meta)


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=KST)


def test_흔한_승인_문자_꼴을_읽는다() -> None:
    samsung = from_sms(_sms(SAMSUNG))
    assert samsung == Payment(at(12, 8, 31), 4500, "스타벅스", "삼성1234", BY_SMS)
    shinhan = from_sms(_sms(SHINHAN))
    assert shinhan is not None
    assert (shinhan.at, shinhan.amount, shinhan.merchant) == (at(11, 19, 2), 12000, "이마트")
    assert shinhan.card == "신한카드(1234)"
    kb = from_sms(_sms(KB))  # 가맹점이 때 다음 줄에 온다
    assert kb is not None and (kb.amount, kb.merchant) == (11000, "김밥천국")


def test_취소는_음수_거절과_안내는_결제가_아니다() -> None:
    canceled = from_sms(_sms(CANCEL))
    assert canceled is not None and canceled.amount == -4500 and canceled.card == "삼성1234"
    assert from_sms(_sms(DECLINED)) is None
    assert from_sms(_sms(NOTICE)) is None
    assert from_sms(_sms(OVERSEAS)) is None  # 원화 금액이 없다 — 짐작하지 않는다


@pytest.mark.parametrize(
    "text",
    [
        "승인 났어? 회비 30,000원 보냈어",  # 개인 문자 — 결제의 때(월/일 시:분)가 없다
        "[OO은행] 대출 승인 안내 11/12 10:00 대출금액 30,000,000원",
        "[Web발신]\n삼성1234 환불승인 12,000원\n11/12 10:00 온라인몰",  # 환불 — 뜻이 갈린다
        "[Web발신]\n삼성1234승인\n4,500원\n3,000원\n11/12 08:31 카페",  # 결제 금액이 둘
        "[Web발신]\n삼성카드 카드론 승인 3,000,000원 11/12 10:00",  # 2차 리뷰
        "[Web발신]\n삼성1234 승인이 거절됨\n9,000원\n11/12 09:00 편의점",  # 2차 리뷰: +9,000 이었다
        "[Web발신]\n삼성1234 승인 거래거절\n9,000원\n11/12 09:00 편의점",
        "[Web발신]\n우리카드 사용 4,500원 11/12 08:31 스타벅스",  # 승인이라는 말이 없는 모르는 꼴
    ],
)
def test_짐작해야_하는_문자는_못_읽은_것으로_센다(text: str) -> None:
    """JAR-045 1차 리뷰 재현 — 전에는 모두 결제로 셌다(+30,000, +30,000,000, +12,000)."""
    assert from_sms(_sms(text)) is None
    assert read([_sms(text)]) == Reading([], [NOW], 0)


@pytest.mark.parametrize(
    "text",
    [
        "[Web발신]\n삼성1234승인실패 홍*동\n9,000원\n11/12 09:00 편의점",
        "[Web발신]\n삼성1234승인불가 홍*동\n9,000원\n11/12 09:00 편의점",
        DECLINED,
    ],
)
def test_안_된_결제는_결제도_못_읽은_것도_아니다(text: str) -> None:
    assert read([_sms(text)]) == Reading([], [], 1)  # 거절로 센다


def test_가맹점_이름에_든_말도_짐작하지_않고_보이게_한다() -> None:
    """가맹점 "불가리"를 거절로 읽어 흔적 없이 사라졌다(2차 리뷰). 거절·취소 말이 확실한 꼴이 아닌
    자리에 있으면 못 읽은 것으로 — 합계엔 안 들지만 보인다. 예시를 받으면 꼴을 확실한 쪽에 더한다.
    """
    text = "[Web발신]\n삼성1234승인 홍*동\n350,000원 일시불\n11/12 15:00 불가리"
    assert read([_sms(text)]) == Reading([], [NOW], 0)


@pytest.mark.parametrize(
    "text, amount",
    [
        ("[Web발신]\n삼성카드 승인취소 안내\n4,500원\n11/12 08:40 스타벅스", -4500),
        ("[Web발신]\n삼성1234 승인취소 요청 완료\n4,500원\n11/12 08:40 스타벅스", -4500),
    ],
)
def test_확실한_취소_꼴은_취소다(text: str, amount: int) -> None:
    """좁힌 정규식이 "승인취소 안내"를 승인(+4,500)으로 셌다(2차 리뷰)."""
    found = from_sms(_sms(text))
    assert found is not None and found.amount == amount


@pytest.mark.parametrize(
    "tail",
    ["취소하시려면 앱에서", "취소는 고객센터", "취소수수료 없음", "취소불가 상품"],
)
def test_꼬리의_취소_안내는_짐작하지_않는다(tail: str) -> None:
    """2차 리뷰: 이런 꼬리가 붙은 승인을 −4,500 으로 셌다. 확실하지 않으니 못 읽음(보인다)."""
    assert read([_sms(SAMSUNG + "\n" + tail)]) == Reading([], [NOW], 0)


def test_안내_문구의_취소는_취소가_아니다() -> None:
    """승인 문자 끝의 "취소문의 1588-…"를 취소로 읽어 −4,500 이 됐다(1차 리뷰)."""
    found = from_sms(_sms(SAMSUNG + "\n취소문의 1588-0000"))
    assert found is not None and found.amount == 4500
    other = from_sms(_sms("[Web발신]\n신한카드(1234)취소 홍*동 12,000원 11/11 20:00 이마트"))
    assert other is not None and other.amount == -12000


def test_안내_문자는_결제가_아니라_세지_않는다() -> None:
    """보낸 사람을 카드사로 좁히면 안내 문자도 온다(SETUP 2-9). 때·금액이 없으면 결제 문자가
    아니다."""
    assert read([_sms(NOTICE)]) == Reading([], [], 0)


def test_결제_금액이_아닌_금액은_뺀다() -> None:
    # "대출"은 금액 앞에서도 빼지만, 문자에 있으면 통째로 못 읽음이다(대출·카드론 승인 — 위 시험)
    for word in ("누적", "잔액", "한도", "사용가능", "포인트", "청구", "할인"):
        text = f"[Web발신]\n우리카드 체크승인\n8,000원\n11/12 10:00 서점\n{word} 52,000원"
        found = from_sms(_sms(text))
        assert found is not None and found.amount == 8000, word
        assert found.merchant == "서점"


def test_해가_바뀌는_밤의_문자는_작년이다() -> None:
    new_year = datetime(2027, 1, 1, 0, 5, tzinfo=KST)
    found = from_sms(_sms("[Web발신]\n삼성1234승인\n30,000원\n12/31 23:59 식당", new_year))
    assert found is not None and found.at == datetime(2026, 12, 31, 23, 59, tzinfo=KST)
    # 받은 때보다 이틀 안쪽 뒤는 올해 그대로(문자의 때가 몇 분 앞서도)
    found = from_sms(_sms("[Web발신]\n삼성1234승인\n30,000원\n11/14 08:00 식당", NOW))
    assert found is not None and found.at.year == 2026


def test_같은_문자가_두_번_오면_하나로() -> None:
    """저장소는 때가 1초만 달라도 둘로 둔다 — 4,500원 하나가 "2건 9,000원"이 됐다(1차 리뷰)."""
    twice_sms = read([_sms(SAMSUNG, at(12, 8, 31, 40)), _sms(SAMSUNG, at(12, 8, 31, 41))])
    assert [p.amount for p in twice_sms.payments] == [4500] and twice_sms.unread == []
    tap = {"amount": "₩4,500", "merchant": "STARBUCKS"}
    twice = [_wallet(at(12, 8, 31, 20), tap), _wallet(at(12, 8, 31, 21), tap)]
    assert len(read(twice).payments) == 1
    # 10초 넘게 벌어진 같은 값은 두 번 산 것이다
    apart = [_wallet(at(12, 8, 31, 20), tap), _wallet(at(12, 8, 33, 0), tap)]
    assert len(read(apart).payments) == 2
    # 값이 다르면(금액) 10초 안이어도 다른 결제다 — 중복 열쇠에 meta 가 든다
    other = [
        _wallet(at(12, 8, 31, 20), tap),
        _wallet(at(12, 8, 31, 22), {**tap, "amount": "₩5,000"}),
    ]
    assert len(read(other).payments) == 2


def test_애플페이는_단축어가_넘긴_값으로() -> None:
    def amount(meta: Dict[str, Any], text: str = "결제") -> Optional[int]:
        found = from_wallet(_wallet(at(12, 8, 31, 20), meta, text))
        return found.amount if found else None

    assert amount({"amount": "₩4,500", "merchant": "STARBUCKS"}) == 4500
    assert amount({"amount": 12000.0}) == 12000
    assert amount({"amount": "12,000.00원"}) == 12000
    assert amount({"amount": ""}, "편의점 3,300원") == 3300  # 금액 칸이 비면 글에서
    assert amount({"amount": "무료"}) is None
    assert amount({"amount": True}) is None
    # 원화가 아니거나 음수면 짐작하지 않는다(1차 리뷰: "$12.00" → 12원, "-₩4,500" → +4,500)
    for strange in (
        "$12.00",
        "¥1,200",
        "€12,50",
        "-₩4,500",
        "USD 12",
        "−₩4,500",
        "(₩4,500)",
        "12 달러",
        "4.500 ₩",
        "12.5",
    ):
        assert amount({"amount": strange}) is None, strange
    assert amount({"amount": -4500}) is None
    # 칸이 있는데 못 읽으면 글로 넘어가지 않는다
    assert amount({"amount": "$12.00"}, "AMAZON 12,000원") is None
    assert amount({}, "AMAZON $12 12,000원") is None  # 글로 읽을 때도 외화면 짐작하지 않는다


def test_금액_0은_결제가_아니고_못_읽은_것도_아니다() -> None:
    """지갑 자동화는 티머니 교통 탭도 잡는다(SETUP 2-5). 금액이 0 으로 오면 그냥 넘긴다."""
    taps = [_wallet(at(12, 7, 40), {"amount": "₩0", "merchant": "티머니"})]
    assert read(taps) == Reading([], [], 0)
    assert read([_wallet(at(12, 9, 0), {"amount": "무료"})]) == Reading([], [at(12, 9, 0)], 0)


def test_두_길로_온_같은_결제는_하나로() -> None:
    sms = Payment(at(12, 8, 31), 4500, "스타벅스", "삼성1234", BY_SMS)
    wallet = Payment(at(12, 8, 31, 20), 4500, "STARBUCKS", "", BY_WALLET)
    [one] = merge([wallet, sms])
    assert one == Payment(at(12, 8, 31), 4500, "스타벅스", "삼성1234", f"{BY_SMS}+{BY_WALLET}")
    # 같은 길로 온 같은 금액 둘은 두 번 산 것이다
    assert len(merge([sms, Payment(at(12, 8, 33), 4500, "스타벅스", "삼성1234", BY_SMS)])) == 2
    # 금액이 다르거나 5분 넘게 벌어지면 다른 결제 — 딱 5분은 같은 결제
    assert len(merge([sms, Payment(at(12, 8, 31), 4000, "", "", BY_WALLET)])) == 2
    assert len(merge([sms, Payment(at(12, 8, 36), 4500, "", "", BY_WALLET)])) == 1
    assert len(merge([sms, Payment(at(12, 8, 36, 1), 4500, "", "", BY_WALLET)])) == 2
    # 이미 합친 것에 세 번째가 붙지 않는다
    third = Payment(at(12, 8, 32), 4500, "", "", BY_WALLET)
    assert len(merge([sms, wallet, third])) == 2


def test_자정을_넘겨_합쳐도_결제한_날에_남는다() -> None:
    """문자 23:59 와 애플페이 00:00:10 을 합치면 다음 날로 넘어갔다(1차 리뷰)."""
    sms = Payment(datetime(2026, 11, 8, 23, 59, tzinfo=KST), 9000, "식당", "", BY_SMS)
    wallet = Payment(datetime(2026, 11, 9, 0, 0, 10, tzinfo=KST), 9000, "", "", BY_WALLET)
    [one] = merge([sms, wallet])
    assert one.at.day == 8  # 일요일 — 지난주에 남는다


PAYMENTS = [
    Payment(at(12, 8, 31), 4500, "스타벅스", "삼성1234", f"{BY_SMS}+{BY_WALLET}"),
    Payment(at(12, 8, 40), -4500, "스타벅스", "삼성1234", BY_SMS),
    Payment(at(12, 12, 10), 11000, "김밥천국", "KB국민카드1234", BY_SMS),
    Payment(at(11, 19, 2), 12000, "이마트", "신한카드(1234)", BY_SMS),
    Payment(at(5, 18, 0), 3300, "편의점", "", BY_WALLET),
]
FRESH = {SMS: at(12, 12, 10, 30), WALLET: at(12, 8, 31, 20)}


def test_기간_합계는_코드가_센다() -> None:
    lines = summarize(PAYMENTS, [], 0, at(5, 0, 0).date(), NOW, 40, FRESH)
    assert lines == [
        "- 마지막으로 받은 때: 카드 문자 11/12(목) 12:10 · 애플페이 11/12(목) 08:31",
        "- 오늘 11/12(목): 결제 2건 15,500원, 취소 1건 4,500원 → 빼면 11,000원 — "
        "08:31 스타벅스 4,500원 · 08:40 스타벅스 취소 4,500원 · 12:10 김밥천국 11,000원",
        "- 어제 11/11(수): 결제 1건 12,000원 — 19:02 이마트 12,000원",
        "- 이번 주(11/09(월)~오늘): 결제 3건 27,500원, 취소 1건 4,500원 → 빼면 23,000원",
        "- 지난주(11/02(월)~11/08(일)): 결제 1건 3,300원 — 11/05부터 받은 것만",
        "- 이번 달(11/01~오늘): 결제 4건 30,800원, 취소 1건 4,500원 → 빼면 26,300원 — "
        "11/05부터 받은 것만",
    ]


def test_끊긴_길은_0원이_아니라_모른다고_적는다() -> None:
    """매일 오던 결제가 멈추면 "오늘 0건 0원"이 사실처럼 나갔다(1차 리뷰, High)."""
    stale = {SMS: at(3, 8, 0)}
    lines = summarize([], [], 0, at(1, 0, 0).date(), NOW, 40, stale)
    assert lines[0] == (
        "- 마지막으로 받은 때: 카드 문자 11/03(화) 08:00 — 9일째 없음(그 뒤 결제가 없었는지 "
        "받기가 끊겼는지는 모른다)"
    )
    # 이틀 안쪽이면 덧붙이지 않는다 — 딱 이틀까지
    fresh = summarize([], [], 0, at(1, 0, 0).date(), NOW, 40, {SMS: at(10, 13, 0)})
    assert "모른다" not in fresh[0]
    just_over = summarize([], [], 0, at(1, 0, 0).date(), NOW, 40, {SMS: at(10, 12, 59)})
    assert "2일째 없음" in just_over[0]


def test_앞_기간_결제의_취소만_들어오면_그렇다고_적는다() -> None:
    refund = [Payment(at(3, 9, 0), -50000, "가전", "", BY_SMS)]
    lines = summarize(refund, [], 0, datetime(2026, 9, 1, tzinfo=KST).date(), NOW, 40, FRESH)
    month = [line for line in lines if line.startswith("- 이번 달")][0]
    assert month == (
        "- 이번 달(11/01~오늘): 결제 0건 0원, 취소 1건 50,000원 → 빼면 -50,000원(이 기간 앞의 "
        "결제가 취소됐다)"
    )


def test_받기_전_기간은_모른다고_적는다() -> None:
    lines = summarize(PAYMENTS[:3], [at(12, 9, 0)], 0, at(12, 0, 0).date(), NOW, 40, FRESH)
    assert lines[1].startswith("- 오늘 11/12(목): 결제 2건 15,500원")
    assert not any(line.startswith("- 어제") for line in lines)  # 받기 전 날은 줄이 없다
    assert "- 지난주(11/02(월)~11/08(일)): 받기 전이라 모름" in lines
    assert lines[-1] == "- 결제로 못 읽은 기록 1건(최근 40일) — 합계에 안 넣었다"
    # 받기 시작한 날이 기간 첫날이면 덧붙이지 않고, 기간 끝날이면 센다
    from_monday = summarize([], [], 0, at(9, 0, 0).date(), NOW, 40, FRESH)
    assert "- 이번 주(11/09(월)~오늘): 결제 0건 0원" in from_monday
    from_sunday = summarize([], [], 0, at(8, 0, 0).date(), NOW, 40, FRESH)
    assert "- 지난주(11/02(월)~11/08(일)): 결제 0건 0원 — 11/08부터 받은 것만" in from_sunday


def test_못_읽은_것과_거절은_기간마다_보인다() -> None:
    """못 읽은 수가 끝에만 붙어 기간마다 "0건 0원"이 사실처럼 보였다(2차 리뷰)."""
    lines = summarize([], [at(12, 9, 0), at(10, 9, 0)], 2, at(1, 0, 0).date(), NOW, 40, FRESH)
    today = [line for line in lines if line.startswith("- 오늘")][0]
    assert today.endswith("결제 0건 0원 — 못 읽은 기록 1건이 이 기간에 있다(빠졌을 수 있다)")
    week = [line for line in lines if line.startswith("- 이번 주")][0]
    assert week.endswith("못 읽은 기록 2건이 이 기간에 있다(빠졌을 수 있다)")
    assert lines[-1] == "- 승인 안 된 결제(거절·실패) 2건(최근 40일) — 합계에 안 넣었다"


def test_오늘은_한국_날짜다() -> None:
    """00:30 KST 는 UTC 로 전날이다. 오늘·이번 주는 한국 날짜로 센다."""
    night = datetime(2026, 11, 9, 0, 30, tzinfo=KST)  # 월요일 새벽
    lines = summarize([], [], 0, at(1, 0, 0).date(), night, 40, {})
    assert lines[0].startswith("- 오늘 11/09(월)")
    assert any(line.startswith("- 이번 주(11/09(월)~오늘)") for line in lines)


def test_결제가_많은_날은_외_N건() -> None:
    many = [Payment(at(12, 8, i), 1000, f"가게{i}", "", BY_SMS) for i in range(10)]
    today = summarize(many, [], 0, at(1, 0, 0).date(), NOW, 40, {})[0]
    assert today.startswith("- 오늘 11/12(목): 결제 10건 10,000원 — 08:00 가게0 1,000원")
    assert today.endswith("외 2건")


class Traces:
    def __init__(self, traces: List[Trace]) -> None:
        self.traces = traces

    def recent(self, kind: str, since: datetime, limit: Optional[int] = None) -> Sequence[Trace]:
        return [t for t in self.traces if t.kind == kind and t.at >= since]


def test_맥락에는_센_것만_결제_흔적이_없으면_안_보인다() -> None:
    assert SpendingProvider(Traces([])).fetch(None, NOW) is None
    old = "[Web발신]\n삼성1234승인\n1,000원\n09/01 09:00 카페"
    traces = [
        _sms(SAMSUNG, at(12, 8, 31, 40)),
        _wallet(at(12, 8, 31, 20), {"amount": "₩4,500", "merchant": "STARBUCKS"}),
        _sms(SHINHAN, at(11, 19, 2, 30)),
        _sms(OVERSEAS, at(12, 9, 0)),
        # 40일보다 오래된 것은 합계에 안 들지만, 받기 시작한 날로는 센다
        _sms(old, datetime(2026, 9, 1, 9, 0, 30, tzinfo=KST)),
    ]
    block = SpendingProvider(Traces(traces)).fetch(None, NOW)
    assert block is not None
    assert "코드가 센 것" in block.label and "다시 더하지 말고" in block.label
    lines = block.body.splitlines()
    assert lines[0] == "- 마지막으로 받은 때: 카드 문자 11/12(목) 09:00 · 애플페이 11/12(목) 08:31"
    assert lines[1] == (
        "- 오늘 11/12(목): 결제 1건 4,500원 — 08:31 스타벅스 4,500원 — 못 읽은 기록 1건이 이 "
        "기간에 있다(빠졌을 수 있다)"
    )
    assert "부터 받은 것만" not in block.body  # 9/1 부터 받았다
    assert lines[-1] == "- 결제로 못 읽은 기록 1건(최근 40일) — 합계에 안 넣었다"


def test_자정_직전_결제를_자정_뒤에_받아도_그날부터_받은_것이다() -> None:
    late = "[Web발신]\n삼성1234승인\n9,000원\n11/04 23:58 식당"
    block = SpendingProvider(Traces([_sms(late, at(5, 0, 1))])).fetch(None, at(5, 9, 0))
    assert block is not None
    assert "- 어제 11/04(수): 결제 1건 9,000원 — 23:58 식당 9,000원" in block.body.splitlines()


def test_결제_흔적은_회고에_안_들어가고_다른_섹터와_안_겹친다() -> None:
    from app.main import SECTORS

    registry = TraceRegistry()
    for sector in SECTORS:
        registry.register(sector.TRACES)  # 같은 종류를 두 섹터가 주장하면 여기서 멈춘다
    for card in TRACES:
        assert card.sensitive and not card.reflect and card.digest is None
        assert card.via == "아이폰"  # 수집 감시가 다른 폰 종류와 묶어 원인을 짚는다
        assert registry.get(card.kind) is card
