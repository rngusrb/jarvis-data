"""채널 계약.

여기가 이 프로젝트에서 제일 중요한 이음매다. 지금은 텔레그램으로 내보내지만
곧 iOS 푸시/단축어로 갈아탈 예정이고, 그때 위쪽 레이어(runtime/loop.py)는
단 한 줄도 바뀌면 안 된다. 그래서 채널이 할 줄 아는 일을 send() 하나로 묶어둔다.

듣는 쪽은 **따로 뒀다**. 모든 채널이 양방향일 필요가 없다 — 콘솔은 받을 게
없고, iOS 푸시도 받는 경로는 완전히 다르다. Channel 에 receive() 를 끼워넣으면
그런 채널들이 쓰지도 않을 메서드를 구현하게 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Protocol


class Channel(Protocol):
    name: str

    async def send(self, text: str) -> None:
        """사용자에게 메시지를 밀어넣는다. 실패하면 예외를 올린다."""
        ...


@dataclass(frozen=True)
class Incoming:
    """사용자가 보낸 것 하나.

    채널이 뭐든 이 모양으로 떨어진다. 텔레그램의 update 구조가 위쪽 레이어로
    새면 채널을 갈아탈 때 전부 고쳐야 한다.
    """

    text: str
    at: datetime
    # 채널이 다음에 어디서부터 읽을지 기억하는 값. 텔레그램은 update_id 다.
    cursor: int = 0
    # 버튼을 눌렀을 때 그 버튼이 들고 있던 값. 일반 메시지면 비어 있다.
    action: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_action(self) -> bool:
        return bool(self.action)


class Listener(Protocol):
    """사용자가 보낸 것을 가져오는 능력. 양방향 채널만 구현한다."""

    async def receive(self, since: int) -> List[Incoming]:
        """`since` 이후에 도착한 것을 가져온다.

        **보낸 사람이 주인인지 채널이 직접 거른다.** 봇 사용자명은 공개라
        아무나 말을 걸 수 있고, 걸러내지 않으면 남이 주인의 건강 데이터를
        물어볼 수 있다. 위쪽 레이어가 챙기게 두면 언젠가 빠뜨린다.
        """
        ...
