from src.sectors.money.context import SpendingProvider
from src.sectors.money.traces import TRACES

# 보기만 한다 — 돈을 움직이는 기능은 없고 만들지 않는다(사용자 결정, 2026-11-11 스프린트 계획).
# 지금은 쓴 돈을 받아 맥락에 보이기만 해서 지표·트리거·사건이 없다. 섹터마다 모양이 같아야
# app/main.py 가 순회할 수 있다.
METRICS: list = []
TRIGGERS: list = []
EVENTS: list = []
EVENT_TRIGGERS: list = []

__all__ = ["EVENTS", "EVENT_TRIGGERS", "METRICS", "SpendingProvider", "TRACES", "TRIGGERS"]
