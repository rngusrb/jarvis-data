"""결제 하나 — 카드 승인 문자·애플페이 결제 흔적을 같은 꼴로 읽고, 같은 결제를 하나로, 쓴 돈을 센다.

흔적은 원문 그대로 쌓고(`card_sms` — 문자 본문, `wallet_payment` — 단축어가 넘긴 값), 읽기는 볼
때마다 여기서 한다. 읽는 법을 고치면 지난 문자까지 바로 고쳐 읽힌다 — 받을 때 읽어 숫자만 남기면
잘못 읽은 값이 그대로 굳는다.

**문자 꼴은 실제 문자로 확인하기 전이다**(2026-11-12). 카드사 문자에 흔한 조각(금액 "12,000원", 때
"11/12 08:31", 승인·취소, 누적·잔액)만 보고 읽는다. 짐작하지 않는다 — 때(월/일 시:분)가 없거나,
결제 금액으로 남는 것이 하나가 아니거나, 환불처럼 뜻이 갈리거나, 원화가 아니면 읽지 않고 "못 읽은
기록"으로 센다. 계산(합계·이번 주·이번 달)은 전부 코드가 한다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from src.core.clock import KST, local
from src.core.models import Trace

SMS = "card_sms"
WALLET = "wallet_payment"
BY_SMS = "문자"
BY_WALLET = "애플페이"
# 맥락에 "마지막으로 받은 때"를 적을 때의 이름.
WAYS = {SMS: "카드 문자", WALLET: "애플페이"}

# 금액 — "12,000원", "12000원", "12,000 원". 앞이 숫자·쉼표면 더 긴 수의 꼬리다.
AMOUNT = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d+)\s*원")
# 결제 금액이 아닌 금액 — 바로 앞(이 글자 수 안)에 이 말이 있다("누적1,234,567원", "잔액 50,000원",
# "결제예정금액 512,340원", "대출금액 30,000,000원").
NOT_PAID = ("누적", "잔액", "한도", "가능", "예정", "포인트", "청구", "할인", "대출")
LOOK_BACK = 8
# 때 — "11/12 08:31". 해는 문자에 없다. 없으면 결제 문자로 보지 않는다("승인 났어? 30,000원
# 보냈어").
WHEN = re.compile(r"(\d{1,2})/(\d{1,2})\s*(\d{1,2}):(\d{2})")
APPROVED = "승인"
# 꼴이 확실한 취소·거절만 그렇게 읽는다. 승인 문자 꼬리의 "취소문의 1588-…"는 안내다.
SURE_CANCEL = re.compile(r"승인\s*취소|취소\s*승인|(?:카드|\d{4}\)?)\s*취소")
SURE_DECLINE = re.compile(r"(?:승인|결제)\s*(?:거절|실패|불가)")
FOOTER = re.compile(r"취소\s*문의")
# 그 밖의 자리에 이 말이 있으면 꼴을 짐작하지 않는다 — 못 읽은 것으로(보인다). 좁은 정규식으로 갈라
# 보다가 "승인취소 안내"를 승인(+), "승인이 거절됨"을 결제로 셌고, 가맹점 "불가리"는 거절로 흔적
# 없이 사라졌다 (JAR-045 2차 리뷰 재현). 예시 문자를 받으면 그 꼴을 확실한 쪽에 더한다.
DOUBT = re.compile(r"취소|거절|실패|불가")
# 뜻이 갈리는 것 — 환불이 취소인지 따로 돌려받은 돈인지, 대출·카드론·현금서비스·리볼빙이 쓴 돈인지
# 문자만으로는 모른다("카드론 승인 3,000,000원"을 결제로 셌다 — 2차 리뷰).
UNSURE = ("환불", "대출", "카드론", "현금서비스", "리볼빙")
# 원화가 아닌 금액 — 짐작해 원으로 바꾸지 않는다("$12.00" 을 12원으로, "12 달러"를 12원으로 읽었다 —
# 1·2차 리뷰).
FOREIGN = re.compile(r"[$€¥£]|USD|EUR|JPY|CNY|GBP|달러|엔|유로|위안|파운드", re.IGNORECASE)
# 음수 표시 — "-₩4,500", "−₩4,500"(U+2212), "(₩4,500)".
NEGATIVE = re.compile(r"[-−(]")
# 두 길(문자·애플페이)로 온 같은 결제. 문자의 때는 분까지라 몇 분은 벌어진다.
SAME_PAYMENT = timedelta(minutes=5)
# 같은 애플페이 결제가 두 번 들어온 것(파일·직접 둘 다 쓰며 본문을 따로 만든 경우).
SAME_TAP = timedelta(seconds=10)
# 이만큼 아무것도 안 들어온 길에는 "끊겼는지 모른다"를 붙인다.
QUIET_NOTE = timedelta(days=2)
WEEKDAYS = "월화수목금토일"
# 오늘·어제 줄에 낱낱이 적는 결제 수. 넘으면 "외 N건".
LISTED = 8


@dataclass(frozen=True)
class Payment:
    at: datetime
    # 원. 취소는 음수 — 합계에서 저절로 빠진다.
    amount: int
    # 가맹점. 문자에서 못 찾으면 "".
    merchant: str
    # 카드. 모르면 "".
    card: str
    # 어느 길로 왔나 — "문자" · "애플페이" · "문자+애플페이"(둘을 하나로 합친 것).
    via: str


def _paid_amounts(text: str) -> List[int]:
    found = []
    for match in AMOUNT.finditer(text):
        before = text[max(0, match.start() - LOOK_BACK) : match.start()]
        if any(word in before for word in NOT_PAID):
            continue
        found.append(int(match.group(1).replace(",", "")))
    return found


def _when(text: str, received: datetime) -> Tuple[datetime, Optional[re.Match[str]]]:
    """문자에 적힌 때. 해는 받은 때의 해 — 받은 때보다 이틀 넘게 뒤면 작년(12/31 결제를
    1/1 에 받음)."""
    match = WHEN.search(text)
    if match is None:
        return received, None
    month, day, hour, minute = (int(g) for g in match.groups())
    got = local(received)
    try:
        at = datetime(got.year, month, day, hour, minute, tzinfo=KST)
    except ValueError:
        return received, None
    if at - got > timedelta(days=2):
        at = at.replace(year=got.year - 1)
    return at, match


def _merchant(text: str, when: re.Match[str]) -> str:
    """때 바로 뒤(같은 줄), 없으면 그다음 줄. 누적·잔액 같은 꼬리는 뗀다."""
    for line in text[when.end() :].split("\n"):
        line = line.strip()
        for word in NOT_PAID:
            line = line.split(word)[0].strip()
        if line and not AMOUNT.search(line):
            return line
    return ""


def _card(text: str) -> str:
    for line in text.split("\n"):
        found = re.search(r"승인|취소", line)
        if found:
            return line[: found.start()].replace("[Web발신]", "").strip()
    return ""


def sms_kind(text: str) -> Optional[str]:
    """ "승인" · "취소" · "거절" · "아님"(결제 문자가 아니다 — 때나 금액이 없는 안내). 꼴이 확실하지
    않으면 None(못 읽음 — 보인다)."""
    body = FOOTER.sub("", text)
    if any(word in body for word in UNSURE):
        return None
    if SURE_DECLINE.search(body):
        return "거절"
    if SURE_CANCEL.search(body):
        return "취소"
    if DOUBT.search(body):
        return None
    if APPROVED in body:
        return "승인"
    # 승인·취소라는 말이 없는데 때와 금액이 있으면 모르는 꼴의 결제 문자일 수 있다 — 못 읽음으로
    # 보인다. 둘 다 없으면 안내 문자다(결제 예정 금액 등).
    return None if WHEN.search(body) and AMOUNT.search(body) else "아님"


def from_sms(trace: Trace) -> Optional[Payment]:
    """카드 승인 문자 하나. 결제가 아니거나(거절) 못 읽으면 None — 둘의 구별은 `read`."""
    text = trace.text.replace("\r\n", "\n")
    kind = sms_kind(text)
    if kind not in ("승인", "취소"):
        return None
    paid = _paid_amounts(text)
    at, when = _when(text, trace.at)
    if when is None or len(paid) != 1 or paid[0] <= 0:
        return None
    amount = -paid[0] if kind == "취소" else paid[0]
    return Payment(at, amount, _merchant(text, when), _card(text), BY_SMS)


def _won(value: Any) -> Optional[int]:
    """단축어가 넘긴 금액 — 12000, 12000.0, "12,000원", "₩12,000", "12,000.00", "₩0"(교통 탭).
    원화가 아니거나 음수거나 숫자가 없으면 None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if FOREIGN.search(text) or NEGATIVE.search(text):
        return None
    whole, _, fraction = text.partition(".")
    if fraction and re.sub(r"[^\d]", "", fraction).strip("0"):
        return None  # "4.500"(천 단위 점?)·"12.5" — 원에는 소수가 없다. 짐작하지 않는다
    digits = re.sub(r"[^\d]", "", whole)
    return int(digits) if digits else None


def from_wallet(trace: Trace) -> Optional[Payment]:
    """애플페이 결제 하나 — 지갑 자동화가 넘긴 금액·가맹점·카드(SETUP 2-9). 금액을 못
    읽으면 None. 금액 0(교통 탭)도 그대로 돌려준다 — 결제가 아닌 것은 `read` 가 거른다."""
    meta = trace.meta or {}
    raw = meta.get("amount")
    if raw is None or raw == "":
        # 금액 칸이 없을 때만 글에서 찾는다. 칸이 있는데 못 읽은 것("$12.00")은 짐작하지 않는다.
        paid = _paid_amounts(trace.text)
        amount = paid[0] if len(paid) == 1 and not FOREIGN.search(trace.text) else None
    else:
        amount = _won(raw)
    if amount is None:
        return None
    merchant = str(meta.get("merchant") or "").strip()
    return Payment(trace.at, amount, merchant, str(meta.get("card") or "").strip(), BY_WALLET)


@dataclass(frozen=True)
class Reading:
    payments: List[Payment]
    # 못 읽은 흔적의 받은 때 — 기간마다 "빠졌을 수 있다"를 붙인다(2차 리뷰).
    unread: List[datetime]
    # 승인이 안 된 결제(거절·실패·불가) — 합계에 안 넣되 센다(2차 리뷰: 흔적 없이 사라졌다).
    declined: int


def read(traces: Sequence[Trace]) -> Reading:
    """흔적들 → 결제들, 못 읽은 것, 거절. 안내 문자와 금액 0(교통 탭)은 결제가 아니다.

    같은 것이 두 번 들어오면 하나로 — 본문이 같은 문자, 10초 안의 같은 애플페이 값. 저장소는 때가
    1초만 달라도 둘로 둔다(JAR-045 1차 리뷰 재현: 4,500원 하나가 "2건 9,000원"). 대가: 누적·잔액이
    안 붙는 꼴이면 같은 분에 같은 가게에서 같은 금액을 두 번 산 것도 하나가 된다."""
    payments: List[Payment] = []
    unread: List[datetime] = []
    declined = 0
    seen_sms: Set[str] = set()
    last_tap: Dict[str, datetime] = {}
    for trace in sorted(traces, key=lambda t: t.at):
        if trace.kind == SMS:
            body = trace.text.strip()
            if body in seen_sms:
                continue
            seen_sms.add(body)
            payment = from_sms(trace)
            if payment is None:
                kind = sms_kind(body)
                if kind == "거절":
                    declined += 1
                elif kind != "아님":
                    unread.append(trace.at)
                continue
        elif trace.kind == WALLET:
            key = trace.text.strip() + json.dumps(trace.meta or {}, sort_keys=True, default=str)
            before = last_tap.get(key)
            last_tap[key] = trace.at
            if before is not None and trace.at - before <= SAME_TAP:
                continue
            payment = from_wallet(trace)
            if payment is None:
                unread.append(trace.at)
                continue
            if payment.amount == 0:
                continue
        else:
            continue
        payments.append(payment)
    return Reading(payments, unread, declined)


def merge(payments: Sequence[Payment]) -> List[Payment]:
    """두 길로 온 같은 결제를 하나로 — 금액이 같고 5분 안이면. 같은 길로 온 것끼리는 합치지 않는다
    (같은 가게에서 같은 금액을 두 번 결제할 수 있다). 가맹점·카드 이름은 문자 쪽, 때는 둘 중
    이른 쪽(문자 23:59 와 애플페이 00:00:10 이 다음 날로 넘어가지 않게)."""
    out: List[Payment] = []
    for payment in sorted(payments, key=lambda p: p.at):
        twin = next(
            (
                i
                for i, kept in enumerate(out)
                if kept.amount == payment.amount
                and {kept.via, payment.via} == {BY_SMS, BY_WALLET}
                and abs(kept.at - payment.at) <= SAME_PAYMENT
            ),
            None,
        )
        if twin is None:
            out.append(payment)
            continue
        sms, wallet = (out[twin], payment) if out[twin].via == BY_SMS else (payment, out[twin])
        out[twin] = Payment(
            at=min(sms.at, wallet.at),
            amount=sms.amount,
            merchant=sms.merchant or wallet.merchant,
            card=sms.card or wallet.card,
            via=f"{BY_SMS}+{BY_WALLET}",
        )
    return sorted(out, key=lambda p: p.at)


def _day(day: date) -> str:
    return f"{day:%m/%d}({WEEKDAYS[day.weekday()]})"


def _total(payments: Sequence[Payment]) -> str:
    spent = [p for p in payments if p.amount > 0]
    canceled = [p for p in payments if p.amount < 0]
    text = f"결제 {len(spent)}건 {sum(p.amount for p in spent):,}원"
    if canceled:
        back = -sum(p.amount for p in canceled)
        net = sum(p.amount for p in payments)
        text += f", 취소 {len(canceled)}건 {back:,}원 → 빼면 {net:,}원"
        if net < 0:
            # 앞 기간 결제의 취소만 들어온 때. 음수를 "썼다"로 읽지 않게(리뷰).
            text += "(이 기간 앞의 결제가 취소됐다)"
    return text


def _listed(payments: Sequence[Payment]) -> str:
    items = []
    for p in payments[:LISTED]:
        what = p.merchant or "가맹점 모름"
        if p.amount < 0:
            what += " 취소"
        items.append(f"{local(p.at):%H:%M} {what} {abs(p.amount):,}원")
    more = f" 외 {len(payments) - LISTED}건" if len(payments) > LISTED else ""
    return " · ".join(items) + more


def _last_line(last: Mapping[str, datetime], now: datetime) -> Optional[str]:
    """길마다 마지막으로 받은 때. 끊긴 수집이 "0원"으로 읽히면 안 된다(JAR-045 1차 리뷰, High)."""
    parts = []
    for kind, name in WAYS.items():
        if kind not in last:
            continue
        when = local(last[kind])
        gap = now - last[kind]
        note = ""
        if gap > QUIET_NOTE:
            note = f" — {gap.days}일째 없음(그 뒤 결제가 없었는지 받기가 끊겼는지는 모른다)"
        parts.append(f"{name} {_day(when.date())} {when:%H:%M}{note}")
    return "- 마지막으로 받은 때: " + " · ".join(parts) if parts else None


def summarize(
    payments: Sequence[Payment],
    unread: Sequence[datetime],
    declined: int,
    first: date,
    now: datetime,
    window_days: int,
    last: Mapping[str, datetime],
) -> List[str]:
    """마지막으로 받은 때, 오늘·어제(낱낱이), 이번 주(월~오늘)·지난주·이번 달(합계). 받기 시작한 날
    (`first`) 전이 섞인 기간에는 그렇다고 적는다 — 11/5 에 받기 시작했는데 "이번 달 N원"이면 달
    전체로 읽힌다."""
    today = local(now).date()

    def within(start: date, end: date) -> List[Payment]:
        return [p for p in payments if start <= local(p.at).date() <= end]

    def missing(start: date, end: date) -> str:
        # 못 읽은 것이 그 기간에 들어왔으면 합계가 모자랄 수 있다고 그 줄에 붙인다(2차 리뷰: 끝에만
        # 적으니 기간마다 "0건 0원"이 사실처럼 보였다).
        count = sum(1 for at in unread if start <= local(at).date() <= end)
        return f" — 못 읽은 기록 {count}건이 이 기간에 있다(빠졌을 수 있다)" if count else ""

    lines = []
    last_line = _last_line(last, now)
    if last_line:
        lines.append(last_line)
    for name, day in (("오늘", today), ("어제", today - timedelta(days=1))):
        if day < first:
            continue
        found = within(day, day)
        detail = f" — {_listed(found)}" if found else ""
        lines.append(f"- {name} {_day(day)}: {_total(found)}{detail}{missing(day, day)}")
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    last_week = (week - timedelta(days=7), week - timedelta(days=1))
    periods = (
        (f"이번 주({_day(week)}~오늘)", week, today),
        (f"지난주({_day(last_week[0])}~{_day(last_week[1])})", *last_week),
        (f"이번 달({month:%m/%d}~오늘)", month, today),
    )
    for name, start, end in periods:
        if end < first:
            lines.append(f"- {name}: 받기 전이라 모름")
            continue
        note = f" — {first:%m/%d}부터 받은 것만" if first > start else ""
        lines.append(f"- {name}: {_total(within(start, end))}{note}{missing(start, end)}")
    if unread:
        lines.append(
            f"- 결제로 못 읽은 기록 {len(unread)}건(최근 {window_days}일) — 합계에 안 넣었다"
        )
    if declined:
        lines.append(
            f"- 승인 안 된 결제(거절·실패) {declined}건(최근 {window_days}일) — 합계에 안 넣었다"
        )
    return lines
