"""여러 저장소의 "마지막으로 들어온 때"를 한 장으로.

관측치·흔적·사건이 각자 다른 표에 산다. 수집 감시와 "수집 경로 현황"은 그걸 한꺼번에
봐야 한다 — 한쪽만 보면 표를 옮긴 종류(위치: 관측치 → 사건)가 "한 번도 안 들어옴"이나
"수집기 없음"으로 보인다. 같은 이름이 두 표에 있으면 어느 쪽 시각인지 모르므로 멈춘다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from src.core.models import ObservationCatalog


class MergedCatalog:
    def __init__(self, *catalogs: ObservationCatalog) -> None:
        self._catalogs = catalogs

    def last_seen(self) -> Dict[str, datetime]:
        merged: Dict[str, datetime] = {}
        for catalog in self._catalogs:
            for kind, at in catalog.last_seen().items():
                if kind in merged:
                    raise ValueError(f"'{kind}' 가 두 저장소에 있다 — 어느 쪽 시각인지 모른다")
                merged[kind] = at
        return merged
