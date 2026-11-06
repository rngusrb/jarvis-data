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
    return struct.pack("<I", tag | (len(payload) << 20)) + payload


def test_hwp_paragraph_records_become_lines() -> None:
    """한글 5.0 본문: 레코드 중 문단 글(67)만, 표·그림 같은 조종 문자(8칸)는 건너뛴다."""
    control = struct.pack("<8H", 11, 0, 0, 0, 0, 0, 0, 11)  # 표 개체 자리
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
