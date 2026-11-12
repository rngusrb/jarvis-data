"""조립 지점이 실제로 뜨는지.

**이 파일이 없어서 서버가 죽었다.**

2026-08-28. `app/main.py` 가 `sector.TRACES` 를 순회하는데 health 섹터에만
그 속성이 없었다. 서버는 기동 즉시 AttributeError 로 죽고 재시작을 반복했는데,
`harness all` 은 통과했다 — **app/main.py 를 import 하는 테스트가 하나도
없었기 때문이다.** 전부를 무너뜨릴 수 있는 유일한 파일에 커버리지가 0이었다.

여기서 보는 건 로직이 아니라 **조립이 성립하는가**다. 섹터가 늘 때마다
계약을 지켰는지 확인하는 자리이기도 하다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app.main import SECTORS, build_reflector, build_reporter
from src.brain.providers import CollectionStatusProvider
from src.core.config import Settings

SECTOR_CONTRACT = ("METRICS", "TRACES", "EVENTS", "TRIGGERS")


@pytest.mark.parametrize("name", SECTOR_CONTRACT)
def test_every_sector_keeps_the_contract(name: str) -> None:
    """섹터 하나가 모양을 어기면 서버가 통째로 안 뜬다.

    app/main.py 가 특별 취급 없이 순회하는 게 이 구조의 전제다. 빠진 섹터를
    getattr 로 봐주면 그 순간 "어떤 섹터는 뭘 안 낸다"는 예외가 생기고,
    다음 사람은 뭘 내야 하는지 알 수 없게 된다.
    """
    missing = [s.__name__ for s in SECTORS if not hasattr(s, name)]
    assert not missing, f"{name} 가 없는 섹터: {missing}"

    wrong = [s.__name__ for s in SECTORS if not isinstance(getattr(s, name), list)]
    assert not wrong, f"{name} 가 list 가 아닌 섹터: {wrong}"


def test_no_sector_claims_the_same_kind() -> None:
    """두 섹터가 같은 kind를 주장하면 저장소에서 섞인다."""
    for name in ("METRICS", "TRACES"):
        kinds: List[Any] = [c.kind for s in SECTORS for c in getattr(s, name)]
        assert len(kinds) == len(set(kinds)), f"{name} 에 중복 kind: {kinds}"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        brain_base_url="http://localhost:8000",
        brain_model="",
        telegram_bot_token="",
        telegram_chat_id="",
        loop_interval_sec=1800,
        db_path=tmp_path / "t.db",
        ingest_token="test-token",
    )


def test_the_app_actually_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """lifespan 을 끝까지 태운다.

    import 만으로는 부족하다 — 죽은 자리가 lifespan 안이었다. TestClient 를
    컨텍스트로 열어야 startup 이 실제로 돈다.
    """
    monkeypatch.setattr("app.main.load_settings", lambda: _settings(tmp_path))
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # 수신구는 이것들을 getattr 로 찾는다(테스트용 구성 때문). 배선이 빠지면 장소가
        # 조용히 안 붙고, 장소를 보는 트리거가 영영 안 울린다(JAR-016 리뷰).
        for name in ("events", "places", "catalog", "event_kinds"):
            assert getattr(client.app.state, name, None) is not None, name
        # 앱 사용 하루 요약이 반응·대화의 맥락에 들어간다(JAR-019).
        digest = [p for p in client.app.state.jarvis.agent.providers if p.name == "trace_digest"]
        assert len(digest) == 1
        # 민감한 웹 방문도 맥락에 넣는다 — 실험 단계라 허들을 낮췄다(JAR-020).
        assert digest[0].include_sensitive is True
        # 회고가 가꾸는 주제 나무도 들어간다(JAR-023).
        names = [p.name for p in client.app.state.jarvis.agent.providers]
        assert "topics" in names
        # 먼저 건 말에 대한 👍👎 가 판단 맥락에, 뜨는 관심사가 신호로 들어간다(JAR-024·025).
        assert "feedback" in names
        jarvis = client.app.state.jarvis
        assert jarvis.topic_watch is not None and jarvis.topics is not None
        assert "topic" in jarvis.agent.gate.family_cooldowns
        # 건강·이동도 줄기로 센다 — 이름에 줄기가 없는 신호도 줄기 간격을 쓴다(JAR-029).
        assert {"health", "commute"} <= set(jarvis.agent.gate.family_cooldowns)
        # 먼저 건 말도 대화 기록에 적는다(JAR-030).
        assert jarvis.transcript is not None
        # 어제·오늘 위치 사건도 맥락에, /report 를 쓰는 쪽도 조립된다(JAR-027·028).
        assert "event_digest" in names
        assert client.app.state.reporter is not None
        # 반응·대화의 수집 현황은 접은 지표도 보인다 — 빼면 버린 지표를 되살리라고 조른다.
        status = [p for p in jarvis.agent.providers if isinstance(p, CollectionStatusProvider)]
        assert len(status) == 1 and status[0].retired is True
        # 날씨 섹터가 켜지고 줄기로 센다. 키가 없으면 서버가 가져오는 것도 없다(JAR-043).
        assert "weather_forecast" in {m.kind for m in client.app.state.metrics.all()}
        assert "weather" in jarvis.agent.gate.family_cooldowns
        assert client.app.state.pullers == []
        # 서울 지하철 키가 없으면 열차 알림 신호를 끈다 — 도착 없이 열차 얘기는 쓸모가 없다.
        assert "train" in jarvis.agent.gate.family_cooldowns
        assert jarvis.prepare_for == {}
        assert "train_ahead" not in {t.name for t in jarvis.event_triggers}
        # 스스로 알아본 것(JAR-046) — 줄기 간격(하루 두 번), 맥락, 신호가 붙는다. 검색이 없으면
        # 탐색은 안 띄운다(시험 설정은 검색 주소가 비었다).
        assert "finding" in jarvis.agent.gate.family_cooldowns
        assert "findings" in names
        assert jarvis.finding_watch is not None and jarvis.findings is not None
        assert "late_departure" in {t.name for t in jarvis.event_triggers}


def test_reporter_leaves_retired_metrics_out(tmp_path: Path) -> None:
    """보고서는 접은 지표(걸음수, 출퇴근 시각)를 "모르는 것"으로 적었다.

    지시문에 "접은 것"이라고 적어도 3번 중 2번 그랬다(2026-11-05 실데이터) — 그래서 아예 안 보인다.
    """
    reporter = build_reporter(_settings(tmp_path))
    status = [p for p in reporter.providers if isinstance(p, CollectionStatusProvider)]
    assert len(status) == 1 and status[0].retired is False


def test_reflector_assembles(tmp_path: Path) -> None:
    """회고 CLI 가 쓰는 조립 경로. 여기도 섹터 모양에 기댄다."""
    reflector = build_reflector(_settings(tmp_path))
    assert [c.kind for c in reflector.kinds if c.reflect] == ["web_visit"]
    # 낱줄로 안 훑는 종류는 하루 요약으로 들어간다.
    assert [c.kind for c in reflector.kinds if not c.reflect and c.digest] == ["mac_app"]


def test_web_search_is_on_only_with_an_address(tmp_path: Path) -> None:
    """검색 주소가 없으면(시험 설정) 도구 고리를 안 붙인다. 있으면 SearXNG 와 페이지 읽기로."""
    from dataclasses import replace

    from app.main import build_tools
    from src.brain.web import SearxSearch

    assert build_tools(_settings(tmp_path)) is None
    tools = build_tools(
        replace(_settings(tmp_path), search_base_url="http://searx:8888"), searches=3
    )
    assert tools is not None and tools.searches == 3
    assert isinstance(tools.search, SearxSearch) and tools.search.base_url == "http://searx:8888"
    assert build_reporter(_settings(tmp_path)).research is None


def test_the_browser_is_built_only_when_on_and_installed(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """대화만 브라우저를 다룬다. 끄면(`JARVIS_BROWSER=0`) 없고, playwright 가 없으면 끄고 남긴다."""
    import sys
    from dataclasses import replace

    from app.main import build_browse
    from src.brain.browse import BrowseAgent

    assert build_browse(_settings(tmp_path)) is None  # 시험 설정은 끔
    on = replace(_settings(tmp_path), browser=True)
    agent = build_browse(replace(on, browsers_path="/opt/jarvis/ms-playwright"))
    assert isinstance(agent, BrowseAgent) and agent.browser.sandbox is True
    assert agent.browser.browsers_path == "/opt/jarvis/ms-playwright"
    monkeypatch.setitem(sys.modules, "playwright", None)
    assert build_browse(on) is None and "playwright 가 없다" in caplog.text


def test_the_chrome_folder_defaults_to_a_root_owned_place_on_linux(monkeypatch: Any) -> None:
    """서버(리눅스)는 root 가 가진 /opt/jarvis/ms-playwright — 격리 허용 프로필이 거기에만
    붙는다."""
    import importlib
    import sys

    from src.core import config

    monkeypatch.setattr(sys, "platform", "linux")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEFAULT_BROWSERS_PATH == "/opt/jarvis/ms-playwright"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert importlib.reload(config).DEFAULT_BROWSERS_PATH == ""
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_pullers_are_built_only_with_the_public_data_key(tmp_path: Path) -> None:
    """공공데이터포털 키가 있으면 공휴일·예보를 서버가 가져온다 — 예보는 이동 섹터의 집 근처."""
    from dataclasses import replace

    from app.main import build_pullers, outside_client
    from src.storage.days import SQLiteHolidays
    from src.storage.events import SQLiteEventStore
    from src.storage.places import SQLitePlaceStore
    from src.storage.sqlite import SQLiteStore

    db = tmp_path / "t.db"
    parts = dict(
        store=SQLiteStore(db),
        holidays=SQLiteHolidays(db),
        events=SQLiteEventStore(db),
        places=SQLitePlaceStore(db),
        client=outside_client(),
    )
    assert build_pullers(_settings(tmp_path), **parts) == []  # type: ignore[arg-type]
    pullers = build_pullers(replace(_settings(tmp_path), data_go_key="k%2B"), **parts)  # type: ignore[arg-type]
    assert [p.name for p in pullers] == ["공휴일", "기상청 예보"]
    assert pullers[1].where() is None  # type: ignore[attr-defined]  # 위치 사건이 없으면 집을 모른다
    named = build_pullers(replace(_settings(tmp_path), kakao_key="kk"), **parts)  # type: ignore[arg-type]
    assert [p.name for p in named] == ["장소 이름"]


def test_the_train_preparation_is_built_only_with_the_subway_key(tmp_path: Path) -> None:
    from dataclasses import replace

    from app.main import build_preparers, outside_client
    from src.storage.places import SQLitePlaceStore

    places, client = SQLitePlaceStore(tmp_path / "t.db"), outside_client()
    assert build_preparers(_settings(tmp_path), places, client) == {}
    # 방향을 정할 역 자리(카카오)와 탈 열차를 가를 시간표(서울 열린데이터)도 있어야 한다 — 하나라도
    # 없으면 끈다(실시간 두 열차만으로는 탈 열차를 못 정했다 — 리뷰 JAR-044).
    assert build_preparers(replace(_settings(tmp_path), seoul_key="sk"), places, client) == {}
    two = replace(_settings(tmp_path), seoul_key="sk", kakao_key="kk")
    assert build_preparers(two, places, client) == {}
    every = replace(two, seoul_open_key="ok")
    built = build_preparers(every, places, client)
    assert list(built) == ["train_ahead"] and built["train_ahead"].required is True


def test_the_saved_holidays_are_loaded_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """받아 둔 공휴일 표를 시작할 때 올린다 — 지워도 시험이 통과했다(리뷰 JAR-043). 안 올리면 다시
    받기 전까지 공휴일 아침이 출근으로 보인다."""
    from datetime import date, datetime, timezone

    from src.core.days import holiday
    from src.storage.days import SQLiteHolidays

    SQLiteHolidays(tmp_path / "t.db").save(
        2026, {date(2026, 9, 24): "추석"}, datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    monkeypatch.setattr("app.main.load_settings", lambda: _settings(tmp_path))
    from app.main import app

    with TestClient(app):
        assert holiday(date(2026, 9, 24)) == "추석"


def test_the_explorer_waits_while_talking_and_resumes_from_its_store(tmp_path: Path) -> None:
    """탐색의 시계는 대화 중이면 미루고, 재시작해도 마지막으로 알아본 때부터 센다(JAR-046)."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from app.main import TALKING, build_explorer
    from src.core.findings import KEEP, Finding
    from src.storage.conversation import SQLiteConversation
    from src.storage.findings import SQLiteFindings

    settings = replace(_settings(tmp_path), search_base_url="http://search.test")
    findings = SQLiteFindings(settings.db_path)
    conversation = SQLiteConversation(settings.db_path)
    loop = build_explorer(settings, findings, conversation)
    assert loop is not None and loop.every == timedelta(minutes=10)
    now = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)
    assert loop.busy(now) is False
    conversation.add("user", now - timedelta(minutes=3), "안녕")
    assert loop.busy(now) is True and loop.busy(now + TALKING) is False
    assert loop.last() is None
    findings.write(Finding(now, "자유", "free:1", "s", "", (), (), (), KEEP))
    assert loop.last() == now
    assert build_explorer(replace(settings, explore_every_min=0), findings, conversation) is None
    assert build_explorer(replace(settings, search_base_url=""), findings, conversation) is None


def test_the_explorer_judges_in_a_fixed_shape(tmp_path: Path, monkeypatch: Any) -> None:
    """판단 두뇌는 정해진 꼴로만 답한다 — 지시문만으론 값어치 칸에 제목을 쓰고 사실을 늘어놓다
    잘렸다(JAR-046 서버 실측)."""
    from dataclasses import replace

    import app.main as main
    from src.brain.explore import JUDGED
    from src.storage.conversation import SQLiteConversation
    from src.storage.findings import SQLiteFindings

    made: List[Dict[str, Any]] = []
    real = main.VLLMClient

    def recording(*args: Any, **options: Any) -> Any:
        made.append(options)
        return real(*args, **options)

    monkeypatch.setattr(main, "VLLMClient", recording)
    settings = replace(_settings(tmp_path), search_base_url="http://search.test")
    loop = main.build_explorer(
        settings, SQLiteFindings(settings.db_path), SQLiteConversation(settings.db_path)
    )
    assert loop is not None
    judges = [o for o in made if o.get("schema") is not None]
    assert len(judges) == 1 and judges[0]["schema"] is JUDGED
    assert judges[0]["max_tokens"] >= 4096


def test_free_seeds_follow_the_sectors_search_traces(tmp_path: Path) -> None:
    """자유 씨앗은 크롬 검색 흔적 — 종류와 앞말을 섹터에서 받는다. 탐색에 박아 두면
    섹터 쪽이 바뀔 때 자유 씨앗이 로그 없이 사라졌다(2차 리뷰 재현)."""
    from dataclasses import replace

    from app.main import build_explorer
    from src.sectors.interest.digest import SEARCH
    from src.sectors.interest.traces import WEB_VISIT
    from src.storage.conversation import SQLiteConversation
    from src.storage.findings import SQLiteFindings

    settings = replace(_settings(tmp_path), search_base_url="http://search.test")
    loop = build_explorer(
        settings, SQLiteFindings(settings.db_path), SQLiteConversation(settings.db_path)
    )
    assert loop is not None
    explorer = loop.job.__self__  # type: ignore[attr-defined]
    assert (explorer.search_kind, explorer.search_prefix) == (WEB_VISIT.kind, SEARCH)
