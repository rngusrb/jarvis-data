"""모든 시험에 공통 — 밖의 네트워크를 타지 않게."""

from __future__ import annotations

from typing import Iterator

import pytest

from src.brain import web

# 시험에서 "이 집의 공인 주소"(NAT 헤어핀으로 막는 주소). 시험 주소들과 겹치지 않는 공개 주소.
OWN_ADDRESS = "1.2.3.4"


@pytest.fixture(autouse=True)
def _own_address() -> Iterator[None]:
    """`web.checked` 는 이 집의 공인 주소를 알아야 붙는다 — 시험은 묻지 않고 정해 둔 값을 쓴다."""
    before = web.OWN.fixed
    web.OWN.fixed = OWN_ADDRESS
    yield
    web.OWN.fixed = before
