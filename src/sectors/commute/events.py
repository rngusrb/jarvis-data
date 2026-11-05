"""commute 섹터의 사건 카드.

위치는 "언제 어디에 있었나"다. 2026-11 전까지는 관측치(숫자)로 담았는데 값이 없어
0 을 넣고 좌표와 이유(leave_home, wallet)를 meta 에 우겨넣었다. 사건으로 옮겼다 —
장소(집·역·회사)와 루틴(평일 07:30 집을 나섬)은 사건을 묶어 만든다.
"""

from __future__ import annotations

from src.core.events import EventKind
from src.sectors.commute.metrics import STALE_AFTER

# 2026-10 기준 74건 중 지갑 탭 58, 집을 나설 때 7. 둘 다 이 단축어로 온다.
SHORTCUT = "아이폰 단축어 — '떠날 때'·지갑 탭 자동화"

LOCATION = EventKind(
    kind="location",
    label="위치",
    collector=SHORTCUT,
    via="아이폰",
    stale_after=STALE_AFTER,
    # 단축어가 값 칸에 넣는 것은 GPS 정확도(m)다. 지금은 0 을 보낸다.
    value_as="accuracy",
    # 단축어 자동화의 이름. 지갑은 교통카드를 Wallet 에 둬서 탈 때·갈아탈 때·내릴 때
    # 찍힌다(OVERVIEW 6).
    labels=(("leave_home", "집을 나섬"), ("wallet", "지갑(교통카드) 탭")),
)

EVENTS = [LOCATION]
