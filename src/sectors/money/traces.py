"""money 섹터의 흔적 종류 — 결제가 들어오는 두 길.

결제는 숫자(금액)지만 관측치로 접지 않는다. 같은 결제가 두 길(문자·애플페이)로 오면 하나로 세야
하는데, 받을 때 합계로 접으면 두 번 센 것을 되돌릴 수 없다. 원문을 흔적으로 쌓고 볼 때
읽는다(`payments.py`)."""

from __future__ import annotations

from datetime import timedelta

from src.core.traces import TraceKind
from src.sectors.money.payments import SMS, WALLET

# 결제가 없는 날은 흔하다(주말 내내 집에 있으면 사흘). 그동안 "수집 끊김"이라 말하지 않게 길게.
QUIET = timedelta(days=14)

CARD_SMS = TraceKind(
    kind=SMS,
    label="카드 승인 문자",
    collector="아이폰 단축어 — 메시지 자동화(카드 승인 문자, SETUP 2-9)",
    via="아이폰",
    stale_after=QUIET,
    # 카드 번호 뒷자리·이름(가림)·누적 금액이 들어 있다. 맥락에는 원문이 아니라 쓴 돈 요약으로만
    # 들어간다(`context.SpendingProvider` — 조립하는 곳이 넣는다).
    sensitive=True,
    # 회고는 관심사를 찾는다. 승인 문자 낱줄은 잡음이다 — 무엇을 사는지 관심사로 읽는 건 뒤 일이다.
    reflect=False,
)

WALLET_PAYMENT = TraceKind(
    kind=WALLET,
    label="애플페이 결제",
    collector="아이폰 단축어 — 지갑 자동화가 넘긴 금액·가맹점(SETUP 2-9)",
    via="아이폰",
    stale_after=QUIET,
    sensitive=True,
    reflect=False,
)

TRACES = [CARD_SMS, WALLET_PAYMENT]
