"""구조 불변식 — 플랫폼과 섹터의 경계가 무너지지 않았는지.

이 파일은 코드가 도는지가 아니라 **구조가 유지되는지**를 본다. 한 방향 규칙을
문장으로만 두면 석 달 뒤에 누군가(작성자 포함) 무심코 어긴다. 그때 조용히
어긋나는 대신 여기서 실패하게 만든다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[2]

# 이 폴더들은 섹터가 뭐든 안 바뀐다. 섹터를 알기 시작하면 섹터를 늘릴 때마다
# 플랫폼도 고쳐야 하고, 갈라놓은 의미가 사라진다.
PLATFORM_DIRS = ("src/core", "src/storage", "src/brain", "src/channels", "src/parsers")

# app/은 조립 지점이다 — "어떤 섹터를 켤지" 고르는 유일한 곳이라 예외.
COMPOSITION_ROOT = "app/main.py"


def _imports(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


def _python_files(*folders: str) -> List[Path]:
    return [p for folder in folders for p in (ROOT / folder).rglob("*.py")]


def test_플랫폼은_섹터를_모른다() -> None:
    offenders = []
    for path in _python_files(*PLATFORM_DIRS):
        if any(m.startswith("src.sectors") for m in _imports(path)):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"플랫폼이 섹터를 import 했다: {offenders}\n"
        f"방향은 섹터→플랫폼 하나뿐이다. 조립이 필요하면 {COMPOSITION_ROOT}에서 한다."
    )


def test_배선만_섹터를_고른다() -> None:
    """app/ 안에서도 섹터를 아는 파일은 하나여야 한다."""
    knowers = sorted(
        str(p.relative_to(ROOT))
        for p in _python_files("app")
        if any(m.startswith("src.sectors") for m in _imports(p))
    )
    assert knowers in ([], [COMPOSITION_ROOT]), (
        f"섹터를 아는 파일: {knowers} — {COMPOSITION_ROOT} 하나만 허용된다"
    )


def test_수신구는_지표_이름을_모른다() -> None:
    """지표 이름이 수신구에 박히면 섹터가 늘 때마다 이 파일이 자란다."""
    source = ast.parse((ROOT / "app/ingest.py").read_text())
    literals = {
        node.value
        for node in ast.walk(source)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    from src.sectors.health import METRICS

    leaked = sorted(m.kind for m in METRICS if m.kind in literals)
    assert not leaked, f"수신구에 지표 이름이 박혀 있다: {leaked} — 카드에서 읽어야 한다"


def test_섹터는_다른_섹터를_모른다() -> None:
    """섹터끼리 얽히면 하나를 떼어낼 수 없게 된다."""
    sectors_dir = ROOT / "src/sectors"
    offenders = []
    for sector in sorted(p for p in sectors_dir.iterdir() if p.is_dir()):
        for path in sector.rglob("*.py"):
            for module in _imports(path):
                if module.startswith("src.sectors.") and not module.startswith(
                    f"src.sectors.{sector.name}"
                ):
                    offenders.append(f"{path.relative_to(ROOT)} → {module}")
    assert not offenders, f"섹터끼리 참조: {offenders}"
