"""흔적 하루 요약 맥락 — 낱줄 대신 카드가 들고 온 요약을 어제·오늘치로."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from src.brain.providers import TraceDigestProvider
from src.core.models import Trace
from src.core.traces import TraceKind

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


@dataclass
class FakeTraces:
    traces: List[Trace]
    asked: List[Tuple[str, datetime]] = field(default_factory=list)

    def recent(self, kind: str, since: datetime, limit: Optional[int] = None) -> Sequence[Trace]:
        self.asked.append((kind, since))
        return [t for t in self.traces if t.kind == kind and t.at >= since]


def _count(traces: Sequence[Trace], since: datetime, until: datetime) -> List[str]:
    return [f"{len(traces)}줄"] if traces else []


def _source() -> FakeTraces:
    return FakeTraces(
        [
            Trace(source="mac_apps", kind="mac_app", text="Code 30분", at=NOW - timedelta(hours=1)),
            Trace(source="mac_chrome", kind="web_visit", text="검색", at=NOW - timedelta(hours=1)),
        ]
    )


APP = TraceKind(
    kind="mac_app", label="맥 앱 사용", collector="mac_apps", reflect=False, digest=_count
)


def test_요약이_있는_종류를_어제_오늘치로_보여준다() -> None:
    source = _source()
    block = TraceDigestProvider(source=source, kinds=[APP]).fetch(None, NOW)
    assert block is not None
    assert "맥 앱 사용" in block.body and "1줄" in block.body
    assert source.asked == [("mac_app", NOW - timedelta(days=2))]


def test_요약이_없는_종류는_넣지_않는다() -> None:
    web = TraceKind(kind="web_visit", label="웹 방문", collector="mac_chrome")
    assert TraceDigestProvider(source=_source(), kinds=[web]).fetch(None, NOW) is None


def test_민감한_종류는_기본으로는_넣지_않는다() -> None:
    # 반응·대화의 말은 텔레그램으로 나가는데 민감한 걸 거르는 장치가 아직 없다(4단계).
    web = TraceKind(
        kind="web_visit", label="웹 방문", collector="mac_chrome", sensitive=True, digest=_count
    )
    assert TraceDigestProvider(source=_source(), kinds=[web]).fetch(None, NOW) is None


def test_민감한_종류도_넣기로_하면_넣는다() -> None:
    # 실험 단계라 허들을 낮춘다(2026-11-02 사용자 결정) — 조립하는 곳이 켠다.
    web = TraceKind(
        kind="web_visit", label="웹 방문", collector="mac_chrome", sensitive=True, digest=_count
    )
    provider = TraceDigestProvider(source=_source(), kinds=[web], include_sensitive=True)
    block = provider.fetch(None, NOW)
    assert block is not None and "웹 방문" in block.body


def test_접힌_종류는_넣지_않는다() -> None:
    retired = TraceKind(kind="mac_app", label="맥 앱 사용", reflect=False, digest=_count)
    assert TraceDigestProvider(source=_source(), kinds=[retired]).fetch(None, NOW) is None


def test_요약할_게_없으면_조용하다() -> None:
    empty = FakeTraces([])
    assert TraceDigestProvider(source=empty, kinds=[APP]).fetch(None, NOW) is None
