"""수집 수신구 — 관측치가 서버로 들어오는 문.

문이 두 개고 쓰임이 다르다.

  POST /ingest        이미 숫자 하나로 정리된 지표 (걸음수 합계, 심박 평균)
  POST /ingest/spans  구간 원본을 그대로 받아 서버가 집계 (수면)

수면이 후자인 이유는 **조각 수가 측정 품질 신호**이기 때문이다. 아이폰이 합산해서
"4.71시간"만 보내면 그 정보가 사라지고, 측정 실패한 밤을 걸러낼 수 없게 된다.

그래서 판단이 필요한 일은 전부 서버로 밀어넣고 단축어는 배달만 하게 한다.
단축어는 고치려면 아이폰을 열어야 하고, git에도 없고, 뭘 바꿨는지 기록도 안 남는다.
**어려운 일은 고칠 수 있는 쪽에서 한다.**
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from pydantic import BaseModel, Field

from src.core.folding import Sample, daily_mean, daily_sum, nights_from_spans
from src.core.metrics import Fold, Metric, MetricRegistry
from src.core.models import Observation
from src.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)
router = APIRouter()

# source는 **데이터 출처**지 전송 경로가 아니다. 단축어로 왔든 export.xml로 왔든
# 애플 건강에서 나온 건 똑같다. 여기를 "shortcuts"로 두면 저장소 키가
# (source, kind, at)이라 같은 밤이 두 줄로 남고 baseline이 이중 계산된다.
DEFAULT_SOURCE = "apple_health"

# 접는 법의 **구현**은 플랫폼이 갖고, **어느 방식인지**는 지표 카드가 고른다.
# 그래서 이 파일에는 "sleep_hours" 같은 지표 이름이 하나도 등장하지 않는다.
FOLDERS = {Fold.SUM: daily_sum, Fold.MEAN: daily_mean}

# 수면을 가리키는 말은 출처마다 다르다 — export.xml은 "AsleepCore",
# 한국어 단축어는 "수면 시간", 영어로 바꾸면 또 달라진다. 반면 **수면이 아닌**
# 상태는 두 가지뿐이고 앞으로도 늘지 않는다: 깨어 있음, 침대에만 있음.
#
# 그래서 "수면인 것만 통과"가 아니라 "수면 아닌 것만 배제"로 판단한다.
# 모르는 표현이 새로 나타나도 조용히 데이터를 잃지 않는다.
NOT_ASLEEP_MARKERS = ("awake", "inbed", "깨어", "침대")


def is_asleep(stage: Optional[str]) -> bool:
    if not stage:
        # 단계 정보 없이 오는 경로도 있다. 모르는 것과 깨어 있는 것은 다르다.
        return True
    normalized = stage.replace(" ", "").lower()
    return not any(marker in normalized for marker in NOT_ASLEEP_MARKERS)


class ObservationIn(BaseModel):
    kind: str
    value: float
    at: datetime
    meta: Dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    source: str = DEFAULT_SOURCE
    observations: List[ObservationIn]


class SpanIn(BaseModel):
    start: datetime
    end: datetime
    # 단축어가 수면 단계를 못 실어 보낼 수도 있다. 없으면 전부 수면으로 본다.
    stage: Optional[str] = None


class SampleIn(BaseModel):
    at: datetime
    value: float
    # 아이폰과 워치가 같은 걸음을 각자 센다. 어느 기기 것인지 알아야
    # 겹치는 구간을 걷어낼 수 있다. 없으면 중복 제거 없이 그냥 더한다.
    end: Optional[datetime] = None
    source: str = ""


class SampleIngestRequest(BaseModel):
    source: str = DEFAULT_SOURCE
    kind: str
    samples: List[SampleIn]


class SpanIngestRequest(BaseModel):
    source: str = DEFAULT_SOURCE
    # 생략할 수 있다. 구간 방식 지표가 하나뿐이면 그것으로 본다 — 단축어가 이미
    # kind 없이 쏘고 있는데, 그 편의 하나 때문에 플랫폼에 지표 이름을 박을 수는 없다.
    kind: Optional[str] = None
    spans: List[SpanIn]


class IngestResponse(BaseModel):
    written: int
    observations: List[Dict[str, Any]] = Field(default_factory=list)
    # 어떤 단계 값이 실제로 도착했는지 되돌려준다. 기기마다 언어마다 다르게 올 수
    # 있어서, written이 0일 때 "무엇이 걸러졌는지"가 안 보이면 원인을 못 찾는다.
    stages: List[str] = Field(default_factory=list)
    # 저장된 결과만 보면 값이 왜 이상한지 알 수 없다. 폰에서 무엇이 떠났는지
    # 서버 로그를 뒤지지 않고 응답만으로 보이게 한다.
    received: Optional[Dict[str, Any]] = None


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


def _summarize(observations: List[Observation]) -> List[Dict[str, Any]]:
    """응답에 실어 보낼 요약.

    아이폰에서 단축어를 만들며 "지금 뭐가 저장됐지"를 눈으로 확인해야 한다.
    /health까지 가지 않고 응답만 봐도 알 수 있게 한다.
    """
    return [
        {"kind": o.kind, "at": o.at.isoformat(), "value": o.value, **o.meta} for o in observations
    ]


def _spans_metric(request: Request, kind: Optional[str]) -> Metric:
    """구간 문으로 들어온 요청의 카드를 찾는다."""
    if kind is not None:
        return _metric(request, kind, Fold.SPANS)

    registry: MetricRegistry = request.app.state.metrics
    candidates = [m for m in registry.all() if m.fold is Fold.SPANS]
    if len(candidates) == 1:
        return candidates[0]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"kind를 지정해야 한다 — 구간 방식 지표가 {len(candidates)}개다",
    )


def _lookup(request: Request, kind: str) -> Metric:
    """카드를 찾는다.

    등록 안 된 것과 방식이 안 맞는 것을 구별한다 — 원인을 뭉뚱그려 알려주면
    엉뚱한 데를 뒤지게 된다.
    """
    registry: MetricRegistry = request.app.state.metrics
    metric = registry.get(kind)
    if metric is None:
        known = ", ".join(m.kind for m in registry.all()) or "(등록된 지표 없음)"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{kind}'는 등록되지 않은 지표다. 아는 것: {known}",
        )
    return metric


def _metric(request: Request, kind: str, expected: Fold) -> Metric:
    """카드를 찾고 접는 방식이 이 문으로 들어올 모양인지 확인한다."""
    metric = _lookup(request, kind)
    if metric.fold is not expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{kind}'는 {metric.fold.value} 방식이다 — 이 경로는 {expected.value}만 받는다",
        )
    return metric


def _wake_jarvis(request: Request, background: BackgroundTasks, written: int) -> None:
    """새 데이터가 들어왔으니 판단을 앞당긴다.

    주기 루프만 있으면 아침에 데이터가 도착해도 최대 30분을 기다린 뒤에야
    말을 건다. 기상 직후에 오는 말과 30분 뒤에 오는 말은 쓸모가 다르다.

    주기 루프는 그대로 둔다 — 데이터가 **안** 들어오는 것도 신호이고,
    그걸 알아채려면 시계가 계속 돌아야 한다.
    """
    if written <= 0:
        return
    jarvis = getattr(request.app.state, "jarvis", None)
    if jarvis is None:
        # 수신구만 띄운 구성(테스트 등)도 있다. 수집 자체는 성공했으므로
        # 판단할 상대가 없다고 요청을 실패시킬 이유는 없다.
        return
    background.add_task(jarvis.run_once)


@router.post("/ingest", response_model=IngestResponse, dependencies=[Depends(_verify_token)])
def ingest(payload: IngestRequest, request: Request, background: BackgroundTasks) -> IngestResponse:
    """이미 계산된 값을 받는다 — 걸음수, 심박처럼 품질 신호가 따로 없는 지표용."""
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
    _wake_jarvis(request, background, written)
    return IngestResponse(written=written, observations=_summarize(observations))


@router.post("/ingest/spans", response_model=IngestResponse, dependencies=[Depends(_verify_token)])
def ingest_spans(
    payload: SpanIngestRequest, request: Request, background: BackgroundTasks
) -> IngestResponse:
    """구간 원본을 받아 서버가 집계한다.

    백필(export.xml)이 쓰는 것과 **같은 함수**(nights_from_spans)를 거친다.
    계산이 두 군데 있으면 경로에 따라 값이 달라지고, 그건 나중에 원인을 찾기
    지독히 어려운 종류의 버그가 된다.
    """
    metric = _spans_metric(request, payload.kind)

    # 단계 필터링도 서버가 한다. 단축어에서 조건 분기를 짜는 것보다 훨씬 싸고,
    # 나중에 기준이 바뀌어도 아이폰을 열 필요가 없다.
    asleep = [(span.start, span.end) for span in payload.spans if is_asleep(span.stage)]
    seen_stages = sorted({span.stage for span in payload.spans if span.stage})

    if not asleep:
        logger.info(
            "구간 수집 — 받은 %d개 전부 걸러짐. 도착한 단계: %s",
            len(payload.spans),
            seen_stages,
        )
        return IngestResponse(written=0, stages=seen_stages)

    observations = nights_from_spans(asleep, kind=metric.kind, source=payload.source)
    store: SQLiteStore = request.app.state.store
    written = store.write(observations)
    logger.info(
        "구간 수집 — 받은 %d개 중 수면 %d개 → 밤 %d건",
        len(payload.spans),
        len(asleep),
        written,
    )
    _wake_jarvis(request, background, written)
    return IngestResponse(
        written=written, observations=_summarize(observations), stages=seen_stages
    )


@router.post(
    "/ingest/samples", response_model=IngestResponse, dependencies=[Depends(_verify_token)]
)
def ingest_samples(
    payload: SampleIngestRequest, request: Request, background: BackgroundTasks
) -> IngestResponse:
    """점 단위 측정값을 받아 서버가 하루치로 접는다.

    걸음수는 더하고 심박은 평균 낸다. 폰에서 미리 접어 보내게 하면 그 규칙이
    두 군데 살게 되고, 백필과 값이 어긋나기 시작한다.
    """
    points = [
        Sample(start=item.at, end=item.end or item.at, value=item.value, source=item.source)
        for item in payload.samples
    ]
    if not points:
        return IngestResponse(written=0)

    values = [p.value for p in points]
    received = {
        "samples": len(points),
        "min": min(values),
        "max": max(values),
        "sources": sorted({p.source for p in points if p.source}) or ["(없음)"],
        "first_3": [
            {"at": p.start.isoformat(), "value": p.value, "source": p.source} for p in points[:3]
        ],
    }

    metric = _lookup(request, payload.kind)
    if metric.fold not in FOLDERS:
        expected = ", ".join(f.value for f in FOLDERS)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{payload.kind}'는 {metric.fold.value} 방식이다 — 이 경로는 {expected}만 받는다"
            ),
        )
    observations = FOLDERS[metric.fold](points, payload.kind, payload.source)

    store: SQLiteStore = request.app.state.store
    written = store.write(observations)
    logger.info("표본 수집 — %s %d개 → %d일치", payload.kind, len(points), written)
    _wake_jarvis(request, background, written)
    return IngestResponse(written=written, observations=_summarize(observations), received=received)


@router.get("/health")
def health(request: Request) -> Dict[str, Any]:
    """수집이 살아 있는지 눈으로 확인하는 곳."""
    store: SQLiteStore = request.app.state.store
    return {
        "ok": True,
        "observations": store.count(),
        "kinds": store.kinds(),
        "last_seen": {kind: at.isoformat() for kind, at in store.last_seen().items()},
    }
