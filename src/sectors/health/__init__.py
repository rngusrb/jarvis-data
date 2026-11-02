from src.sectors.health.metrics import METRICS
from src.sectors.health.parser import parse_export
from src.sectors.health.triggers import TRIGGERS

# 아직 텍스트 흔적을 안 모은다. 섹터마다 모양이 같아야 app/main.py 가
# 특별 취급 없이 순회할 수 있다.
TRACES: list = []

# 사건도 아직 없다. 모양을 맞추려고 빈 목록을 내보낸다.
EVENTS: list = []

__all__ = ["EVENTS", "METRICS", "TRACES", "TRIGGERS", "parse_export"]
