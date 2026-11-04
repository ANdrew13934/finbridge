"""Parsers for bank SMS (Priorbank, BelVEB).

Each parser returns a CardOp or None if the SMS is not a card operation
(OTP codes, payment confirmations, ads).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass
class CardOp:
    bank: str
    card: str
    bank_time: datetime          # naive, bank's timezone (Minsk)
    kind: str                    # "Expense" | "Income"
    op_word: str                 # bank's word for the operation: Oplata, Perevod, Spisanie...
    amount: Decimal              # in `currency`
    currency: str
    merchant: str                # raw merchant / description
    balance: Optional[Decimal]   # balance after the operation
    balance_currency: Optional[str]
    raw: str


class UnknownOperation(Exception):
    """SMS has the usual shape but an operation word we don't know.
    `op` still carries amount and balance (kind=""), so the bridge can tell
    income from expense by the balance change."""
    def __init__(self, msg: str, text: str, op: Optional["CardOp"] = None):
        super().__init__(msg)
        self.text = text
        self.op = op


def _num(s: str) -> Decimal:
    return Decimal(s.replace(",", "."))


# Karta 4***1234 03-11-26 08:12:44. Oplata 52000.00 VND. VNM Grab* A-AB12CD34EF56GH. Balance: 806.15 BYN Tel. 7299090
_PRIOR = re.compile(
    r"Karta\s+(?P<card>\S+)\s+(?P<date>\d{2}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\.\s*"
    r"(?P<op>[A-Za-z ]+?)\s+(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<cur>[A-Z]{3})\.\s*"
    r"(?P<merchant>.*?)\.?\s*Balance:\s*(?P<bal>-?\d+(?:[.,]\d+)?)\s+(?P<balcur>[A-Z]{3})",
    re.S,
)

# 28/06 13:30. Na vashu kartu zachisleno 20.00 BYN. Dostupnaja summa: 140.50 BYN. Tel. 7299090
_PRIOR_IN = re.compile(
    r"^(?P<d>\d{2})/(?P<m>\d{2})\s+(?P<time>\d{2}:\d{2})\.\s*Na vashu kartu zachisleno\s+"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<cur>[A-Z]{3})\.\s*Dostupnaja summa:\s*"
    r"(?P<bal>-?\d+(?:[.,]\d+)?)\s+(?P<balcur>[A-Z]{3})",
    re.S,
)

# Priorbank's word for the operation. Unknown words -> SMS is reported, not guessed.
_PRIOR_KIND = {
    "oplata": "Expense",
    "perevod": "Expense",
    "snyatie nalichnyh": "Expense",
    "nalichnye": "Expense",
    "otmena": "Income",       # cancelled purchase -> money back
    "vozvrat": "Income",
    "zachislenie": "Income",
    "popolnenie": "Income",
    "postuplenie": "Income",
}

# Incoming-money SMS have no card number; config.json maps this name to the account.
PRIOR_INCOMING_CARD = "Priorbank*"


def parse_priorbank(text: str, received: Optional[datetime] = None) -> Optional[CardOp]:
    m = _PRIOR.search(text)
    if m:
        op = m["op"].strip()
        kind = _PRIOR_KIND.get(op.lower())
        if kind is None and re.search(r"nalichn|bankomat|\batm\b|snyatie", op, re.I):
            kind = "Expense"   # 'Nalichnye v bankomate', 'Snyatie nalichnyh v ATM', ...
        res = CardOp(
            bank="Priorbank", card=m["card"],
            bank_time=datetime.strptime(f"{m['date']} {m['time']}", "%d-%m-%y %H:%M:%S"),
            kind=kind or "", op_word=op, amount=_num(m["amount"]), currency=m["cur"],
            merchant=m["merchant"].strip(), balance=_num(m["bal"]), balance_currency=m["balcur"],
            raw=text,
        )
        if kind is None:
            raise UnknownOperation(f"Priorbank: unknown operation '{op}'", text, res)
        return res
    m = _PRIOR_IN.search(text)
    if m:
        ref = received or datetime.now()
        year = ref.year - (1 if int(m["m"]) > ref.month else 0)   # SMS from December read in January
        return CardOp(
            bank="Priorbank", card=PRIOR_INCOMING_CARD,
            bank_time=datetime.strptime(f"{m['d']}.{m['m']}.{year} {m['time']}", "%d.%m.%Y %H:%M"),
            kind="Income", op_word="Zachisleno", amount=_num(m["amount"]), currency=m["cur"],
            merchant="Зачисление на карту", balance=_num(m["bal"]), balance_currency=m["balcur"],
            raw=text,
        )
    return None


# Karta 9.1234 03.11.2026 09:00:00
# Zachislenie + 250.00 BYN Equation - Izmenenie ostatka OK.
# Dostupno 430.00 BYN
_BELVEB = re.compile(
    r"Karta\s+(?P<card>\S+)\s+(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<op>[A-Za-z ]+?)\s*(?P<sign>[-+])\s*(?P<amount>\d+(?:[.,]\d+)?)\s+(?P<cur>[A-Z]{3})\s+"
    r"(?P<merchant>.*?)\s+(?P<status>OK|[A-Z]{2,})\.?\s+"
    r"(?:Dostupno|Остаток|Ostatok)\s+(?P<bal>-?\d+(?:[.,]\d+)?)\s+(?P<balcur>[A-Z]{3})",
    re.S,
)


def parse_belveb(text: str, received: Optional[datetime] = None) -> Optional[CardOp]:
    m = _BELVEB.search(text)
    if not m:
        return None
    if m["status"] != "OK":
        return None  # declined / not completed
    return CardOp(
        bank="BelVEB", card=m["card"],
        bank_time=datetime.strptime(f"{m['date']} {m['time']}", "%d.%m.%Y %H:%M:%S"),
        kind="Expense" if m["sign"] == "-" else "Income", op_word=m["op"].strip(),
        amount=_num(m["amount"]), currency=m["cur"],
        merchant=re.sub(r"\s+", " ", m["merchant"]).strip(),
        balance=_num(m["bal"]), balance_currency=m["balcur"], raw=text,
    )


# 123456 Код для операции БелВЭБ. ERIP, ERIP 15.00 / ... Card to card, 120.00 BYN
_OTP = re.compile(r"Код для операции[^.]*\.\s*(?P<what>ERIP|Card to card)\D*(?P<amount>\d+(?:[.,]\d+)?)", re.I)


def parse_otp(text: str) -> Optional[tuple[str, Decimal]]:
    """Confirmation-code SMS tell what the next 'Equation' debit is (ERIP payment, card transfer)."""
    m = _OTP.search(text)
    return (m["what"].upper(), _num(m["amount"])) if m else None


PARSERS = {
    "Priorbank": parse_priorbank,
    "BelVEB24.BY": parse_belveb,
}


def parse_sms(sender: str, text: str, received: Optional[datetime] = None) -> Optional[CardOp]:
    parser = PARSERS.get(sender)
    if parser:
        return parser(text, received)
    for p in PARSERS.values():   # unknown sender name: try every parser
        op = p(text, received)
        if op:
            return op
    return None


_ID_LIKE = re.compile(r"^(?:(?=.*\d)[A-Z0-9-]{6,}|[A-Z]-[A-Z0-9]{8,})$")
_CITY = r"(MINSK|MINSKIY R-N|MOSKVA|MOSCOW|SANKT-PETERBU|St Petersburg|Minsk)"


def clean_merchant(merchant: str) -> str:
    """'VNM Grab* A-AW5HSOEGX5R6AV' -> 'Grab'; 'YANDEX*4121*TAXI Moscow RUS' -> 'YANDEX TAXI';
    'SHOP "SOSEDI" MINSK' -> 'SHOP "SOSEDI"'."""
    s = merchant.strip()
    s = re.sub(r"^[A-Z]{3}\s+", "", s)                       # leading country code
    parts = [p.strip() for p in s.split("*")]
    parts = [p for p in parts if p and not p.isdigit() and not _ID_LIKE.match(p)]
    s = " ".join(parts) if parts else s
    s = re.sub(r"\s+(BLR|VNM|RUS|TUR|KAZ)$", "", s)          # trailing country code
    s = re.sub(rf"\s+{_CITY}$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s or merchant.strip()
