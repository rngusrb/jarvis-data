"""여러 저장소의 "마지막으로 들어온 때"를 한 장으로.

관측치·흔적·사건이 각자 다른 표에 산다. 수집 감시와 "수집 경로 현황"은 그걸 한꺼번에
봐야 한다 — 한쪽만 보면 표를 옮긴 종류(위치: 관측치 → 사건)가 "한 번도 안 들어옴"이나
"수집기 없음"으로 보인다.

같은 이름이 두 표에 있으면 늦은 쪽을 쓰고 경고를 남긴다. 표를 옮기는 중(새 코드는 떴는데
옮기기 전)에만 생긴다. 처음엔 멈추게 했는데, 그러면 그동안 수면·웹·앱의 끊김 경고까지
전부 꺼졌다(2026-11-02 JAR-015 리뷰). 카드 이름이 겹치는 건 `app/main.py` 가 기동 때 막는다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict

from src.core.models import ObservationCatalog

logger = logging.getLogger(__name__)


class MergedCatalog:
    def __init__(self, *catalogs: ObservationCatalog) -> None:
        self._catalogs = catalogs

    def last_seen(self) -> Dict[str, datetime]:
        merged: Dict[str, datetime] = {}
        for catalog in self._catalogs:
            for kind, at in catalog.last_seen().items():
                if kind in merged:
                    logger.warning(
                        "'%s' 가 두 저장소에 있다 — 늦은 쪽을 쓴다 (옮기기 전인가?)", kind
                    )
                    at = max(at, merged[kind])
                merged[kind] = at
        return merged
