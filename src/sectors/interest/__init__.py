from src.sectors.interest.traces import TRACES

# 아직 지표도 트리거도 없다. 흔적만 모으는 단계라 빈 목록을 내보낸다 —
# app/main.py 가 모든 섹터를 같은 모양으로 다루게 하려는 것이다.
METRICS: list = []
TRIGGERS: list = []

# 사건도 아직 없다. 모양을 맞추려고 빈 목록을 내보낸다.
EVENTS: list = []

__all__ = ["EVENTS", "METRICS", "TRACES", "TRIGGERS"]
