"""파일 읽기 — 종류 알아보기, 글만 뽑기, 깨지거나 큰 파일은 시끄럽게."""

from __future__ import annotations

import asyncio
import io
import json
import struct
import zipfile
import zlib
from pathlib import Path
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

    폭탄 하나가 서버 메모리를 다 쓰지 않게. 자식 둘에 본체까지 `jarvis` 의 MemoryMax(3G) 안에
    든다. 라이브러리 경고는 찍지 않는다 — 부모가 끝만 받아도, 애초에 쏟지 않게.
    """
    import logging
    import resource
    import sys
    import warnings

    calls: list = []
    quiet: list = []
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append((which, limits)))
    # 이 시험 프로세스의 로깅·경고 설정은 건드리지 않는다(뒤 시험의 caplog 가 비었다).
    monkeypatch.setattr(logging, "disable", lambda level: quiet.append(level))
    monkeypatch.setattr(warnings, "simplefilter", lambda action, *a, **k: quiet.append(action))
    monkeypatch.setattr(files, "TEXT_CAP", 7)

    class In:
        buffer = io.BytesIO(_pdf("Exam 2026.09.21"))

    class Out:
        buffer = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", In())
    monkeypatch.setattr(sys, "stdout", Out())
    monkeypatch.setattr(sys, "argv", ["files.py", "pdf"])
    files._child()

    assert (resource.RLIMIT_AS, (files.CHILD_MEMORY, files.CHILD_MEMORY)) in calls
    assert (resource.RLIMIT_CPU, (files.CHILD_CPU, files.CHILD_CPU + 1)) in calls
    assert files.CHILD_MEMORY * files.SLOTS <= 2_000_000_000 and files.CHILD_CPU <= 30
    assert quiet == [logging.CRITICAL, "ignore"]
    # 글은 TEXT_CAP 자까지만 돌려준다.
    assert json.loads(Out.buffer.getvalue()) == {"text": "[1쪽]\nEx"}


def test_the_child_reports_limits_it_could_not_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """맥은 메모리 상한을 못 건다. 같이 걸었더니 그 실패에 CPU 상한까지 건너뛰었고, 못 건 사실은
    오류 출력에 묻혔다(재리뷰) — 따로 걸고, 못 건 것은 답에 담아 부모가 한 번 남긴다."""
    import logging
    import resource
    import sys
    import warnings

    calls: list = []

    def refuse_memory(which: int, limits: tuple) -> None:
        if which == resource.RLIMIT_AS:
            raise ValueError("current limit exceeds maximum limit")
        calls.append(which)

    monkeypatch.setattr(resource, "setrlimit", refuse_memory)
    monkeypatch.setattr(logging, "disable", lambda level: None)
    monkeypatch.setattr(warnings, "simplefilter", lambda action, *a, **k: None)

    class In:
        buffer = io.BytesIO(_pdf("Exam"))

    class Out:
        buffer = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", In())
    monkeypatch.setattr(sys, "stdout", Out())
    monkeypatch.setattr(sys, "argv", ["files.py", "pdf"])
    files._child()

    result = json.loads(Out.buffer.getvalue())
    assert calls == [resource.RLIMIT_CPU] and result["unlimited"][0].startswith("RLIMIT_AS")

    monkeypatch.setattr(files, "_warned_limits", False)
    with caplog.at_level(logging.WARNING, logger="src.brain.files"):
        files._note_limits(result["unlimited"])
        files._note_limits(result["unlimited"])
    assert caplog.text.count("상한을 못 걸었다") == 1


def test_odd_surrogates_from_a_parser_do_not_kill_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf 는 ToUnicode 의 <D800> 을 짝 없는 서로게이트로 준다 — utf-8 로 쓰다 자식이 죽어 그 파일
    전체를 못 읽었다(재리뷰). 짝은 한 글자로, 짝 없는 것은 �로."""
    import logging
    import resource
    import sys
    import warnings

    # 자식의 상한을 이 시험 프로세스에 걸지 않는다 — 걸었더니 뒤에 뜬 크롬·드라이버가 1GB 상한을
    # 물려받아 바로 죽었다(conftest `_limits_untouched` 의 사고 이력).
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: None)
    monkeypatch.setattr(logging, "disable", lambda level: None)
    monkeypatch.setattr(warnings, "simplefilter", lambda action, *a, **k: None)
    monkeypatch.setattr(files, "text_of", lambda data, kind: "a\ud800b \ud83d\ude00")

    class In:
        buffer = io.BytesIO(b"")

    class Out:
        buffer = io.BytesIO()

    monkeypatch.setattr(sys, "stdin", In())
    monkeypatch.setattr(sys, "stdout", Out())
    monkeypatch.setattr(sys, "argv", ["files.py", "pdf"])
    files._child()
    assert json.loads(Out.buffer.getvalue())["text"] == "a\ufffdb 😀"


_SECTOR, _END, _FREE, _FAT, _NONE = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF


def _entry(
    name: str, kind: int, right: int = _NONE, child: int = _NONE, start: int = _END, size: int = 0
) -> bytes:
    raw = name.encode("utf-16-le") + b"\x00\x00"
    out = raw.ljust(64, b"\x00") + struct.pack("<HBB", len(raw), kind, 1)
    out += struct.pack("<III", _NONE, right, child) + b"\x00" * 36
    return out + struct.pack("<IQ", start, size)


def _ole_head(directory: int, minifat: int, minifat_count: int, fat_at: int) -> bytes:
    head = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    head += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
    head += struct.pack("<9I", 0, 1, directory, 0, 0x1000, minifat, minifat_count, _END, 0)
    return head + struct.pack("<109I", fat_at, *([_FREE] * 108))


def _cfb(header: bytes, section: bytes) -> bytes:
    """한글 5.0 꼴 OLE(CFB v3) 파일 — Root → FileHeader, BodyText → Section0.

    시험용 최소 작성기다(리뷰 재현 도구를 옮김). 스트림은 4096 바이트 이상으로 채워 작은
    스트림 칸을 안 쓴다.
    """
    streams = [header.ljust(4096, b"\x00"), section.ljust(4096, b"\x00")]
    sectors: list = []
    starts: list = []
    fat: list = []
    for data in streams:
        count = -(-len(data) // _SECTOR)
        starts.append(len(sectors))
        padded = data.ljust(count * _SECTOR, b"\x00")
        for i in range(count):
            sectors.append(padded[i * _SECTOR : (i + 1) * _SECTOR])
            fat.append(len(sectors) if i < count - 1 else _END)
    directory = len(sectors)
    sectors.append(
        _entry("Root Entry", 5, child=1)
        + _entry("FileHeader", 2, right=2, start=starts[0], size=len(streams[0]))
        + _entry("BodyText", 1, child=3)
        + _entry("Section0", 2, start=starts[1], size=len(streams[1]))
    )
    fat.append(_END)
    fat_at = len(sectors)
    fat.append(_FAT)
    fat.extend([_FREE] * (128 - len(fat)))
    sectors.append(struct.pack("<128I", *fat))
    return _ole_head(directory, _END, 0, fat_at) + b"".join(sectors)


def _mini_hwp(text: str, root: int, loop: bool = False) -> bytes:
    """본문(Section0)이 작은 스트림 칸에 든 한글 파일(재리뷰 재현의 꼴).

    `root` 는 Root Entry 가 적은 칸 크기, `loop` 면 그 칸의 FAT 가 제자리를 가리킨다.
    """
    header = b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000300, 0)
    header = header.ljust(4096, b"\x00")
    section = _record(67, text.encode("utf-16-le"))
    sectors = [header[i : i + _SECTOR] for i in range(0, 4096, _SECTOR)]  # 0~7 FileHeader
    sectors.append(section.ljust(_SECTOR, b"\x00"))  # 8 작은 스트림 칸
    sectors.append(struct.pack("<128I", _END, *([_FREE] * 127)))  # 9 MiniFAT
    sectors.append(  # 10 디렉터리
        _entry("Root Entry", 5, child=1, start=8, size=root)
        + _entry("FileHeader", 2, right=2, start=0, size=4096)
        + _entry("BodyText", 1, child=3)
        + _entry("Section0", 2, start=0, size=len(section))
    )
    fat = [1, 2, 3, 4, 5, 6, 7, _END, 8 if loop else _END, _END, _END, _FAT]
    sectors.append(struct.pack("<128I", *fat, *([_FREE] * (128 - len(fat)))))  # 11 FAT
    return _ole_head(10, 9, 1, 11) + b"".join(sectors)


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


def test_small_streams_cannot_hide_a_huge_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """작은 스트림은 Root Entry 의 한 덩어리에서 읽는다 — 구역 크기만 봐서는 못 막는다.

    6.6KB HWP 가 그 덩어리를 300MB 로 적고 FAT 를 제자리로 돌려 728MB 를 썼다(재리뷰 재현). 구조
    검사(`DEFECT_INCORRECT`)가 덩어리가 FAT 보다 크다는 걸 잡고, 덩어리 크기도 먼저 본다.
    """
    assert text_of(_mini_hwp("3회 접수", 64), files.HWP) == "3회 접수"
    # FAT(128칸)보다 큰 덩어리를 제자리 고리로 — 구조 검사가 없으면 고리를 돌며 읽어 "ok"였다.
    with pytest.raises(Unreadable):
        text_of(_mini_hwp("3회 접수", 100_000, loop=True), files.HWP)
    # 구조는 멀쩡해도 덩어리가 상한보다 크면 열기 전에.
    monkeypatch.setattr(files, "UNZIPPED", 32)
    with pytest.raises(Unreadable, match="작은 스트림"):
        text_of(_mini_hwp("3회 접수", 64), files.HWP)


def test_hwp_caps_count_across_sections_and_uncompressed_too() -> None:
    """구역마다 따로 세면 구역을 늘려 상한을 넘긴다. 압축 안 된 본문도 같은 상한."""
    record = _record(67, "가나".encode("utf-16-le"))
    with pytest.raises(Unreadable):
        hwp_text([record * 10], compressed=False, limit=len(record))
    with pytest.raises(Unreadable):
        hwp_text([record, record], compressed=False, limit=len(record) + 4)

    def deflated(raw: bytes) -> bytes:
        deflate = zlib.compressobj(wbits=-15)
        return deflate.compress(raw) + deflate.flush()

    with pytest.raises(Unreadable):
        hwp_text([deflated(record), deflated(record)], compressed=True, limit=len(record) + 4)
    assert hwp_text([record, record], compressed=False, limit=2 * len(record)) == "가나\n가나"


def test_zip_members_are_read_only_up_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """다 읽고 나서 크기를 보면 실패는 해도 메모리는 이미 쓴 뒤다. 문서 전체에서 누적으로 센다."""
    import tracemalloc

    big = _zip({"word/document.xml": f"<w:document {W}>" + " " * 20_000_000 + "</w:document>"})
    monkeypatch.setattr(files, "UNZIPPED", 1_000)
    tracemalloc.start()
    try:
        with pytest.raises(Unreadable):
            text_of(big, files.DOCX)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000

    section = f"<hs {HP}><hp:p><hp:run><hp:t>" + "가" * 200 + "</hp:t></hp:run></hp:p></hs>"
    two = _zip({"Contents/section0.xml": section, "Contents/section1.xml": section})
    with pytest.raises(Unreadable):
        text_of(two, files.HWPX)


def test_hwpx_text_after_line_breaks_and_marks_is_kept() -> None:
    """한글 OWPML 은 줄바꿈·탭·형광펜이 글 조각 안의 요소이고, 뒤 글은 그 꼬리에 있다.

    .text 만 읽었더니 korea.kr 보도자료에서 17곳 중 12곳이 빠졌다 — 날짜 뒤 시각이 사라졌다(재리뷰).
    """
    hwpx = _zip(
        {
            "Contents/section0.xml": f"<hs {HP}><hp:p><hp:run><hp:t>11. 14.(토)<hp:lineBreak/>"
            "13:00~14:30(북토크)<hp:lineBreak/>15:00~16:30(워크숍)</hp:t></hp:run></hp:p>"
            '<hp:p><hp:run><hp:t><hp:markpenBegin color="#FFFF00"/>상수동 골목 일대'
            "<hp:markpenEnd/>에서<hp:tab/>열린다</hp:t></hp:run></hp:p></hs>"
        }
    )
    assert text_of(hwpx, files.HWPX) == (
        "11. 14.(토) 13:00~14:30(북토크) 15:00~16:30(워크숍)\n상수동 골목 일대에서 열린다"
    )


def test_excel_times_are_times_and_odd_cell_addresses_stay_small() -> None:
    """시각 서식까지 날짜로 보면 09:30 이 "1899-12-30 09:29"가 됐다(재리뷰 — 자르지 말고 반올림).

    셀 주소 하나("ZZZZZZZZZZ1")로 빈 칸을 수십억 개 만들다 1GB 를 썼다 — 엑셀의 마지막 열까지만.
    """
    xlsx = _zip(
        {
            "xl/workbook.xml": f"<workbook {S}/>",
            "xl/styles.xml": f"<styleSheet {S}><numFmts>"
            '<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
            '<numFmt numFmtId="165" formatCode="[h]:mm"/></numFmts>'
            '<cellXfs><xf numFmtId="0"/><xf numFmtId="20"/><xf numFmtId="164"/>'
            '<xf numFmtId="165"/></cellXfs></styleSheet>',
            "xl/worksheets/sheet1.xml": f"<worksheet {S}><sheetData><row>"
            '<c r="A1" s="1"><v>0.3958333333</v></c><c r="B1" s="2"><v>46286</v></c>'
            '<c r="C1" s="3"><v>1.5</v></c>'
            '<c r="ZZZZZZZZZZ1" t="inlineStr"><is><t>멀리</t></is></c>'
            "</row></sheetData></worksheet>",
        }
    )
    # 누적 시각([h]:mm)의 1.5일은 "12:00"이 아니라 "36:00"(3차 리뷰).
    assert text_of(xlsx, files.XLSX) == "[시트 1]\n09:30 | 2026-09-21 | 36:00 | 멀리"


def _child_script(body: str) -> list:
    import sys

    return [sys.executable, "-c", "import sys\nsys.stdin.buffer.read()\n" + body]


def test_a_flood_on_the_error_output_stays_in_the_child() -> None:
    """자식의 오류 출력은 끝만 남긴다. 다 모았더니 87KB PDF 하나에 본체 메모리가 1.4GB 가 됐다

    (pypdf 가 깨진 줄마다 그 줄을 통째로 경고로 찍었다 — 재리뷰 재현). 왜 멈췄는지는 끝에 있다.
    """
    import tracemalloc

    from src.brain.files import extract

    flood = _child_script(
        "for _ in range(320):\n    sys.stderr.buffer.write(b'x' * 65536)\n"
        "sys.stderr.buffer.write('\\n끝에 남은 이유'.encode())\nsys.exit(3)\n"
    )
    tracemalloc.start()
    try:
        with pytest.raises(Unreadable, match="종료 3.*끝에 남은 이유"):
            asyncio.run(extract(b"x", "pdf", command=flood))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 5_000_000  # 20MB 를 쏟아도


def test_too_much_text_from_the_child_is_cut_off() -> None:
    from src.brain.files import extract

    chatty = _child_script("sys.stdout.buffer.write(b'x' * 5_000_000)\n")
    with pytest.raises(Unreadable, match="넘게 돌려줘"):
        asyncio.run(extract(b"x", "pdf", command=chatty))


def test_a_child_that_ends_before_reading_everything_still_answers() -> None:
    """자식이 입력을 다 안 읽고 끝나면 보내던 쪽이 끊긴 파이프를 만난다 — 그래도 답을 받는다."""
    import sys

    from src.brain.files import extract

    early = [sys.executable, "-c", 'print(\'{"text": "먼저 끝"}\')']
    assert asyncio.run(extract(b"x" * 5_000_000, "pdf", command=early)) == "먼저 끝"


def test_at_most_two_children_run_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """대화·관심사 준비·보고서가 겹쳐도 자식은 둘까지 — 자식마다 1GB 다."""
    from src.brain.files import extract

    monkeypatch.setattr(files, "_slots", None)  # 이 시험의 루프에서 새로 만든다
    body = (
        "import os, time\nstart = time.time()\ntime.sleep(0.3)\n"
        "open(os.path.join(sys.argv[1], str(os.getpid())), 'w').write(f'{start} {time.time()}')\n"
        'sys.stdout.write(\'{"text": "ok"}\')\n'
    )
    command = _child_script(body) + [str(tmp_path)]

    async def four() -> list:
        return list(await asyncio.gather(*(extract(b"", "pdf", command=command) for _ in range(4))))

    assert asyncio.run(four()) == ["ok"] * 4
    events = []
    for path in tmp_path.iterdir():
        start, end = map(float, path.read_text().split())
        events += [(start, 1), (end, -1)]
    running = most = 0
    for _, change in sorted(events):
        running += change
        most = max(most, running)
    assert most == 2


def test_the_child_starts_isolated_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """자식은 `-I` 로 — 이 파일의 폴더(src/brain)가 모듈 경로 맨 앞에 오면 같은 이름의 파일이 표준
    모듈을 가린다(재리뷰: 리뷰어의 concurrent.py 가 asyncio 를 깨뜨렸다). 환경은 PATH·LANG 만."""
    import sys

    from src.brain.files import extract

    seen: dict = {}

    async def spawn(*argv: str, **options: object) -> object:
        seen.update(argv=argv, env=options.get("env"))
        raise OSError("여기까지만")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(files, "_slots", None)
    with pytest.raises(OSError):
        asyncio.run(extract(b"", "pdf"))
    assert seen["argv"][:2] == (sys.executable, "-I") and seen["argv"][-1] == "pdf"
    assert set(seen["env"]) == {"PATH", "LANG"}


def test_a_child_that_closes_its_pipes_and_then_ends_still_answers() -> None:
    """통로를 닫고 조금 더 돌다 끝나는 자식 — 끝날 때까지 기다린다. 안 기다리면 막 끝나던 자식을
    죽여 "종료 -9"가 났다(3차 리뷰 돌연변이)."""
    from src.brain.files import extract

    late = _child_script(
        'import os, time\nsys.stdout.write(\'{"text": "다 읽음"}\')\nsys.stdout.flush()\n'
        "os.close(1)\nos.close(2)\ntime.sleep(0.3)\n"
    )
    assert asyncio.run(extract(b"x", "pdf", command=late)) == "다 읽음"


def test_limits_a_child_could_not_set_are_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """맥처럼 상한을 못 건 자식은 답에 그 사실을 담고, 부모가 한 번 남긴다.

    서버에서 이 경고가 나오면 사고다.
    """
    import logging

    from src.brain.files import extract

    told = _child_script(
        'sys.stdout.write(\'{"text": "ok", "unlimited": ["RLIMIT_AS: 못 걺"]}\')\n'
    )
    monkeypatch.setattr(files, "_warned_limits", False)
    with caplog.at_level(logging.WARNING, logger="src.brain.files"):
        assert asyncio.run(extract(b"", "pdf", command=told)) == "ok"
        assert asyncio.run(extract(b"", "pdf", command=told)) == "ok"
    assert caplog.text.count("상한을 못 걸었다") == 1 and "RLIMIT_AS" in caplog.text
