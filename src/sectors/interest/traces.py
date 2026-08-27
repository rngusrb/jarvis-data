"""interest 섹터의 흔적 종류.

이 섹터가 사는 이유는 다른 섹터들과 다르다. health 나 commute 는 **삶의
영역**을 맡지만, interest 는 "이 사람이 지금 뭘 알아보고 있나"를 맡는다.
브라우저 기록·파일·스크린샷처럼 **의도가 드러나는 흔적**이 여기 모인다.

왜 흔적을 소스별로 섹터를 쪼개지 않았나: 검색 기록은 쇼핑에도 스케줄에도
쓰인다. 소스마다 섹터를 만들면 나중에 "이 흔적은 누구 것이냐"로 싸운다.
**흔적의 주인은 그걸 만든 기계가 아니라 그걸 쓰는 목적**이다.
"""

from __future__ import annotations

from datetime import timedelta

from src.core.traces import TraceKind

WEB_VISIT = TraceKind(
    kind="web_visit",
    label="웹 방문",
    collector="mac_chrome",
    # 크롬 기록은 하루에도 수십 건이다. 이틀 조용하면 수집기가 멈춘 것이다
    # (맥이 꺼져 있었을 수도 있지만, 그것도 알아야 할 사실이다).
    stale_after=timedelta(days=2),
    # 검색 기록은 본인도 자비스가 언급 안 했으면 하는 게 섞인다. 모으는 건
    # 다 모으되, 프롬프트에 넣을지는 따로 정한다.
    sensitive=True,
)

TRACES = [WEB_VISIT]
