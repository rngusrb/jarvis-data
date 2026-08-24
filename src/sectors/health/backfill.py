"""export.xml 백필 — 과거 히스토리를 한 번에 밀어넣는다.

단축어 자동화는 어제치만 가져오므로 baseline이 없다. "평소보다 적게 잤다"를
판단하려면 과거가 있어야 하고, 그건 건강 앱의 전체 내보내기로만 얻을 수 있다.
그래서 이 스크립트는 딱 한 번(또는 가끔) 돌린다.

    python -m app.backfill ~/Downloads/apple_health_export/내보내기.xml
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from src.core.config import load_settings
from src.sectors.health.parser import parse_export
from src.storage.sqlite import SQLiteStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1

    export_path = Path(argv[1]).expanduser()
    if not export_path.exists():
        logger.error("파일이 없다: %s", export_path)
        return 1

    settings = load_settings()
    store = SQLiteStore(settings.db_path)

    logger.info("파싱 시작 (%.1fMB)", export_path.stat().st_size / 1024 / 1024)
    observations = parse_export(export_path)
    logger.info("관측치 %d건 추출", len(observations))

    # 저장소가 (source, kind, at) 기준으로 덮어쓰므로 여러 번 돌려도 안전하다.
    written = store.write(observations)
    logger.info("저장 완료 — %d건, 총 %d건, 종류 %s", written, store.count(), store.kinds())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
