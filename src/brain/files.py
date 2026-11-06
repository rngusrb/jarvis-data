"""파일 읽기 — 웹에서 받은 파일(PDF, 한글, 워드, 엑셀, 파워포인트, 글)에서 글만 뽑는다.

사용자 요청(2026-11-06): "어떤 파일이든 열어 볼 수 있는 리더". 실제 대화에서 큐넷 "3회 실기
원서접수 안내"의 본문은 비어 있고 일정은 첨부 PDF·HWP 에만 있었다 — 읽을 수 없으니 "첨부에
있을 텐데 못 열었어"로 끝났다.

**받은 파일은 남이 만든 것이다.** 글만 뽑고 실행하지 않는다. 그리고:
  - 분석은 **별도 프로세스**에서 한다(`extract`) — 메모리·CPU 상한을 걸고, 시간을 넘기면 끝낸다.
    스레드로 돌렸더니 시간 초과가 나도 스레드는 끝까지 돌았고, 몇 KB 짜리 압축 폭탄 하나가
    서버 메모리를 다 썼다(JAR-035 리뷰 재현: 0.3MB HWP 가 666MB, 9KB OLE 가 2.5GB 상한까지).
  - 그 안에서도 푸는 크기를 센다 — HWP 는 조금씩 풀며, zip 은 안전한 압축 방식만 조금씩 읽으며,
    OLE 는 크기를 먼저 보고. zip 의 "풀면 몇 바이트"는 파일이 스스로 적은 값이라 믿지 않는다.
  - 자식 프로세스에는 비밀값이 든 환경변수를 넘기지 않는다.

종류는 내용의 첫 바이트로 먼저 본다 — 큐넷은 PDF 를 `application/octet-stream` 으로 준다.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

# 읽는 종류.
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
EXTENSIONS = {
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
# 풀었을 때 읽을 최대 크기(파일 하나 전체). 몇 KB 가 수 GB 로 풀리는 파일이 있다.
UNZIPPED = 30_000_000
# PDF 는 앞쪽만 — 일정·안내는 보통 앞에 있다.
PDF_PAGES = 30
# 엑셀은 시트마다 앞쪽 줄만.
SHEET_ROWS = 300
# 자식 프로세스가 돌려줄 글의 상한. 읽는 쪽이 어차피 4천 자로 자른다.
TEXT_CAP = 50_000
# 자식 프로세스의 상한(리눅스에서 걸린다). 서버는 가용 메모리 4.7GB, 스왑 99%(리뷰 실측).
CHILD_MEMORY = 1_000_000_000
CHILD_CPU = 30
# 한꺼번에 띄우는 자식 수. 대화·관심사 준비·보고서가 겹쳐도 둘을 넘지 않게.
SLOTS = 2
_slots: Optional[asyncio.Semaphore] = None
# zip 꼴 문서에서 받는 압축 방식. bzip2·lzma 는 풀 길이를 정할 수 없어 받지 않는다(리뷰 재현).
_ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


class Unreadable(ValueError):
    """읽을 수 없는 파일(모르는 종류, 암호, 깨짐, 상한 초과)."""


# ── 이름과 종류 ──────────────────────────────────


def name_of(url: str, disposition: str = "") -> str:
    """파일 이름 — Content-Disposition 이 먼저, 없으면 주소의 끝."""
    star = re.search(r"filename\*\s*=\s*([\w-]+)'[^']*'([^;]+)", disposition, re.IGNORECASE)
    plain = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.IGNORECASE)
    if star:
        value = star.group(2).strip().strip('"')
        name = unquote(value, encoding=_codec(star.group(1)), errors="replace")
    elif plain:
        name = _mojibake(unquote(plain.group(1).strip()))
    else:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    # 폼 꼴로 공백을 + 로 적는 사이트가 있다(목포해양대 실측 "2026년+일정.pdf").
    if "+" in name and " " not in name:
        name = name.replace("+", " ")
    return name.strip()


def _codec(charset: str) -> str:
    charset = charset.lower()
    return "cp949" if charset in ("euc-kr", "ks_c_5601-1987", "cp949") else charset


def _mojibake(name: str) -> str:
    """헤더의 날바이트 EUC-KR 이름은 latin-1 로 풀려 들어온다('ÀÏÁ¤.pdf'). 되돌린다."""
    if not name or any(ord(c) > 0xFF for c in name) or all(ord(c) < 0x80 for c in name):
        return name
    raw = name.encode("latin-1")
    for codec in ("utf-8", "cp949"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return name


def kind_of(head: bytes, content_type: str, name: str) -> str:
    """종류. 첫 바이트 → 응답 종류 → 이름의 확장자 순으로 본다. 모르면 빈 문자열."""
    lowered = name.lower()
    by_name = next((k for ext, k in EXTENSIONS.items() if lowered.endswith(ext)), "")
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


# ── 별도 프로세스에서 ─────────────────────────────


async def extract(
    data: bytes, kind: str, timeout: float = 25.0, command: Optional[Sequence[str]] = None
) -> str:
    """자식 프로세스에서 글을 뽑는다. 메모리·CPU 상한, 시간을 넘기면 끝낸다. 못 읽으면 `Unreadable`.

    `command` 는 시험이 자식을 바꿔 끼울 때만 쓴다.
    """
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(SLOTS)
    async with _slots:
        argv = list(command) if command else [sys.executable, str(Path(__file__).resolve()), kind]
        child = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 비밀값(봇 토큰 등)이 든 환경을 넘기지 않는다 — 남이 만든 파일을 여는 쪽이다.
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        try:
            out, err = await asyncio.wait_for(child.communicate(data), timeout=timeout)
        except asyncio.TimeoutError:
            raise Unreadable(f"글을 뽑는 데 {timeout:.0f}초를 넘겨 멈췄다") from None
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    if child.returncode != 0 or not out:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise Unreadable(f"글을 뽑다 멈췄다(종료 {child.returncode}): {tail[0][:200]}")
    try:
        result = json.loads(out.decode("utf-8"))
    except ValueError as error:
        raise Unreadable(f"자식이 이상한 답을 줬다: {error}") from error
    if "error" in result:
        raise Unreadable(str(result["error"]))
    return str(result["text"])


def _child() -> None:
    """자식 프로세스: 상한을 걸고, 표준입력의 파일에서 글을 뽑아 JSON 으로 돌려준다."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY, CHILD_MEMORY))
        resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU, CHILD_CPU + 1))
    except (ImportError, ValueError, OSError) as error:
        # 맥은 RLIMIT_AS 를 안 받는다 — 시간 상한(부모가 끝낸다)만 남는다. 서버는 리눅스다.
        print(f"상한을 못 걸었다: {error}", file=sys.stderr)
    data = sys.stdin.buffer.read()
    try:
        result = {"text": text_of(data, sys.argv[1])[:TEXT_CAP]}
    except Unreadable as error:
        result = {"error": str(error)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


def text_of(data: bytes, kind: str) -> str:
    """파일에서 글만(이 프로세스 안에서). 못 읽으면 `Unreadable`(이유를 담아)."""
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
    from pypdf import PasswordType, PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # 빈 암호로 잠근 공문이 흔하다. 틀린 암호여도 예외 없이 "못 풀었다"를 돌려준다(리뷰).
        if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise Unreadable("암호가 걸린 PDF 다")
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
# 8칸짜리 조종 문자(표·그림·각주 같은 개체, 탭은 9). 뒤 7칸을 같이 건너뛴다.
_LONG_CONTROLS = set(range(1, 10)) | {11, 12} | set(range(14, 24))
# 공백이 되는 조종 문자(탭, 줄바꿈, 문단 끝, 하이픈·띄어쓰기 류).
_SPACES = {9, 10, 13, 24, 30, 31}


def _hwp(data: bytes) -> str:
    import olefile

    # 잘못된 구조(자기 자신을 가리키는 FAT 등)는 바로 실패하게 — 기본값은 넘겨 버린다(리뷰 재현).
    with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        if not ole.exists("FileHeader"):
            raise Unreadable("한글 파일이 아니다(FileHeader 없음)")
        header = _stream(ole, ["FileHeader"], 4096)
        if not header.startswith(b"HWP Document File"):
            raise Unreadable("한글 파일이 아니다")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 0x2:
            raise Unreadable("암호가 걸린 한글 파일이다")
        if flags & 0x4:
            raise Unreadable("배포용 한글 파일이다(본문이 암호화돼 있다)")
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"),
            key=lambda e: int(re.sub(r"\D", "", e[1]) or 0),
        )
        return hwp_text((_stream(ole, e, UNZIPPED) for e in sections), bool(flags & 0x1))


def _stream(ole: object, entry: List[str], limit: int) -> bytes:
    size = ole.get_size(entry)  # type: ignore[attr-defined]
    if size > limit:
        raise Unreadable(f"한글 파일의 구역이 너무 크다: {'/'.join(entry)} {size}바이트")
    return bytes(ole.openstream(entry).read(limit))  # type: ignore[attr-defined]


def hwp_text(sections: Iterable[bytes], compressed: bool, limit: int = UNZIPPED) -> str:
    """한글 본문 구역들에서 문단 글만. 레코드 머리(4바이트): 태그 10비트·깊이 10비트·크기 12비트.

    압축은 조금씩 풀며 센다 — 한 번에 풀면 0.3MB 가 666MB 가 됐다(리뷰 재현). 끝이 잘린 레코드는
    거기까지만 쓴다(앞 문단까지 버리지 않는다).
    """
    paragraphs: List[str] = []
    left = limit
    for raw in sections:
        if compressed:
            inflate = zlib.decompressobj(-15)
            body = inflate.decompress(raw, max(left, 0) + 1)
            if len(body) > left or inflate.unconsumed_tail:
                raise Unreadable(f"한글 본문이 풀면 {limit}바이트를 넘는다")
        else:
            body = raw[: left + 1]
            if len(body) > left:
                raise Unreadable(f"한글 본문이 {limit}바이트를 넘는다")
        left -= len(body)
        at = 0
        while at + 4 <= len(body):
            head = struct.unpack_from("<I", body, at)[0]
            tag, size = head & 0x3FF, (head >> 20) & 0xFFF
            at += 4
            if size == 0xFFF:
                if at + 4 > len(body):
                    break
                size = struct.unpack_from("<I", body, at)[0]
                at += 4
            if tag == _PARA_TEXT:
                line = _hwp_line(body[at : at + size])
                if line.strip():
                    paragraphs.append(line.strip())
            at += size
    return "\n".join(paragraphs)


def _hwp_line(raw: bytes) -> str:
    """UTF-16 단위들에서 글만.

    짝 맞는 서로게이트(이모지·확장 한자)는 한 글자로, 짝 없는 것은 �로 — 낱낱이 chr 하면 짝 없는
    서로게이트가 남아 다음 요청을 utf-8 로 못 보냈다(리뷰 재현).
    """
    count = len(raw) // 2
    units = struct.unpack(f"<{count}H", raw[: count * 2])
    kept: List[int] = []
    i = 0
    while i < len(units):
        unit = units[i]
        if unit in _LONG_CONTROLS:
            if unit in _SPACES:
                kept.append(0x20)
            i += 8
            continue
        if unit < 32:
            if unit in _SPACES:
                kept.append(0x20)
            i += 1
            continue
        kept.append(unit)
        i += 1
    text = struct.pack(f"<{len(kept)}H", *kept).decode("utf-16-le", errors="replace")
    return re.sub(r"\s+", " ", text)


# ── zip 꼴 문서 (HWPX, DOCX, XLSX, PPTX) ─────────────


class _Budget:
    """zip 안에서 푼 양을 센다. 파일이 스스로 적은 크기는 믿지 않고 실제로 읽은 만큼 센다."""

    def __init__(self, limit: int) -> None:
        self.left = limit

    def read(self, zf: zipfile.ZipFile, name: str) -> bytes:
        info = zf.getinfo(name)
        if info.compress_type not in _ZIP_METHODS:
            raise Unreadable(f"받지 않는 압축 방식이다({info.compress_type}): {name}")
        with zf.open(info) as handle:
            data = handle.read(self.left + 1)  # deflate 는 이만큼만 푼다
        if len(data) > self.left:
            raise Unreadable(f"풀면 너무 크다: {name}")
        self.left -= len(data)
        return data


def _zipped(data: bytes, kind: str) -> str:
    budget = _Budget(UNZIPPED)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if kind == "zip":
            kind = _zip_kind(names)

        def xml(name: str) -> ElementTree.Element:
            return ElementTree.fromstring(budget.read(zf, name))

        if kind == HWPX:
            parts = sorted(
                (n for n in names if re.match(r"Contents/section\d+\.xml$", n)), key=_number
            )
            return "\n".join(_lines(xml(n)) for n in parts)
        if kind == DOCX:
            return _lines(xml("word/document.xml"))
        if kind == PPTX:
            slides = sorted(
                (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=_number
            )
            return "\n".join(f"[슬라이드 {i}]\n{_lines(xml(n))}" for i, n in enumerate(slides, 1))
        if kind == XLSX:
            return _xlsx(names, xml)
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


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# 글 조각, 공백이 되는 것(탭·줄바꿈), 표(행·칸). DOCX(w:)·HWPX(hp:)·PPTX(a:) 모두 이름이 같다.
_TEXT, _BREAKS, _TABLE, _ROW, _CELL, _PARA = "t", ("tab", "br"), "tbl", "tr", "tc", "p"


def _lines(root: ElementTree.Element) -> str:
    """문단마다 한 줄, 표는 한 행을 한 줄(칸은 " | ")로.

    한글 OWPML 은 표가 문단 안(run)에 들어 있어서, 문단의 글을 모을 때 안의 표는 빼고 따로 줄을
    만든다 — 안 그러면 칸 글이 한 줄로 붙었다(리뷰 재현: "회차접수3회9.21~9.28").
    """
    lines: List[str] = []

    def own(node: ElementTree.Element) -> List[str]:
        parts: List[str] = []
        for child in node:
            tag = _local(child.tag)
            # 안의 표와 안의 문단(글상자 등)은 따로 줄이 된다 — 여기 넣으면 두 번 나온다.
            if tag in (_TABLE, _ROW, _PARA):
                continue
            if tag == _TEXT:
                parts.append(child.text or "")
            elif tag in _BREAKS:
                parts.append(" ")
            else:
                parts.extend(own(child))
        return parts

    def cell(node: ElementTree.Element) -> str:
        inner: List[str] = []
        walk(node, inner)
        return " ".join(inner)

    def walk(node: ElementTree.Element, out: List[str]) -> None:
        tag = _local(node.tag)
        if tag == _ROW:
            cells = [cell(c).strip() for c in node if _local(c.tag) == _CELL]
            line = " | ".join(cells)
            if line.strip(" |"):
                out.append(line)
            return
        if tag == _PARA:
            line = re.sub(r"\s+", " ", "".join(own(node))).strip()
            if line:
                out.append(line)
        for child in node:
            if _local(child.tag) != _TEXT:
                walk(child, out)

    walk(root, lines)
    return "\n".join(lines)


def _xlsx(names: List[str], xml: Callable[[str], ElementTree.Element]) -> str:
    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in xml("xl/sharedStrings.xml"):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == _TEXT))
    dates = _date_styles(xml("xl/styles.xml")) if "xl/styles.xml" in names else set()
    sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=_number)
    out: List[str] = []
    for i, name in enumerate(sheets, 1):
        rows: List[str] = []
        for row in xml(name).iter():
            if _local(row.tag) != "row":
                continue
            cells: Dict[int, str] = {}
            for c in row:
                if _local(c.tag) == "c":
                    column = _column(c.get("r"), max(cells) + 1 if cells else 0)
                    cells[column] = _cell(c, shared, dates).strip()
            if cells:
                line = " | ".join(cells.get(j, "") for j in range(max(cells) + 1))
                if line.strip(" |"):
                    rows.append(line)
            if len(rows) >= SHEET_ROWS:
                break
        if rows:
            out.append(f"[시트 {i}]\n" + "\n".join(rows))
    return "\n".join(out)


def _cell(c: ElementTree.Element, shared: List[str], dates: Set[int]) -> str:
    kind = c.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter() if _local(t.tag) == _TEXT)
    value = next((v.text or "" for v in c if _local(v.tag) == "v"), "")
    if kind == "s":
        return shared[int(value)] if value.isdigit() and int(value) < len(shared) else ""
    style = c.get("s", "0")
    if kind in (None, "n") and value and style.isdigit() and int(style) in dates:
        return _serial(value)
    return value


def _date_styles(styles: ElementTree.Element) -> Set[int]:
    """날짜 서식인 셀 스타일 번호들. 날짜는 일련번호(46286 = 2026-09-21)로 적혀 있다(리뷰 재현)."""
    custom: Dict[str, str] = {}
    for fmt in styles.iter():
        if _local(fmt.tag) == "numFmt":
            custom[fmt.get("numFmtId", "")] = fmt.get("formatCode", "")
    found: Set[int] = set()
    xfs = next((n for n in styles.iter() if _local(n.tag) == "cellXfs"), None)
    for index, xf in enumerate(list(xfs) if xfs is not None else []):
        fid = xf.get("numFmtId", "0")
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fid, "")).lower()
        builtin = fid.isdigit() and (14 <= int(fid) <= 22 or 45 <= int(fid) <= 47)
        if builtin or any(ch in code for ch in "yd"):
            found.add(index)
    return found


def _serial(value: str) -> str:
    try:
        moment = datetime(1899, 12, 30) + timedelta(days=float(value))
    except (ValueError, OverflowError):
        return value
    return moment.strftime("%Y-%m-%d %H:%M" if moment.hour or moment.minute else "%Y-%m-%d")


def _column(ref: Optional[str], fallback: int) -> int:
    """셀 주소("C3")의 열 번호(0부터). 빈 칸은 행에 안 적혀 있어서, 순서로 세면 열이 밀린다."""
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return fallback
    number = 0
    for ch in letters.group(0):
        number = number * 26 + (ord(ch) - 64)
    return number - 1


def _number(name: str) -> int:
    digits = re.findall(r"\d+", name)
    return int(digits[-1]) if digits else 0


if __name__ == "__main__":
    _child()
