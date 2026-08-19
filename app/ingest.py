"""수집 수신구 — 아이폰 단축어가 관측치를 밀어넣는 곳.

export.xml은 손으로 내보내야 해서 자동화가 안 된다. 그래서 과거 히스토리는
export로 한 번 백필하고, 그 뒤로는 단축어 자동화가 매일 어제치만 여기로 쏜다.

Tailscale 안이라 외부에 노출되지 않지만 토큰을 한 겹 더 둔다. 아이폰을 잃어버리거나
같은 tailnet에 다른 기기가 붙었을 때 건강 데이터가 무방비면 곤란하다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from src.core.models import Observation
from src.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)
router = APIRouter()


class ObservationIn(BaseModel):
    kind: str
    value: float
    at: datetime
    meta: Dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    source: str = "shortcuts"
    observations: List[ObservationIn]


class IngestResponse(BaseModel):
    written: int


def _verify_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected: str = request.app.state.settings.ingest_token
    if not expected:
        # 토큰을 설정하지 않았으면 아예 받지 않는다. 인증 없이 열어두는 것보다
        # 대놓고 막히는 편이 낫다 — 조용히 무방비가 되는 게 제일 나쁘다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JARVIS_INGEST_TOKEN이 설정되지 않았다",
        )
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if supplied != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="토큰 불일치")


@router.post("/ingest", response_model=IngestResponse, dependencies=[Depends(_verify_token)])
def ingest(payload: IngestRequest, request: Request) -> IngestResponse:
    store: SQLiteStore = request.app.state.store
    observations = [
        Observation(
            source=payload.source,
            kind=item.kind,
            value=item.value,
            at=item.at,
            meta=item.meta,
        )
        for item in payload.observations
    ]
    # 저장소가 (source, kind, at) 기준으로 덮어쓰므로 단축어가 두 번 울려도 안전하다.
    written = store.write(observations)
    logger.info("수집 %d건 (source=%s)", written, payload.source)
    return IngestResponse(written=written)


@router.get("/health")
def health(request: Request) -> Dict[str, Any]:
    store: SQLiteStore = request.app.state.store
    return {"ok": True, "observations": store.count(), "kinds": store.kinds()}
