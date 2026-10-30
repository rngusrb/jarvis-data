from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.models import Observation
from src.storage.sqlite import SQLiteStore

KST = timezone(timedelta(hours=9))
NIGHT = datetime(2026, 8, 19, tzinfo=KST)


def _observation(value: float, at: datetime, kind: str = "sleep_hours") -> Observation:
    return Observation(source="apple_health", kind=kind, value=value, at=at, meta={"segments": 3})


def test_쓰고_읽는다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.5, NIGHT)])
    found = store.recent("sleep_hours", NIGHT - timedelta(days=1))
    assert len(found) == 1
    assert found[0].value == 7.5
    assert found[0].meta["segments"] == 3


def test_같은_날짜를_두_번_넣어도_한_줄이다(tmp_path: Path) -> None:
    """단축어가 두 번 울리거나 export를 다시 넣어도 중복되면 안 된다."""
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.5, NIGHT)])
    store.write([_observation(7.5, NIGHT)])
    assert store.count() == 1


def test_다시_넣으면_최신값으로_덮인다(tmp_path: Path) -> None:
    """워치가 늦게 동기화돼서 값이 갱신되는 경우."""
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.5, NIGHT)])
    store.write([_observation(8.2, NIGHT)])
    assert store.recent("sleep_hours", NIGHT - timedelta(days=1))[0].value == 8.2


def test_시간대_정보가_왕복해도_살아남는다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.5, NIGHT)])
    restored = store.recent("sleep_hours", NIGHT - timedelta(days=1))[0]
    assert restored.at == NIGHT
    assert restored.at.tzinfo is not None


def test_기간_밖의_데이터는_안_가져온다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.0, NIGHT - timedelta(days=30)), _observation(8.0, NIGHT)])
    found = store.recent("sleep_hours", NIGHT - timedelta(days=7))
    assert len(found) == 1
    assert found[0].value == 8.0


def test_다른_종류는_섞이지_않는다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(7.0, NIGHT), _observation(8000, NIGHT, kind="step_count")])
    assert len(store.recent("sleep_hours", NIGHT - timedelta(days=1))) == 1
    assert store.kinds() == ["sleep_hours", "step_count"]


def test_시간순으로_돌려준다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    store.write(
        [
            _observation(6.0, NIGHT),
            _observation(7.0, NIGHT - timedelta(days=2)),
            _observation(8.0, NIGHT - timedelta(days=1)),
        ]
    )
    values = [o.value for o in store.recent("sleep_hours", NIGHT - timedelta(days=7))]
    assert values == [7.0, 8.0, 6.0]


def test_잘린_기록이_온전한_기록을_덮지_않는다(tmp_path: Path) -> None:
    """수집 창이 밀리면 어젯밤이 반토막 난 채 다시 온다.

    단축어의 "최근 1일"은 24시간 롤링이라, 어제보다 일찍 깬 날엔 창의 시작
    경계가 어젯밤 한가운데를 지나간다. 그 조각이 온전한 밤을 덮으면 과거가
    조용히 줄어든다.
    """
    from src.core.metrics import Conflict

    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(8.0, NIGHT)], on_conflict=Conflict.KEEP_LARGER)
    store.write([_observation(0.5, NIGHT)], on_conflict=Conflict.KEEP_LARGER)

    assert store.recent("sleep_hours", NIGHT - timedelta(days=1))[0].value == 8.0


def test_더_완전한_기록은_갱신한다(tmp_path: Path) -> None:
    from src.core.metrics import Conflict

    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(0.5, NIGHT)], on_conflict=Conflict.KEEP_LARGER)
    store.write([_observation(8.0, NIGHT)], on_conflict=Conflict.KEEP_LARGER)

    stored = store.recent("sleep_hours", NIGHT - timedelta(days=1))[0]
    assert stored.value == 8.0
    assert stored.meta["segments"] == 3  # meta 도 같이 갱신된다


def test_백필은_짧은_값으로도_바로잡는다(tmp_path: Path) -> None:
    """export.xml 은 항상 완전하므로, 잘못 부풀려진 값을 되돌릴 수 있어야 한다."""
    from src.core.metrics import Conflict

    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(99.0, NIGHT)])
    store.write([_observation(8.0, NIGHT)], on_conflict=Conflict.REPLACE)

    assert store.recent("sleep_hours", NIGHT - timedelta(days=1))[0].value == 8.0


def test_기간은_표기가_아니라_순간으로_자른다(tmp_path: Path) -> None:
    """관측치는 +09:00 로 남고 루프는 UTC 로 묻는다. 글자로 비교하면 9시간이 어긋난다.

    한국 시각 30일 07:00 은 UTC 로 29일 22:00 이다. 29일 23:00(UTC) 이후를 물으면
    빠져야 하는데, 글자로는 "2026-10-30T07" 이 "2026-10-29T23" 보다 뒤라 끼어 들어왔다.
    """
    store = SQLiteStore(tmp_path / "t.db")
    store.write([_observation(1.0, datetime(2026, 10, 30, 7, 0, tzinfo=KST))])

    found = store.recent("sleep_hours", datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc))

    assert found == []


def test_마지막_수신은_표기가_달라도_가장_늦은_순간이다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    later = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)  # 한국 시각 10:00
    earlier = datetime(2026, 10, 30, 9, 0, tzinfo=KST)  # UTC 00:00
    store.write([_observation(1.0, earlier), _observation(2.0, later)])

    assert store.last_seen()["sleep_hours"] == later


def test_표기가_섞여도_시간_순서로_돌려준다(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "t.db")
    # 글자로는 "…T10:00+09:00"(= 01:00Z)이 "…T02:00+00:00" 보다 뒤지만 순간으로는 앞이다.
    early = datetime(2026, 10, 30, 10, 0, tzinfo=KST)
    late = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)
    store.write([_observation(2.0, late), _observation(1.0, early)])

    found = store.recent("sleep_hours", datetime(2026, 10, 29, tzinfo=timezone.utc))

    assert [o.value for o in found] == [1.0, 2.0]
