"""파일 읽기 — 종류 알아보기, 글만 뽑기, 깨지거나 큰 파일은 시끄럽게."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from typing import Dict

import pytest

from src.brain import files
from src.brain.files import Unreadable, hwp_text, kind_of, name_of, text_of


def _pdf(text: str) -> bytes:
    """글 한 줄짜리 PDF. 위치표(xref)까지 맞춰 만든다."""
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def _zip(entries: Dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def test_the_kind_comes_from_the_first_bytes_first() -> None:
    """큐넷은 PDF 를 application/octet-stream 으로 준다

    — 이름이나 종류보다 내용의 첫 바이트를 믿는다.
    """
    assert kind_of(b"%PDF-1.4", "application/octet-stream", "x.bin") == files.PDF
    assert (
        kind_of(b"\xd0\xcf\x11\xe0\xa1\xb1", "application/octet-stream", "안내문.hwp") == files.HWP
    )
    # 같은 OLE 꼴이라도 옛 오피스(.doc)는 읽지 않는다.
    assert kind_of(b"\xd0\xcf\x11\xe0\xa1\xb1", "", "old.doc") == ""
    assert kind_of(b"PK\x03\x04", "", "표.xlsx") == files.XLSX
    assert kind_of(b"PK\x03\x04", "", "noname") == "zip"
    assert kind_of(b"<html>", "text/html; charset=utf-8", "") == files.HTML
    assert kind_of(b"a,b", "text/csv", "") == files.TEXT
    assert kind_of(b"\x89PNG", "image/png", "a.png") == ""


def test_the_name_comes_from_the_header_then_the_address() -> None:
    disposition = "attachment; filename*=UTF-8''2026%EB%85%84%20%EC%9D%BC%EC%A0%95.pdf"
    assert name_of("https://x/download?id=1", disposition) == "2026년 일정.pdf"
    assert name_of("https://x/d", 'attachment; filename="plan.hwp"') == "plan.hwp"
    assert name_of("https://x/files/%EC%95%88%EB%82%B4.pdf") == "안내.pdf"


def test_a_pdf_gives_its_text_with_page_marks() -> None:
    text = text_of(_pdf("Exam 2026.09.21"), files.PDF)
    assert text.startswith("[1쪽]") and "Exam 2026.09.21" in text


def test_a_broken_pdf_is_loud() -> None:
    with pytest.raises(Unreadable):
        text_of(b"%PDF-1.4 garbage", files.PDF)


def _record(tag: int, payload: bytes) -> bytes:
    """레코드 머리 4바이트. 크기가 0xFFF 이상이면 크기 칸에 0xFFF, 진짜 크기는 뒤 4바이트에."""
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | (0xFFF << 20), len(payload)) + payload
    return struct.pack("<I", tag | (len(payload) << 20)) + payload


def test_hwp_paragraph_records_become_lines() -> None:
    """한글 5.0 본문: 레코드 중 문단 글(67)만, 표·그림 같은 조종 문자(8칸)는 건너뛴다."""
    # 표 개체 자리 — 뒤 7칸은 개체 이름 같은 값이다(0 이면 건너뛰기를 빼도 시험이 못 잡았다, 리뷰).
    control = struct.pack("<8H", 11, 0x6274, 0x6C74, 0x2020, 0x4142, 0x4344, 0x4546, 11)
    first = _record(
        67, "3회 실기 접수 ".encode("utf-16-le") + control + "9.21~9.28".encode("utf-16-le")
    )
    second = _record(66, b"\x00" * 6) + _record(67, "발표 12.18\r".encode("utf-16-le"))
    raw = first + second
    deflate = zlib.compressobj(wbits=-15)
    compressed = deflate.compress(raw) + deflate.flush()

    assert hwp_text([raw], compressed=False) == "3회 실기 접수 9.21~9.28\n발표 12.18"
    assert hwp_text([compressed], compressed=True) == "3회 실기 접수 9.21~9.28\n발표 12.18"


def test_a_file_that_only_looks_like_hwp_is_loud() -> None:
    with pytest.raises(Unreadable):
        text_of(b"\xd0\xcf\x11\xe0" + b"\x00" * 600, files.HWP)


def test_office_and_hwpx_documents_give_lines_and_table_rows() -> None:
    docx = _zip(
        {
            "word/document.xml": f"<w:document {W}><w:body>"
            "<w:p><w:r><w:t>2026 실기 일정</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>3회</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>9.21~9.28</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>"
        }
    )
    assert text_of(docx, files.DOCX) == "2026 실기 일정\n3회 | 9.21~9.28"

    xlsx = _zip(
        {
            "xl/workbook.xml": f"<workbook {S}/>",
            "xl/sharedStrings.xml": f"<sst {S}><si><t>회차</t></si><si><t>접수</t></si></sst>",
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>"
            '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
            '<row><c><v>3</v></c><c t="inlineStr"><is><t>9.21~9.28</t></is></c></row>'
            "</sheetData></worksheet>",
        }
    )
    assert text_of(xlsx, "zip") == "[시트 1]\n회차 | 접수\n3 | 9.21~9.28"

    pptx = _zip(
        {
            "ppt/presentation.xml": "<p/>",
            "ppt/slides/slide2.xml": f"<s {A}><a:p><a:r><a:t>둘째</a:t></a:r></a:p></s>",
            "ppt/slides/slide1.xml": f"<s {A}><a:p><a:r><a:t>첫째</a:t></a:r></a:p></s>",
        }
    )
    assert text_of(pptx, files.PPTX) == "[슬라이드 1]\n첫째\n[슬라이드 2]\n둘째"

    hwpx = _zip(
        {
            "Contents/section0.xml": f"<hs {HP}><hp:p><hp:run><hp:t>3회 접수</hp:t></hp:run>"
            "<hp:run><hp:t> 9.21~9.28</hp:t></hp:run></hp:p></hs>"
        }
    )
    assert text_of(hwpx, files.HWPX) == "3회 접수 9.21~9.28"


def test_a_zip_that_unpacks_huge_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """몇 KB 가 수 GB 로 풀리는 파일이 있다 — 풀기 전에 크기를 본다."""
    monkeypatch.setattr(files, "UNZIPPED", 100)
    big = _zip({"word/document.xml": f"<w:document {W}>" + "x" * 1000 + "</w:document>"})
    with pytest.raises(Unreadable):
        text_of(big, files.DOCX)


def test_unknown_kinds_are_loud() -> None:
    with pytest.raises(Unreadable):
        text_of(b"whatever", "")


def test_a_compressed_hwp_section_is_inflated_with_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 번에 풀면 0.3MB 가 666MB 가 됐다(리뷰 재현) — 조금씩 풀며 세고, 상한+1 바이트까지만 푼다.

    다 풀고 나서 크기를 보면 실패는 하지만 메모리는 이미 다 쓴 뒤다. 그래서 푼 양을 센다.
    """
    made = []

    class Counting:
        def __init__(self, wbits: int) -> None:
            self.inner = zlib.decompressobj(wbits)

        def decompress(self, data: bytes, max_length: int = 0) -> bytes:
            out = self.inner.decompress(data, max_length)
            made.append(len(out))
            return out

        @property
        def unconsumed_tail(self) -> bytes:
            return self.inner.unconsumed_tail

    class Zlib:
        decompressobj = Counting

    monkeypatch.setattr(files, "zlib", Zlib)
    deflate = zlib.compressobj(wbits=-15)
    bomb = deflate.compress(_record(67, "가".encode("utf-16-le") * 50_000)) + deflate.flush()
    with pytest.raises(Unreadable):
        hwp_text([bomb], compressed=True, limit=10_000)
    assert sum(made) <= 10_001


def test_hwp_text_keeps_emoji_and_survives_odd_bytes() -> None:
    """이모지는 서로게이트 짝이다 — 낱낱이 chr 하면 짝 없는 서로게이트가 남았다(리뷰).

    그 글자 때문에 다음 요청이 실패했다. 탭(8칸 조종)은 공백이 되고, 끝이 잘린 레코드 하나
    때문에 앞 문단까지 버리지 않는다.
    """
    emoji = "합격 😀".encode("utf-16-le")
    lone = struct.pack("<H", 0xD83D) + "끝".encode("utf-16-le")
    tab = (
        "접수기간".encode("utf-16-le")
        + struct.pack("<8H", 9, 0, 0, 0, 0, 0, 0, 9)
        + "9.21".encode("utf-16-le")
    )
    body = _record(67, emoji) + _record(67, lone) + _record(67, tab)
    truncated = body + struct.pack("<I", 67 | (0xFFF << 20)) + b"\x01"
    text = hwp_text([truncated], compressed=False)
    assert text.split("\n") == ["합격 😀", "\ufffd끝", "접수기간 9.21"]
    text.encode("utf-8")  # 보낼 수 있어야 한다


def test_hwp_streams_bigger_than_the_cap_are_refused() -> None:
    """크기를 먼저 본다.

    자기 자신을 가리키는 FAT 로 4GB 를 적은 9KB 파일이 메모리를 다 썼다(리뷰 재현).
    """
    from src.brain.files import _stream

    class Lying:
        def get_size(self, entry: list) -> int:
            return 4_000_000_000

        def openstream(self, entry: list) -> object:
            raise AssertionError("열기 전에 막혀야 한다")

    with pytest.raises(Unreadable):
        _stream(Lying(), ["BodyText", "Section0"], 30_000_000)


def test_zip_documents_are_read_with_a_real_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """파일이 스스로 적은 크기는 믿지 않는다.

    bzip2·lzma 는 풀 길이를 정할 수 없어 받지 않는다(리뷰).
    """
    monkeypatch.setattr(files, "UNZIPPED", 1_000)
    body = (
        f"<w:document {W}><w:body><w:p><w:r><w:t>"
        + "가" * 5_000
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", body)
    with pytest.raises(Unreadable):
        text_of(out.getvalue(), files.DOCX)

    monkeypatch.setattr(files, "UNZIPPED", 30_000_000)
    bz = io.BytesIO()
    with zipfile.ZipFile(bz, "w", compression=zipfile.ZIP_BZIP2) as zf:
        zf.writestr("word/document.xml", f"<w:document {W}/>")
    with pytest.raises(Unreadable, match="압축 방식"):
        text_of(bz.getvalue(), files.DOCX)


def test_hwpx_tables_inside_paragraphs_become_rows() -> None:
    """한글 OWPML 은 표가 문단의 run 안에 있다 — 칸 글이 한 줄로 붙었다(리뷰 재현)."""
    hwpx = _zip(
        {
            "Contents/section0.xml": f"<hs {HP}><hp:p><hp:run><hp:t>일정</hp:t>"
            "<hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>회차</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
            "<hp:tc><hp:subList><hp:p><hp:run><hp:t>접수</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>"
            "<hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>3회</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
            "<hp:tc><hp:subList><hp:p><hp:run><hp:t>9.21~9.28</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr>"
            "</hp:tbl></hp:run></hp:p></hs>"
        }
    )
    assert text_of(hwpx, files.HWPX) == "일정\n회차 | 접수\n3회 | 9.21~9.28"


def test_excel_dates_and_empty_cells_land_in_place() -> None:
    """날짜는 일련번호(46286)로 적혀 있고, 빈 칸은 행에 안 적혀 있다.

    순서로 세면 열이 밀렸다(리뷰).
    """
    xlsx = _zip(
        {
            "xl/workbook.xml": f"<workbook {S}/>",
            "xl/styles.xml": f'<styleSheet {S}><cellXfs count="2"><xf numFmtId="0"/>'
            '<xf numFmtId="14"/></cellXfs></styleSheet>',
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>"
            '<row r="1"><c r="A1" t="inlineStr"><is><t>회차</t></is></c>'
            '<c r="C1" s="1"><v>46286</v></c></row>'
            "</sheetData></worksheet>",
        }
    )
    assert text_of(xlsx, files.XLSX) == "[시트 1]\n회차 |  | 2026-09-21"


def test_word_tabs_are_spaces() -> None:
    docx = _zip(
        {
            "word/document.xml": f"<w:document {W}><w:body><w:p><w:r><w:t>접수기간</w:t><w:tab/>"
            "<w:t>9.21</w:t></w:r></w:p></w:body></w:document>"
        }
    )
    assert text_of(docx, files.DOCX) == "접수기간 9.21"


def test_names_survive_odd_encodings() -> None:
    """날바이트 EUC-KR('ÀÏÁ¤.pdf'), RFC 5987 의 EUC-KR, 공백 대신 + (리뷰·목포해양대 실측)."""
    raw = "일정.pdf".encode("cp949").decode("latin-1")
    assert name_of("https://x/d", f'attachment; filename="{raw}"') == "일정.pdf"
    assert name_of("https://x/d", "attachment; filename*=EUC-KR''%C0%CF%C1%A4.pdf") == "일정.pdf"
    assert name_of("https://x/d", 'attachment; filename="2026년+일정.pdf"') == "2026년 일정.pdf"


def test_encrypted_pdfs_open_only_with_an_empty_password() -> None:
    """빈 암호로 잠근 공문이 흔하다.

    틀린 암호면 예외 없이 "못 풀었다"가 와서 그 가지가 안 돌았다(리뷰).
    """
    from pypdf import PdfReader, PdfWriter

    def locked(password: str) -> bytes:
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf("Exam 2026.09.21"))))
        writer.encrypt(user_password=password, owner_password="owner", algorithm="AES-128")
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    assert "Exam 2026.09.21" in text_of(locked(""), files.PDF)
    with pytest.raises(Unreadable, match="암호"):
        text_of(locked("secret"), files.PDF)


def test_the_child_sets_memory_and_cpu_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """서버(리눅스)에서 자식은 메모리·CPU 상한을 걸고 시작한다.

    폭탄 하나가 서버 메모리를 다 쓰지 않게.
    """
    import resource
    import sys

    calls: list = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append(which))

    class In:
        buffer = io.BytesIO(_pdf("Exam"))

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", In())
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["files.py", "pdf"])
    files._child()

    assert resource.RLIMIT_AS in calls and resource.RLIMIT_CPU in calls
    assert "Exam" in out.getvalue()


def _cfb(header: bytes, section: bytes) -> bytes:
    """한글 5.0 꼴 OLE(CFB v3) 파일 — Root → FileHeader, BodyText → Section0.

    시험용 최소 작성기다(리뷰 재현 도구를 옮김). 스트림은 4096 바이트 이상으로 채워 작은
    스트림 칸을 안 쓴다.
    """
    sector, end, free, fat_mark, none = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF

    def entry(
        name: str, kind: int, right: int = none, child: int = none, start: int = end, size: int = 0
    ) -> bytes:
        raw = name.encode("utf-16-le") + b"\x00\x00"
        out = raw.ljust(64, b"\x00") + struct.pack("<HBB", len(raw), kind, 1)
        out += struct.pack("<III", none, right, child) + b"\x00" * 36
        return out + struct.pack("<IQ", start, size)

    streams = [header.ljust(4096, b"\x00"), section.ljust(4096, b"\x00")]
    sectors: list = []
    starts: list = []
    fat: list = []
    for data in streams:
        count = -(-len(data) // sector)
        starts.append(len(sectors))
        padded = data.ljust(count * sector, b"\x00")
        for i in range(count):
            sectors.append(padded[i * sector : (i + 1) * sector])
            fat.append(len(sectors) if i < count - 1 else end)
    directory = len(sectors)
    sectors.append(
        entry("Root Entry", 5, child=1)
        + entry("FileHeader", 2, right=2, start=starts[0], size=len(streams[0]))
        + entry("BodyText", 1, child=3)
        + entry("Section0", 2, start=starts[1], size=len(streams[1]))
    )
    fat.append(end)
    fat_at = len(sectors)
    fat.append(fat_mark)
    fat.extend([free] * (128 - len(fat)))
    sectors.append(struct.pack("<128I", *fat))
    head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    head += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
    head += struct.pack("<9I", 0, 1, directory, 0, 0x1000, end, 0, end, 0)
    head += struct.pack("<109I", fat_at, *([free] * 108))
    return head + b"".join(sectors)


def _hwp_file(text: str, flags: int) -> bytes:
    header = b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000300, flags)
    body = _record(67, text.encode("utf-16-le"))
    if flags & 0x1:
        deflate = zlib.compressobj(wbits=-15)
        body = deflate.compress(body) + deflate.flush()
    return _cfb(header, body)


def test_a_real_hwp_file_is_read_and_locked_ones_are_loud() -> None:
    """OLE 를 열어 FileHeader 를 보고 본문을 읽는다.

    암호·배포용은 본문이 암호화돼 있어 시끄럽게 실패한다.
    """
    assert (
        text_of(_hwp_file("3회 실기 접수 9.21~9.28", 0x1), files.HWP) == "3회 실기 접수 9.21~9.28"
    )
    assert text_of(_hwp_file("압축 안 함", 0x0), files.HWP) == "압축 안 함"
    with pytest.raises(Unreadable, match="암호"):
        text_of(_hwp_file("x", 0x1 | 0x2), files.HWP)
    with pytest.raises(Unreadable, match="배포용"):
        text_of(_hwp_file("x", 0x1 | 0x4), files.HWP)


def test_only_the_first_pages_and_rows_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """일정·안내는 보통 앞에 있다. PDF 는 앞 30쪽, 엑셀은 시트마다 앞 300줄 — 상한을 줄여 본다."""
    from pypdf import PdfReader, PdfWriter

    one = PdfReader(io.BytesIO(_pdf("Exam")))
    writer = PdfWriter()
    writer.add_page(one.pages[0])
    writer.add_page(one.pages[0])
    out = io.BytesIO()
    writer.write(out)
    monkeypatch.setattr(files, "PDF_PAGES", 1)
    text = text_of(out.getvalue(), files.PDF)
    assert "[1쪽]" in text and "[2쪽]" not in text

    monkeypatch.setattr(files, "SHEET_ROWS", 2)
    rows = "".join(f'<row><c t="inlineStr"><is><t>{i}행</t></is></c></row>' for i in range(1, 4))
    xlsx = _zip(
        {
            "xl/workbook.xml": f"<workbook {S}/>",
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData>{rows}</sheetData></worksheet>",
        }
    )
    assert text_of(xlsx, files.XLSX) == "[시트 1]\n1행\n2행"
