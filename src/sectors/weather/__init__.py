from src.sectors.weather.metrics import METRICS
from src.sectors.weather.triggers import FAMILY, TRIGGERS

# 바깥의 사실(예보·공휴일)만 다룬다 — 흔적·사건이 없다. 섹터마다 모양이 같아야 app/main.py 가
# 특별 취급 없이 순회할 수 있다.
TRACES: list = []
EVENTS: list = []
EVENT_TRIGGERS: list = []

__all__ = ["EVENTS", "EVENT_TRIGGERS", "FAMILY", "METRICS", "TRACES", "TRIGGERS"]
