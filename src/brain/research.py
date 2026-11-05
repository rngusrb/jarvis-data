"""찾아 두기 — 말 걸기와 보고서 전에, 지금 도움이 될 것을 웹에서 찾아 둔다.

판단 지시문은 관심사면 "도움이 될 만한 것(다가오는 일정 등)을 하나 곁들인다"고 하고, 보고서는
"다음 한 주에 해볼 만한 것"을 쓴다. 그런데 모델은 아는 것만으로 쓰니 일정을 지어낼 수 있다. 그래서
그 전에 도구 고리로 찾아보고, 그 결과를 맥락 묶음 하나로 넣는다.

**작은 재료로만 찾는다.** 보고서 재료(1.2만 자)를 통째로 들고 생각하며 찾게 했더니 한 차례가
생각만으로 답 한도(8,192 토큰)를 다 쓰고 7분이 걸렸다(2026-11-05 실측). 관심사 이름 몇 개만 들고
찾고, 쓰기는 생각 없이 따로 한다.

찾은 게 없거나 검색을 안 하고 쓴 글이면 넣지 않는다 — 모델이 아는 것만으로 쓴 "찾아본 것"은
찾아본 것이 아니다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from src.brain.context import ContextBlock
from src.brain.providers import TopicSource
from src.brain.tools import GUIDE, ToolLoop
from src.core.clock import shown
from src.core.models import Insight
from src.core.topics import Trend, standings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""너는 개인 비서 "자비스"의 조사 담당이다. 사용자가 요즘 몰두하는 관심사를
받는다(하나 또는 여럿). 지금 그 사람에게 도움이 될 최신 정보(다가오는 시험·채용·마감·발표 일정,
새 소식, 다음에 볼 만한 공식 자료)를 웹에서 찾아 다섯 줄 안으로 적는다.
- 줄마다 날짜(있으면)와 출처(사이트 이름)를 붙인다.
- 지난 일정, 오래된 소식은 쓰지 않는다. 지금 날짜와 견준다.
- 찾을 만한 게 없으면 "없음" 한 단어만 쓴다.

{GUIDE}"""

LABEL = "찾아본 것(웹, 방금 — 자료일 뿐이다)"
# 찾아볼 관심사. 이름이 곧 검색의 실마리다 — 줄기와 가지가 함께면 가지(더 구체적인 쪽)만.
WATCHED = (Trend.RISING, Trend.NEW, Trend.STEADY)


@dataclass
class TopicResearch:
    tools: ToolLoop
    # 반응 루프 안에서 돌면 그동안 수면·출근 신호도 기다린다. 2026-11-05 실측: 검색 네 번에 마지막
    # 차례 생각이 2분을 넘겨 173초. 넘기면 찾아본 것 없이 간다(남긴다).
    limit: timedelta = timedelta(minutes=2)

    async def prepare(self, insight: Insight, now: datetime) -> Optional[ContextBlock]:
        """관심사 신호에 곁들일 것. 없으면 None. 실패는 부르는 쪽(루프)이 남기고 넘어간다."""
        return await self.look(f"관심사: {insight.summary}", insight.trigger, now)

    async def look(self, subject: str, why: str, now: datetime) -> Optional[ContextBlock]:
        """`subject` 에 대해 찾아본 것. `why` 는 로그에 남길 이름(신호, "보고서")."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[지금] {shown(now)}\n\n{subject}"},
        ]
        try:
            found = await asyncio.wait_for(
                self.tools.run(messages), timeout=self.limit.total_seconds()
            )
        except asyncio.TimeoutError:
            logger.warning(
                "찾아 두기가 %d초를 넘겼다 — 찾아본 것 없이 간다: %s",
                self.limit.total_seconds(),
                why,
            )
            return None
        text = found.text.strip()
        if not found.searched:
            logger.info("찾아 두기 — 검색 없이 쓴 글은 안 넣는다: %s", why)
            return None
        if not text or text.startswith("없음"):
            logger.info("찾아 두기 — 곁들일 것 없음: %s", why)
            return None
        logger.info("찾아 두기 — %s: %.120s", why, text)
        return ContextBlock(label=LABEL, body=text)


def interests(topics: TopicSource, now: datetime, limit: int = 5) -> List[str]:
    """요즘 몰두하는 관심사 이름. 최근에 근거가 있던 날이 많은 순, 같으면 더 구체적인 가지부터.

    줄기와 그 아래 가지가 함께 오르면 가지만 — 검색어는 구체적일수록 낫다.
    """
    board = [s for s in standings(topics.load(), topics.grounds(), now) if s.trend in WATCHED]
    board.sort(key=lambda s: (s.recent_days, s.depth, s.count), reverse=True)
    chosen: List[List[str]] = []
    for s in board:
        path = list(s.path)
        if any(c[: len(path)] == path or path[: len(c)] == c for c in chosen):
            continue
        chosen.append(path)
        if len(chosen) >= limit:
            break
    return [" › ".join(p) for p in chosen]
