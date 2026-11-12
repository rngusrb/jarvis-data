"""모든 시험에 공통 — 밖의 네트워크를 타지 않게, 이 시험 프로세스의 자원 상한을 건드리지 않게."""

from __future__ import annotations

from typing import Iterator

import pytest

from src.brain import web
from src.core.days import set_holidays

# 시험에서 "이 집의 공인 주소"(NAT 헤어핀으로 막는 주소). 시험 주소들과 겹치지 않는 공개 주소.
OWN_ADDRESS = "1.2.3.4"


@pytest.fixture(autouse=True)
def _own_address() -> Iterator[None]:
    """`web.checked` 는 이 집의 공인 주소를 알아야 붙는다 — 시험은 묻지 않고 정해 둔 값을 쓴다."""
    before = web.OWN.fixed
    web.OWN.fixed = OWN_ADDRESS
    yield
    web.OWN.fixed = before


@pytest.fixture(autouse=True)
def _limits_untouched() -> Iterator[None]:
    """시험이 이 시험 프로세스의 자원 상한(메모리·CPU)을 바꾸지 않았는지 본다.

    사고 이력: 2026-11-09 PR #55 CI. 파일 리더 시험 하나가 자식용 `files._child()` 를 시험 프로세스
    안에서 그대로 불러 메모리 1GB·CPU 30초 상한이 시험 프로세스에 걸렸다. 뒤에 뜬 Playwright
    드라이버·크롬이 그 상한을 물려받아 바로 죽어 브라우저 시험 32개가 "Connection closed while
    reading from the driver" 로 실패했다. 맥은 메모리 상한이 안 먹어 안 보였다. 낮춘 상한은
    되돌릴 수 없어(권한 없는 프로세스) 바꾼 그 시험을 실패시킨다.
    """
    import resource

    kinds = (resource.RLIMIT_AS, resource.RLIMIT_DATA, resource.RLIMIT_CPU)
    before = [resource.getrlimit(kind) for kind in kinds]
    yield
    after = [resource.getrlimit(kind) for kind in kinds]
    assert after == before, (
        f"시험이 이 프로세스의 자원 상한을 바꿨다({before} → {after}) — setrlimit 을 막고 부른다"
    )


@pytest.fixture(autouse=True)
def _no_holidays() -> Iterator[None]:
    """공휴일 표는 프로세스에 하나다(`core/days`) — 시험마다 비우고 시작해 서로 섞이지 않게."""
    set_holidays({})
    yield
    set_holidays({})
