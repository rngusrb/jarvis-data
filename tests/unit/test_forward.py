"""배달부 — 파일에 적힌 줄을 잃지도, 두 번 세지도 않는다.

iCloud 파일은 폰이 덧붙이고 맥은 읽기만 한다. 어디까지 보냈는지는 파일마다
바이트 위치로 기억한다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from collectors.common import Rejected, Retry
from collectors.forward import files, forward, keep_rejected, main, replay
from collectors.mac_apps import Span, to_line


class FakeServer:
    def __init__(self, refuse: Optional[str] = None, down_after: Optional[int] = None) -> None:
        self.got: List[Tuple[str, Dict[str, Any]]] = []
        self.refuse = refuse
        self.down_after = down_after

    def __call__(self, route: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.down_after is not None and len(self.got) >= self.down_after:
            raise Retry("서버에 닿지 않는다")
        if self.refuse and body.get("kind") == self.refuse:
            raise Rejected("'%s'는 등록되지 않은 흔적 종류다" % self.refuse)
        self.got.append((route, body))
        return {"written": 1}


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "icloud" / "jarvis"
    folder.mkdir(parents=True)
    monkeypatch.setenv("JARVIS_INBOX", str(folder))
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    return folder / "inbox.jsonl"


def line(kind: str, value: float = 1.0) -> str:
    return json.dumps(
        {
            "to": "samples",
            "kind": kind,
            "samples": [{"at": "2026-10-30T09:00:00+09:00", "value": value}],
        },
        ensure_ascii=False,
    )


def run(server: FakeServer, state: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, Any]]:
    report, new_state, rejects = forward(files().paths, state or {}, server)
    keep_rejected(rejects)
    return report, new_state


def test_보낸_데까지_기억하고_새로_붙은_줄만_보낸다(inbox: Path) -> None:
    inbox.write_text(line("location") + "\n" + line("resting_heart_rate") + "\n")
    server = FakeServer()
    report, state = run(server)
    assert report.sent == 2

    with inbox.open("a") as f:
        f.write(line("location", 2.0) + "\n")
    report, state = run(server, state)

    assert report.sent == 1
    assert server.got[-1][1]["samples"][0]["value"] == 2.0
    assert state[str(inbox)]["offset"] == len(inbox.read_bytes())


def test_문_이름은_서버로_보내지_않는다(inbox: Path) -> None:
    inbox.write_text(line("location") + "\n")
    server = FakeServer()
    run(server)

    route, body = server.got[0]
    assert route == "/ingest/samples"
    assert "to" not in body


def test_한글이_섞여도_바이트_위치가_맞는다(inbox: Path) -> None:
    korean = {"to": "traces", "source": "iphone", "kind": "x", "traces": [{"text": "카톡 열기"}]}
    inbox.write_text(json.dumps(korean, ensure_ascii=False) + "\n" + line("location") + "\n")
    _, state = run(FakeServer())

    assert state[str(inbox)]["offset"] == len(inbox.read_bytes())


def test_덜_쓴_마지막_줄은_기다렸다가_다_쓰이면_보낸다(inbox: Path) -> None:
    whole = line("resting_heart_rate")
    inbox.write_text(line("location") + "\n" + whole[:20])
    server = FakeServer()
    report, state = run(server)
    assert report.sent == 1

    with inbox.open("a") as f:
        f.write(whole[20:] + "\n")
    report, state = run(server, state)

    assert report.sent == 1
    assert server.got[-1][1]["kind"] == "resting_heart_rate"


def test_줄바꿈이_없거나_여러_줄에_걸친_객체도_읽는다(inbox: Path) -> None:
    pretty = json.dumps(json.loads(line("sleep")), indent=2)
    inbox.write_text(line("location") + line("resting_heart_rate") + "\n" + pretty + "\n")
    report, _ = run(FakeServer())

    assert report.sent == 3


def test_깨진_줄은_따로_두고_다음_줄로_넘어간다(inbox: Path, tmp_path: Path) -> None:
    inbox.write_text(line("location") + '\n{"to":"samples", 깨짐\n' + line("sleep") + "\n")
    report, _ = run(FakeServer())

    assert (report.sent, report.rejected) == (2, 1)
    kept = (tmp_path / "state" / "rejected.jsonl").read_text()
    assert "깨짐" in kept


def test_문을_모르면_거절한다(inbox: Path) -> None:
    inbox.write_text('{"to":"elsewhere","x":1}\n{"kind":"no-door"}\n' + line("sleep") + "\n")
    report, _ = run(FakeServer())

    assert (report.sent, report.rejected) == (1, 2)


def test_서버가_거절한_줄이_뒤의_줄을_막지_않는다(inbox: Path) -> None:
    inbox.write_text(line("unknown_kind") + "\n" + line("sleep") + "\n")
    server = FakeServer(refuse="unknown_kind")
    report, _ = run(server)

    assert (report.sent, report.rejected) == (1, 1)


def test_서버에_못_닿으면_보낸_데까지만_기억하고_다음에_잇는다(inbox: Path) -> None:
    inbox.write_text(line("location") + "\n" + line("sleep") + "\n")
    report, state = run(FakeServer(down_after=1))
    assert report.sent == 1
    assert report.stopped

    server = FakeServer()
    report, state = run(server, state)

    assert [body["kind"] for _, body in server.got] == ["sleep"]


def test_파일이_새로_만들어지면_처음부터_다시_읽는다(inbox: Path) -> None:
    inbox.write_text(line("location") + "\n")
    _, state = run(FakeServer())

    # 지우고 다시 만들었다. 길이는 더 길어서 위치만으론 모른다.
    inbox.write_text(line("sleep") + "\n" + line("resting_heart_rate") + "\n")
    server = FakeServer()
    report, _ = run(server, state)

    assert [body["kind"] for _, body in server.got] == ["sleep", "resting_heart_rate"]
    assert any("바뀌었다" in note for note in report.notes)


def test_거절된_줄을_되살리면_다음_배달에_간다(inbox: Path) -> None:
    inbox.write_text(line("mac_app_before_deploy") + "\n")
    _, state = run(FakeServer(refuse="mac_app_before_deploy"))

    replay()
    server = FakeServer()
    report, _ = run(server, state)

    assert [body["kind"] for _, body in server.got] == ["mac_app_before_deploy"]
    assert report.rejected == 0


def test_맥_앱_수집기가_적은_줄이_그대로_배달된다(inbox: Path, tmp_path: Path) -> None:
    start = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)
    outbox = tmp_path / "state" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "mac_apps-2026-10.jsonl").write_text(
        to_line([Span("Cursor", start, start + timedelta(minutes=30))]) + "\n"
    )
    server = FakeServer()
    run(server)

    route, body = server.got[0]
    assert route == "/ingest/traces"
    assert (body["source"], body["kind"]) == ("mac_apps", "mac_app")


def test_UTF8_이_아닌_바이트가_든_줄만_따로_두고_앞뒤_줄은_보낸다(inbox: Path) -> None:
    # 예전엔 한 바이트에 파일 전체가 멈췄다. 그 앞의 멀쩡한 줄까지.
    broken = b'{"to": "samples", "kind": "x\xff"}\n'
    data = (line("location") + "\n").encode() + broken + (line("sleep") + "\n").encode()
    inbox.write_bytes(data)
    server = FakeServer()
    report, state = run(server)

    assert [body["kind"] for _, body in server.got] == ["location", "sleep"]
    assert report.rejected == 1
    assert state[str(inbox)]["offset"] == len(data)


def test_줄바꿈_없이_붙은_깨진_객체가_뒤를_막지_않는다(inbox: Path) -> None:
    # 단축어에서 "새 줄 만들기"를 끄면 줄이 이어 붙는다. 다음 줄의 `{"to":` 에서 다시 잡는다.
    inbox.write_text(line("location") + '{"to": "samples", 깨짐' + line("sleep") + "\n")
    server = FakeServer()
    report, _ = run(server)

    assert [body["kind"] for _, body in server.got] == ["location", "sleep"]
    assert report.rejected == 1


def test_받은_편지함을_못_열어도_맥_상자는_보낸다(
    inbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # iCloud 폴더가 권한에 막혀도 앱 사용 기록은 나가야 한다. 예전엔 목록에서 죽었다.
    outbox = tmp_path / "state" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / "mac_apps-2026-10.jsonl").write_text(line("mac_app") + "\n")
    monkeypatch.setenv("JARVIS_INGEST_TOKEN", "t")
    got: List[str] = []
    monkeypatch.setattr(
        "collectors.forward.post", lambda server, auth, route, body: got.append(body["kind"]) or {}
    )
    os.chmod(inbox.parent, 0)
    try:
        code = main([])
    finally:
        os.chmod(inbox.parent, 0o755)

    assert got == ["mac_app"]
    assert code == 1
    assert "못 열었다" in capsys.readouterr().out


def test_받은_편지함이_없으면_만들고_시험_실행은_만들지_않는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "icloud" / "jarvis"
    folder.parent.mkdir()
    monkeypatch.setenv("JARVIS_INBOX", str(folder))
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))

    assert not files(create=False).paths and not folder.exists()
    found = files()

    assert folder.is_dir()
    assert any("만들었다" in note for note in found.notes)
