"""파일 읽기 — 웹에서 받은 파일(PDF, 한글, 워드, 엑셀, 파워포인트, 글)에서 글만 뽑는다.

사용자 요청(2026-11-06): "어떤 파일이든 열어 볼 수 있는 리더". 실제 대화에서 큐넷 "3회 실기
원서접수 안내"의 본문은 비어 있고 일정은 첨부 PDF·HWP 에만 있었다 — 읽을 수 없으니 "첨부에
있을 텐데 못 열었어"로 끝났다.

**글만 뽑고 실행하지 않는다.** 받은 파일은 남이 만든 것이다:
  - 크기는 읽는 쪽(`web.PageReader`)이 받으며 끊는다. 압축 파일(zip 꼴 문서)은 풀었을 때 크기도
  본다.
  - PDF 는 앞쪽 몇 쪽만. 분석은 부르는 쪽이 스레드에서 돌린다(큰 파일이 자비스 전체를 멈추지 않게).
  - 매크로·스크립트·외부 참조는 읽지도 따라가지도 않는다.

종류는 내용의 첫 바이트로 먼저 본다 — 큐넷은 PDF 를 `application/octet-stream` 으로 준다.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from typing import Iterable, List, Optional
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

# 읽는 종류. 이름이 곧 `Page` 의 제목 옆에 붙는다.
PDF, HWP, HWPX, DOCX, XLSX, PPTX, TEXT, HTML = (
    "pdf",
    "hwp",
    "hwpx",
    "docx",
    "xlsx",
    "pptx",
    "text",
    "html",
)
FILES = (PDF, HWP, HWPX, DOCX, XLSX, PPTX)
_EXTENSIONS = {
    ".pdf": PDF,
    ".hwp": HWP,
    ".hwpx": HWPX,
    ".docx": DOCX,
    ".xlsx": XLSX,
    ".pptx": PPTX,
    ".txt": TEXT,
    ".csv": TEXT,
    ".json": TEXT,
    ".md": TEXT,
}
# 압축 문서를 풀었을 때 읽을 최대 크기. 몇 KB 가 수 GB 로 풀리는 파일이 있다.
UNZIPPED = 30_000_000
# PDF 는 앞쪽만 — 일정·안내는 보통 앞에 있다.
PDF_PAGES = 30
# 엑셀은 시트마다 앞쪽 줄만.
SHEET_ROWS = 300


class Unreadable(ValueError):
    """읽을 수 없는 파일(모르는 종류, 암호, 깨짐)."""


def name_of(url: str, disposition: str = "") -> str:
    """파일 이름 — Content-Disposition 이 먼저, 없으면 주소의 끝."""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if not match:
        match = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1)).strip()
    path = urlsplit(url).path
    return unquote(path.rsplit("/", 1)[-1])


def kind_of(head: bytes, content_type: str, name: str) -> str:
    """종류. 첫 바이트 → 응답 종류 → 이름의 확장자 순으로 본다. 모르면 빈 문자열."""
    lowered = name.lower()
    by_name = next((k for ext, k in _EXTENSIONS.items() if lowered.endswith(ext)), "")
    if head.startswith(b"%PDF"):
        return PDF
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE 묶음 — 한글(.hwp)과 옛 오피스(.doc/.xls/.ppt)가 같은 꼴이다. 옛 오피스는 읽지 않는다.
        # 이름이 없거나 다른 것이면 한글로 열어 보고, 아니면 FileHeader 에서 시끄럽게 실패한다.
        return "" if lowered.endswith((".doc", ".xls", ".ppt", ".msg")) else HWP
    if head.startswith(b"PK\x03\x04"):
        return by_name if by_name in (HWPX, DOCX, XLSX, PPTX) else "zip"
    kind = content_type.lower()
    if "html" in kind:
        return HTML
    if kind.startswith("text/") or "json" in kind or "csv" in kind:
        return TEXT
    if "pdf" in kind:
        return PDF
    return by_name


def text_of(data: bytes, kind: str) -> str:
    """파일에서 글만. 못 읽으면 `Unreadable`(이유를 담아)."""
    try:
        if kind == PDF:
            return _pdf(data)
        if kind == HWP:
            return _hwp(data)
        if kind in (HWPX, DOCX, XLSX, PPTX, "zip"):
            return _zipped(data, kind)
    except Unreadable:
        raise
    except Exception as error:  # 남이 만든 파일이다 — 어떤 식으로든 깨져 있을 수 있다.
        raise Unreadable(f"{kind} 를 못 읽었다: {type(error).__name__}: {error}") from error
    raise Unreadable(f"읽을 수 없는 종류다: {kind or '모름'}")


# ── PDF ──────────────────────────────────────────


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")  # 빈 암호로 잠근 공문이 흔하다
        except Exception as error:
            raise Unreadable(f"암호가 걸린 PDF 다: {error}") from error
    pages = []
    for number, page in enumerate(reader.pages[:PDF_PAGES], 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[{number}쪽]\n{text}")
    if not pages:
        raise Unreadable("PDF 에 글이 없다(스캔한 그림일 수 있다)")
    return "\n".join(pages)


# ── 한글 HWP 5.0 ──────────────────────────────────

# 문단 글 레코드. 나머지(글자 모양, 표 칸 정보 등)는 건너뛴다.
_PARA_TEXT = 67
# 글 안의 조종 문자. 이 번호들은 뒤에 7자리(14바이트)를 더 달고 온다(표·그림·각주 같은 개체).
_LONG_CONTROLS = set(range(1, 10)) | {11, 12} | set(range(14, 24))


def _hwp(data: bytes) -> str:
    import olefile

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if not ole.exists("FileHeader"):
            raise Unreadable("한글 파일이 아니다(FileHeader 없음)")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise Unreadable("한글 파일이 아니다")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 0x2:
            raise Unreadable("암호가 걸린 한글 파일이다")
        if flags & 0x4:
            raise Unreadable("배포용 한글 파일이다(본문이 암호화돼 있다)")
        compressed = bool(flags & 0x1)
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"),
            key=lambda e: int(re.sub(r"\D", "", e[1]) or 0),
        )
        return hwp_text((ole.openstream(e).read() for e in sections), compressed)


def hwp_text(sections: Iterable[bytes], compressed: bool) -> str:
    """한글 본문 구역들에서 문단 글만. 레코드 머리(4바이트): 태그 10비트·깊이 10비트·크기 12비트."""
    paragraphs: List[str] = []
    for raw in sections:
        body = zlib.decompress(raw, -15) if compressed else raw
        at = 0
        while at + 4 <= len(body):
            head = struct.unpack_from("<I", body, at)[0]
            tag, size = head & 0x3FF, (head >> 20) & 0xFFF
            at += 4
            if size == 0xFFF:
                size = struct.unpack_from("<I", body, at)[0]
                at += 4
            if tag == _PARA_TEXT:
                line = _hwp_line(body[at : at + size])
                if line.strip():
                    paragraphs.append(line.strip())
            at += size
    return "\n".join(paragraphs)


def _hwp_line(raw: bytes) -> str:
    units = struct.unpack(f"<{len(raw) // 2}H", raw[: len(raw) // 2 * 2])
    out: List[str] = []
    i = 0
    while i < len(units):
        unit = units[i]
        if unit in _LONG_CONTROLS:
            i += 8
            continue
        if unit < 32:
            out.append(" " if unit in (9, 10, 13, 24, 30, 31) else "")
            i += 1
            continue
        out.append(chr(unit))
        i += 1
    return re.sub(r"\s+", " ", "".join(out))


# ── zip 꼴 문서 (HWPX, DOCX, XLSX, PPTX) ─────────────


def _zipped(data: bytes, kind: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if kind == "zip":
            kind = _zip_kind(names)
        if kind == HWPX:
            parts = sorted(
                (n for n in names if re.match(r"Contents/section\d+\.xml$", n)), key=_number
            )
            return "\n".join(_paragraphs(_xml(zf, n), "p", "t") for n in parts)
        if kind == DOCX:
            return _paragraphs(_xml(zf, "word/document.xml"), "p", "t", rows=("tr", "tc"))
        if kind == PPTX:
            slides = sorted(
                (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=_number
            )
            return "\n".join(
                f"[슬라이드 {i}]\n{_paragraphs(_xml(zf, n), 'p', 't')}"
                for i, n in enumerate(slides, 1)
            )
        if kind == XLSX:
            return _xlsx(zf, names)
    raise Unreadable(f"읽을 수 없는 압축 문서다: {kind}")


def _zip_kind(names: List[str]) -> str:
    if "word/document.xml" in names:
        return DOCX
    if "xl/workbook.xml" in names:
        return XLSX
    if "ppt/presentation.xml" in names:
        return PPTX
    if any(n.startswith("Contents/section") for n in names):
        return HWPX
    raise Unreadable("모르는 압축 파일이다")


def _xml(zf: zipfile.ZipFile, name: str) -> ElementTree.Element:
    info = zf.getinfo(name)
    if info.file_size > UNZIPPED:
        raise Unreadable(f"풀면 너무 크다: {name} {info.file_size}바이트")
    return ElementTree.fromstring(zf.read(name))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragraphs(
    root: ElementTree.Element, para: str, text: str, rows: Optional[tuple] = None
) -> str:
    """문단(`para`)마다 한 줄, 글(`text`) 조각을 잇는다. 표(`rows` = (행, 칸))는 한 행을 한 줄로."""
    lines: List[str] = []

    def walk(node: ElementTree.Element) -> None:
        tag = _local(node.tag)
        if rows and tag == rows[0]:
            cells = [
                " ".join("".join(_texts(p, text)) for p in c.iter() if _local(p.tag) == para)
                for c in node
                if _local(c.tag) == rows[1]
            ]
            line = " | ".join(c.strip() for c in cells)
            if line.strip(" |"):
                lines.append(line)
            return
        if tag == para:
            line = "".join(_texts(node, text)).strip()
            if line:
                lines.append(line)
            return
        for child in node:
            walk(child)

    walk(root)
    return "\n".join(lines)


def _texts(node: ElementTree.Element, text: str) -> List[str]:
    return [t.text or "" for t in node.iter() if _local(t.tag) == text]


def _xlsx(zf: zipfile.ZipFile, names: List[str]) -> str:
    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in _xml(zf, "xl/sharedStrings.xml"):
            shared.append("".join(_texts(si, "t")))
    sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=_number)
    out: List[str] = []
    for i, name in enumerate(sheets, 1):
        rows: List[str] = []
        for row in _xml(zf, name).iter():
            if _local(row.tag) != "row":
                continue
            cells = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                value = next((v.text or "" for v in c if _local(v.tag) == "v"), "")
                if c.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                elif c.get("t") == "inlineStr":
                    value = "".join(_texts(c, "t"))
                cells.append(value.strip())
            line = " | ".join(cells)
            if line.strip(" |"):
                rows.append(line)
            if len(rows) >= SHEET_ROWS:
                break
        if rows:
            out.append(f"[시트 {i}]\n" + "\n".join(rows))
    return "\n".join(out)


def _number(name: str) -> int:
    digits = re.findall(r"\d+", name)
    return int(digits[-1]) if digits else 0
