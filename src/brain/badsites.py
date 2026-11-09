"""문제를 일으킨 사이트를 30일 기억한다 — 브라우저가 다시 가지 않는다(JAR-036~038 4차 리뷰 뒤).

사용자 결정(2026-11-06): "그 페이지는 그냥 거르게 해". 페이지는 JS 한 줄로 큰 글을 자비스에 밀어
넣을 수 있다(4차 리뷰 재현: 자비스 1.2~1.6GB, 유닛 3~4GB). 열기 전에는 알 길이 없으니 한 번 일으킨
곳을 기억해 다시 안 간다:
  - 안전장치에 걸린 곳(쏟아냄·탭 죽음·이상한 값) — 그 자리에서 적는다(`add`).
  - 자비스가 쓰러진 곳 — 보는 동안 지금 사이트를 표시 파일에 적고(`visiting`), 끝나면 지운다
    (`left`). 다시 켜질 때 표시가 남아 있으면 그 사이트 탓으로 적는다(`recover`).
사이트는 주소의 호스트 이름 그대로다 — 하위 도메인을 묶지 않는다(블로그 하나 때문에 그 서비스
전체를 막지 않게). 방문 기록이라 저장소에 올리지 않는다(.gitignore).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)

DAYS = 30
# 적어 두는 사이트 수 — 넘으면 오래된 것부터 뺀다.
KEEP = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(host: str) -> str:
    return host.strip().lower().rstrip(".")[:253]


@dataclass
class BadSites:
    path: Path
    days: int = DAYS
    now: Callable[[], datetime] = _now
    _visiting: str = field(default="", init=False)

    @property
    def marker(self) -> Path:
        """지금 보는 사이트 표시 — 자비스가 보다 쓰러지면 남는다."""
        return self.path.with_suffix(".now")

    def why(self, host: str) -> Optional[str]:
        """적힌 사이트면 "언제에 무슨 일" 한 줄, 아니면 None."""
        entry = self._load().get(_clean(host))
        if entry is None:
            return None
        return f"{entry['at'][:10]}에 {entry['why']}"

    def hosts(self) -> FrozenSet[str]:
        return frozenset(self._load())

    def add(self, host: str, why: str) -> None:
        host = _clean(host)
        if not host:
            return
        data = self._load()
        data[host] = {"at": self.now().isoformat(), "why": why[:200]}
        for old in sorted(data, key=lambda h: data[h]["at"])[: max(0, len(data) - KEEP)]:
            del data[old]
        self._write(self.path, json.dumps(data, ensure_ascii=False, indent=1))
        logger.warning(
            "문제 사이트로 적었다(%d일 동안 브라우저가 안 간다): %s — %s", self.days, host, why
        )

    def visiting(self, host: str) -> None:
        """지금 보는 사이트를 표시한다. 같은 사이트면 다시 쓰지 않는다."""
        host = _clean(host)
        if not host or host == self._visiting:
            return
        self._visiting = host
        self._write(self.marker, host)

    def left(self) -> None:
        """다 봤다(실패해도 자비스는 살아 있다) — 표시를 지운다."""
        self._visiting = ""
        try:
            self.marker.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            logger.warning(
                "브라우저 표시를 못 지웠다(다음에 켜질 때 그 사이트를 탓한다): %s", error
            )

    def recover(self) -> Optional[str]:
        """켜질 때 — 표시가 남아 있으면 지난번에 그 사이트를 보다 쓰러진 것이다. 적고 지운다."""
        try:
            host = self.marker.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as error:
            logger.warning("브라우저 표시를 못 읽었다: %s", error)
            return None
        self.left()
        if not host:
            return None
        self.add(host, "자비스가 이 사이트를 보다 쓰러졌다")
        return _clean(host)

    def _load(self) -> Dict[str, Dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("문제 사이트 목록을 못 읽었다 — 빈 목록으로 본다: %s", error)
            return {}
        if not isinstance(data, dict):
            logger.warning("문제 사이트 목록의 꼴이 이상하다 — 빈 목록으로 본다")
            return {}
        cutoff = self.now() - timedelta(days=self.days)
        kept: Dict[str, Dict[str, str]] = {}
        for host, entry in data.items():
            if not isinstance(host, str) or not isinstance(entry, dict):
                continue
            try:
                at = datetime.fromisoformat(str(entry.get("at")))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if at >= cutoff:
                kept[_clean(host)] = {"at": at.isoformat(), "why": str(entry.get("why", ""))[:200]}
        return kept

    def _write(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
        except OSError as error:
            logger.warning("문제 사이트 파일을 못 썼다: %s — %s", path, error)
