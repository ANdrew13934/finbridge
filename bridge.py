#!/usr/bin/env python3
"""Bank SMS -> «Финансы» app.

  python3 bridge.py --dry-run          show what would be added, change nothing
  python3 bridge.py                    add new operations and let the app sync them
  python3 bridge.py --sms-file F       take SMS from a text file (blocks separated by '---')
  python3 bridge.py --list-senders     show SMS senders found in Messages
  python3 bridge.py --test-tx          add one 0.01 test expense to «Наличные» and sync it
  python3 bridge.py --check-config     check that every account / category in config.json exists
  python3 bridge.py --fix-balances     align card / wallet balances with the bank SMS / blockchain
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from itertools import combinations
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bybit_source import (Bybit, BybitError, chain_deposits, funding_balance, funding_history,  # noqa: E402
                          internal_deposits, key_expiry, withdrawals)
from crypto_sources import tron_usdt_balance, tron_usdt_transfers  # noqa: E402
from finance_db import FinanceDB, decimals, to_minor  # noqa: E402
from messages_source import Sms, list_senders, read_sms  # noqa: E402
from messages_source import stats as sms_stats  # noqa: E402
from sms_parsers import CardOp, UnknownOperation, clean_merchant, parse_otp, parse_sms  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
BACKUP_DIR = HERE / "backups"
log = logging.getLogger("finbridge")

PAIR_WINDOW = timedelta(minutes=30)   # debit on one card + credit on another = own transfer
HOLD_FOR_PAIR = timedelta(minutes=20)  # wait this long for the other half before writing
HOLD_FOR_CRYPTO = timedelta(hours=6)   # card debit may be paid back in USDT by a friend later
CRYPTO_WINDOW = (timedelta(hours=-2), timedelta(hours=12))  # USDT arrival relative to the BYN debit
BYN_PER_USDT = (Decimal("2.6"), Decimal("3.4"))              # plausible exchange rate
HOLD_FOR_CANCEL = 10                   # minutes a Bybit Pay purchase waits in case it gets cancelled
HOLD_FOR_LATE_SMS = 30                 # minutes a ≈ card payment waits for the SMS that came out of order
TRANSIT_WINDOW = timedelta(hours=1)    # USDT in, then almost the same out = money just passing through
TRANSIT_MAX_KEEP = Decimal("0.03")     # what stays behind is at most 3% of what came in


# ---------------------------------------------------------------- state

def load_state() -> dict:
    st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    st.setdefault("last_rowid", 0)
    st.setdefault("balance_history", {})
    st.setdefault("processed", {})
    st.setdefault("unparsed", [])
    st.setdefault("pending", [])
    st.setdefault("recent_otp", [])
    return st


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str))
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------- planning

@dataclass
class Item:
    """One entry to write: a transaction ("tx") or a transfer between accounts."""
    kind: str
    guids: list[str]
    texts: list[str]
    bank_time: str                # ISO, aware
    day: str                      # ISO date, user's local time
    comment: str = ""
    notes: list[str] = field(default_factory=list)
    # transaction
    tx_type: str = ""
    account: str = ""
    category: str = ""
    amount: str = ""              # in account currency
    real_amount: Optional[str] = None
    real_currency: Optional[str] = None
    pairable: bool = False        # may be half of a transfer between own accounts
    crypto: bool = False          # comes from a crypto wallet / exchange
    hold_minutes: int = 20        # how long a pairable item waits for its other half
    wait_minutes: int = 0         # any item: write only after this many minutes
    # estimated by the app's rate (≈): recheck against card balances that arrive later
    bal_key: str = ""
    bal_after: Optional[str] = None
    estimate: Optional[str] = None
    # transfer
    from_account: str = ""
    to_account: str = ""
    to_amount: Optional[str] = None

    @property
    def time(self) -> datetime:
        return datetime.fromisoformat(self.bank_time)


def bybit_on(cfg: dict) -> bool:
    return bool(cfg.get("bybit")) and cfg["bybit"].get("enabled", True)


def check_config(cfg: dict, db: FinanceDB) -> list[str]:
    """Every account / category uid in config.json must exist in the app (and not be deleted)."""
    problems: list[str] = []
    hint = " (список: python3 show_ids.py)"

    def acc(uid: str, where: str) -> None:
        try:
            a = db.account(uid)
        except KeyError:
            problems.append(f"{where}: счёта {uid!r} нет в приложении{hint}")
            return
        if a["isRemoved"]:
            problems.append(f"{where}: счёт «{a['title']}» удалён в приложении")

    def cat(uid: str, where: str, kind: Optional[str]) -> None:
        try:
            c = db.category(uid)
        except KeyError:
            problems.append(f"{where}: категории {uid!r} нет в приложении{hint}")
            return
        if c["isRemoved"]:
            problems.append(f"{where}: категория «{c['title']}» удалена в приложении")
        elif kind and c["type"] != kind:
            problems.append(f"{where}: категория «{c['title']}» — это {c['type']}, а нужна {kind}")

    for card, c in cfg.get("cards", {}).items():
        acc(c["account"], f"cards «{card}»")
    for ccy, uid in cfg.get("cash_accounts", {}).items():
        acc(uid, f"cash_accounts «{ccy}»")
    for r in cfg.get("category_rules", []):
        cat(r["category"], f"category_rules «{r.get('title') or r['pattern'][:25]}»", r["type"])
    for kind, uid in cfg.get("default_categories", {}).items():
        cat(uid, f"default_categories «{kind}»", kind)
    for kind, uid in cfg.get("crypto_default_categories", {}).items():
        cat(uid, f"crypto_default_categories «{kind}»", kind)
    if cfg.get("tax_rule"):
        cat(cfg["tax_rule"]["category"], "tax_rule.category", "Expense")
        cat(cfg["tax_rule"]["income_category"], "tax_rule.income_category", "Income")
    for w in cfg.get("tron_wallets", []):
        acc(w["account"], f"tron_wallets «{w.get('title') or w['address']}»")
    for addr, label in cfg.get("address_labels", {}).items():
        for key, kind in (("category", None), ("category_expense", "Expense"), ("category_income", "Income")):
            if label.get(key):
                cat(label[key], f"address_labels «{label.get('title') or addr}»", kind)
    if bybit_on(cfg):
        acc(cfg["bybit"]["account"], "bybit.account")
        cat(cfg["bybit"]["pay_category"], "bybit.pay_category", "Expense")
    return problems


def match_rule(rules: list[dict], op: CardOp, text: str) -> Optional[dict]:
    for r in rules:
        if r["type"] == op.kind and r.get("bank", op.bank) == op.bank and re.search(r["pattern"], text, re.I):
            return r
    return None


BANK_MARKUP = Decimal("1.03")  # Priorbank charges ~3% above the app's rate (seen Oct-Nov 2026)


def account_amount(db: FinanceDB, op: CardOp, acc_ccy: str, prev: Optional[dict],
                   nearby: list[Decimal] = ()) -> tuple[Decimal, list[str]]:
    """Amount in the account currency. For foreign-currency payments use the change
    of the card balance (what the bank actually charged).
    SMS often arrive out of order, so if the balance from the SMS received just
    before doesn't fit, try other balances of the same days and take the one whose
    difference is closest to the expected charge."""
    if op.currency == acc_ccy:
        return op.amount, []
    r = db.rate(acc_ccy, op.currency)
    estimate = (op.amount / r).quantize(Decimal("0.01")) if r else None
    if prev and op.balance is not None and prev["currency"] == op.balance_currency == acc_ccy:
        p = Decimal(prev["balance"])
        delta = p - op.balance if op.kind == "Expense" else op.balance - p
        if delta > 0 and (estimate is None or Decimal("0.8") * estimate <= delta <= Decimal("1.25") * estimate):
            return delta, []
        note = f"остаток не сходится (разница {delta}), взят курс приложения"
    else:
        note = "нет предыдущего остатка, взят курс приложения"
    if estimate and op.balance is not None and op.balance_currency == acc_ccy:
        want = estimate * BANK_MARKUP
        deltas = [(b - op.balance if op.kind == "Expense" else op.balance - b) for b in nearby if b != op.balance]
        fits = [d for d in deltas if Decimal("0.95") * estimate <= d <= Decimal("1.10") * estimate]
        if fits:
            return min(fits, key=lambda d: abs(d - want)), []
    if estimate is None:
        raise ValueError(f"нет курса {acc_ccy}->{op.currency}")
    return estimate, [note]


def looks_like_tax(cfg: dict, db: FinanceDB, op: CardOp, text: str, account: str,
                   day: date, amount: Decimal, items: list[Item]) -> bool:
    rule = cfg.get("tax_rule")
    if not rule or op.kind != "Expense" or op.bank != rule["bank"] or not re.search(rule["pattern"], text, re.I):
        return False
    share = Decimal(str(rule["share"]))
    incomes = [Decimal(i.amount) for i in items
               if i.kind == "tx" and i.tx_type == "Income" and i.account == account
               and i.category == rule["income_category"] and abs((date.fromisoformat(i.day) - day).days) <= 3]
    scale = Decimal(10) ** decimals(db.account(account)["currencyCode"])
    incomes += [Decimal(a) / scale for a in db.recent_income(account, rule["income_category"], day)]
    return any(abs(inc * share - amount) <= max(Decimal("0.01"), amount * Decimal("0.01")) for inc in incomes)


def previous_balance(state: dict, card: str, when: datetime) -> Optional[dict]:
    """Card balance from the latest SMS before `when` (SMS can sync out of order)."""
    prev = None
    for t, bal, ccy in state["balance_history"].get(card, []):
        if datetime.fromisoformat(t) >= when:
            break
        prev = {"balance": bal, "currency": ccy}
    return prev


def nearby_balances(state: dict, card: str, when: datetime, hours: int = 36) -> list[Decimal]:
    return [Decimal(bal) for t, bal, ccy in state["balance_history"].get(card, [])
            if abs(datetime.fromisoformat(t) - when) <= timedelta(hours=hours)]


def remember_balance(state: dict, card: str, when: datetime, balance: Decimal, ccy: str) -> None:
    hist = state["balance_history"].setdefault(card, [])
    entry = [when.isoformat(), str(balance), ccy]
    if entry not in hist:
        hist.append(entry)
        hist.sort(key=lambda e: datetime.fromisoformat(e[0]))
        del hist[:-300]


def otp_label(cfg: dict, state: dict, when: datetime, amount: Decimal) -> Optional[str]:
    """'ЕРИП-платёж' if a confirmation code for the same amount came shortly before."""
    for t, what, amt in reversed(state["recent_otp"]):
        if Decimal(amt) == amount and timedelta(0) <= when - datetime.fromisoformat(t) <= timedelta(minutes=15):
            return cfg.get("otp_labels", {}).get(what)
    return None


def plan_sms(cfg: dict, db: FinanceDB, state: dict, sms_list: list[Sms], since: datetime,
             items: list[Item]) -> list[str]:
    """Turn new SMS into Items (appended to `items`). Returns problems to report."""
    bank_tz = ZoneInfo(cfg["bank_timezone"])
    problems = []
    for sms in sorted(sms_list, key=lambda s: s.received):
        state["last_rowid"] = max(state["last_rowid"], sms.rowid)
        received_bank = sms.received.astimezone(bank_tz).replace(tzinfo=None)
        unknown = None
        try:
            op = parse_sms(sms.sender, sms.text, received_bank)
        except UnknownOperation as e:
            op, unknown = e.op, e
            if op is None or op.balance is None:
                problems.append(f"{e}: {sms.text!r}")
                state["unparsed"].append({"guid": sms.guid, "text": sms.text})
                notify(f"Не понял SMS банка, внеси вручную: {sms.text[:80]}")
                continue
        if op is None:  # codes, confirmations, ads
            otp = parse_otp(sms.text)
            if otp:
                state["recent_otp"] = (state["recent_otp"] + [[sms.received.isoformat(), otp[0], str(otp[1])]])[-30:]
            continue

        # The time printed in the SMS is the merchant's local time (Minsk for online
        # payments, local time for shops abroad), so use the moment the phone got the SMS.
        when = sms.received if sms.exact_time else op.bank_time.replace(tzinfo=bank_tz)
        hist_key = cfg["cards"].get(op.card, {}).get("account", op.card)  # one history per account
        prev = previous_balance(state, hist_key, when)
        if op.balance is not None:
            remember_balance(state, hist_key, when, op.balance, op.balance_currency)
        if unknown:  # unknown word: the balance change tells income from expense
            if prev is None or prev["currency"] != op.balance_currency or Decimal(prev["balance"]) == op.balance:
                problems.append(f"{unknown}: {sms.text!r}")
                state["unparsed"].append({"guid": sms.guid, "text": sms.text})
                notify(f"Не понял SMS банка, внеси вручную: {sms.text[:80]}")
                continue
            op.kind = "Expense" if Decimal(prev["balance"]) > op.balance else "Income"

        if when < since or sms.guid in state["processed"]:
            continue  # only warms up the balance
        card = cfg["cards"].get(op.card)
        if card is None:
            problems.append(f"карта {op.card} не настроена в config.json: {sms.text!r}")
            continue

        account = card["account"]
        acc_ccy = db.account(account)["currencyCode"]
        try:
            amount, notes = account_amount(db, op, acc_ccy, prev, nearby_balances(state, hist_key, when))
        except ValueError as e:
            problems.append(f"{e}: {sms.text!r}")
            continue
        merchant = clean_merchant(op.merchant)
        text = f"{op.op_word} {op.merchant} {merchant}"
        day = when.astimezone().date()
        approx = " (≈)" if notes else ""
        foreign = op.currency != acc_ccy
        base = dict(guids=[sms.guid], texts=[sms.text], bank_time=when.isoformat(),
                    day=day.isoformat(), notes=notes)

        # cash withdrawal -> transfer to the cash account of that currency
        if op.kind == "Expense" and re.search(cfg["atm_pattern"], text, re.I):
            cash = cfg["cash_accounts"].get(op.currency)
            if cash:
                items.append(Item(kind="transfer", from_account=account, to_account=cash,
                                  amount=str(amount), to_amount=str(op.amount) if foreign else None,
                                  comment=f"Банкомат {merchant}{approx}", **base))
                continue

        rule = None if unknown else match_rule(cfg["category_rules"], op, text)
        review = None if rule else match_rule(cfg["review_rules"], op, text)
        if unknown:
            review = {"note": f"незнакомая операция банка «{op.op_word}» — проверь"}
        category = rule["category"] if rule else cfg["default_categories"][op.kind]
        if review and looks_like_tax(cfg, db, op, text, account, day, amount, items):
            category, review = cfg["tax_rule"]["category"], None
        if review:
            notes.append(review["note"])
            label = otp_label(cfg, state, when, amount if not foreign else op.amount)
            if label:
                merchant = label
        items.append(Item(
            kind="tx", tx_type=op.kind, account=account, category=category,
            amount=str(amount), real_amount=str(op.amount) if foreign else None,
            real_currency=op.currency if foreign else None,
            comment=("❓ " if review else "") + merchant + approx,
            pairable=(op.kind == "Expense" and review is not None) or op.kind == "Income",
            hold_minutes=int(HOLD_FOR_CRYPTO.total_seconds() // 60) if (op.kind == "Expense" and review) else 20,
            wait_minutes=HOLD_FOR_LATE_SMS if approx else 0,
            bal_key=hist_key if approx else "", bal_after=str(op.balance) if approx else None,
            estimate=str(amount) if approx else None,
            **base,
        ))
    return problems


def refine_estimates(state: dict, items: list[Item]) -> None:
    """An amount taken at the app's rate (≈) because the SMS of the previous purchase
    hadn't arrived yet: now that more balances are known, use the bank's real charge."""
    for it in items:
        if not (it.kind == "tx" and it.bal_key and it.estimate and it.bal_after):
            continue
        est, after = Decimal(it.estimate), Decimal(it.bal_after)
        deltas = [(b - after if it.tx_type == "Expense" else after - b)
                  for b in nearby_balances(state, it.bal_key, it.time) if b != after]
        fits = [d for d in deltas if Decimal("0.95") * est <= d <= Decimal("1.10") * est]
        if fits:
            it.amount = str(min(fits, key=lambda d: abs(d - est * BANK_MARKUP)))
            it.comment = it.comment.replace(" (≈)", "")
            it.notes = [n for n in it.notes if "курс приложения" not in n]
            it.bal_key = ""


def plan_crypto(cfg: dict, db: FinanceDB, state: dict, since: datetime, items: list[Item]) -> list[str]:
    """USDT transfers of the TRON wallets -> Items."""
    wallets = {w["address"]: w for w in cfg.get("tron_wallets", [])}
    own_other = {}  # own addresses whose history we don't read (exchange deposit address)
    if cfg.get("bybit", {}).get("deposit_address"):
        own_other[cfg["bybit"]["deposit_address"]] = {"account": cfg["bybit"]["account"], "title": "Bybit"}
    labels = cfg.get("address_labels", {})
    problems = []
    checked = state.setdefault("tron_checked", {})
    seen = {g for it in items for g in it.guids}  # already waiting from an earlier run
    for addr, w in wallets.items():
        start = since
        if addr in checked:
            start = max(since, datetime.fromisoformat(checked[addr]) - timedelta(hours=2))
        try:
            txs = tron_usdt_transfers(addr, start)
        except Exception as e:
            problems.append(f"TRON {w['title']}: не удалось получить историю ({e})")
            continue
        checked[addr] = datetime.now(timezone.utc).isoformat()
        for tx in txs:
            key = f"tron:{tx.txid}:{addr}"
            if key in state["processed"] or key in seen or tx.time < since or tx.amount <= 0:
                continue
            seen.add(key)
            base = dict(guids=[key], texts=[f"{tx.direction} {tx.amount} USDT {tx.counterparty} {tx.txid}"],
                        bank_time=tx.time.isoformat(), day=tx.time.astimezone().date().isoformat(), crypto=True)
            other = wallets.get(tx.counterparty)
            if other:  # between own wallets: write once, from the sending side
                if tx.direction == "OUT":
                    items.append(Item(kind="transfer", from_account=w["account"], to_account=other["account"],
                                      amount=str(tx.amount), comment="USDT между своими кошельками", **base))
                else:
                    state["processed"][key] = {"uid": None, "note": "между своими кошельками"}
                continue
            dest = own_other.get(tx.counterparty)
            if dest:
                src, dst = (w["account"], dest["account"]) if tx.direction == "OUT" else (dest["account"], w["account"])
                items.append(Item(kind="transfer", from_account=src, to_account=dst, amount=str(tx.amount),
                                  to_amount=str(tx.amount), comment="USDT " + (f"→ {dest['title']}" if tx.direction == "OUT"
                                                                               else f"с {dest['title']}"), **base))
                continue
            label = labels.get(tx.counterparty, {})
            short = label.get("title") or f"{tx.counterparty[:6]}…{tx.counterparty[-4:]}"
            tx_type = "Income" if tx.direction == "IN" else "Expense"
            category = (label.get(f"category_{tx_type.lower()}") or label.get("category")
                        or cfg.get("crypto_default_categories", {}).get(tx_type))
            if category and db.category(category)["type"] != tx_type:
                category = None
            review = category is None
            items.append(Item(
                kind="tx", tx_type=tx_type, account=w["account"],
                category=category or cfg["default_categories"][tx_type],
                amount=str(tx.amount),
                comment=("❓ " if review else "") + ("от " if tx.direction == "IN" else "→ ") + short + " · USDT",
                notes=["криптоперевод без метки — проверь"] if review else [],
                pairable=tx_type == "Income" and review,
                hold_minutes=int(HOLD_FOR_CRYPTO.total_seconds() // 60),
                **base,
            ))
    return problems


def vnd_estimate(db: FinanceDB, usdt: Decimal) -> Optional[str]:
    """'≈36 000 ₫' using the app's own rates (BYN→VND / BYN→USDT)."""
    vnd, usd = db.rate("BYN", "VND"), db.rate("BYN", "ct:USDT")
    if not vnd or not usd:
        return None
    return "≈" + f"{int(round(usdt * vnd / usd, -3)):,}".replace(",", " ") + " ₫"


def plan_bybit(cfg: dict, db: FinanceDB, state: dict, since: datetime, items: list[Item]) -> list[str]:
    """Bybit Funding account history -> Items.
    Bybit Pay (QR) purchases become expenses, cancelled ones are dropped, deposits from
    own wallets are left to the wallet side, withdrawals to own wallets become transfers,
    money from others waits to be paired with a BYN card debit (exchange through a friend)."""
    if not bybit_on(cfg):
        return []
    bc = cfg["bybit"]
    try:
        b = Bybit()
        start = since
        if state.get("bybit_checked"):
            start = max(since, datetime.fromisoformat(state["bybit_checked"]) - timedelta(hours=2))
        rows = funding_history(b, start)
    except Exception as e:
        return [f"Bybit: не удалось получить историю ({e})"]
    state["bybit_checked"] = datetime.now(timezone.utc).isoformat()

    acc = bc["account"]
    wallets = {w["address"]: w for w in cfg.get("tron_wallets", [])}
    labels = cfg.get("address_labels", {})
    problems: list[str] = []
    cache: dict[str, list[dict]] = {}

    def records(name, fetch):  # deposit / withdrawal details, fetched only when needed
        if name not in cache:
            try:
                cache[name] = fetch(b, start - timedelta(days=1))
            except Exception as e:
                problems.append(f"Bybit: {name}: {e}")
                cache[name] = []
        return cache[name]

    def near(ts, row, minutes=30):
        return abs(ts - row.time.timestamp()) <= minutes * 60

    seen = {g for it in items for g in it.guids}  # already waiting from an earlier run
    for row in rows:
        key = f"bybit:{row.id}"
        if key in state["processed"] or key in seen or row.time < since:
            continue
        seen.add(key)
        if "Unified Trading Account" in row.desc or "UnifiedTrading" in row.code:
            # Funding <-> Unified: money stays on Bybit, the Bybit account in the app is the whole Bybit
            state["processed"][key] = {"uid": None, "note": "перемещение внутри Bybit"}
            continue
        if row.coin != "USDT":
            problems.append(f"Bybit: {row.busi} {row.amount} {row.coin} — счёт Bybit в приложении только в USDT, пропускаю")
            state["processed"][key] = {"uid": None, "note": f"не USDT: {row.coin}"}
            continue
        base = dict(guids=[key], texts=[f"Bybit {row.busi} / {row.desc} {row.direction} {row.amount} USDT, остаток {row.after}"],
                    bank_time=row.time.isoformat(), day=row.time.astimezone().date().isoformat(), crypto=True)
        is_pay = row.busi == "Bybit Pay" or "Bybitpay" in row.code

        if is_pay and row.direction == "OUT":
            minor = to_minor(row.amount, "ct:USDT")
            learned = db.category_for_amount(acc, "Expense", minor)
            vnd = vnd_estimate(db, row.amount)
            items.append(Item(kind="tx", tx_type="Expense", account=acc,
                              category=learned or bc["pay_category"], amount=str(row.amount),
                              comment="Bybit Pay" + (f" · {vnd}" if vnd else ""),
                              notes=["категория как в прошлый раз с этой суммой"] if learned else [],
                              wait_minutes=HOLD_FOR_CANCEL, **base))
            continue

        if is_pay and row.direction == "IN":  # cancelled purchase / refund
            match = next((it for it in reversed(items) if it.kind == "tx" and it.tx_type == "Expense"
                          and it.account == acc and it.guids[0].startswith("bybit:")
                          and Decimal(it.amount) == row.amount and timedelta(0) <= row.time - it.time <= timedelta(days=1)), None)
            if match:
                items.remove(match)
                for g in match.guids + [key]:
                    state["processed"][g] = {"uid": None, "note": "отменённая оплата Bybit Pay"}
                continue
            items.append(Item(kind="tx", tx_type="Income", account=acc, category=cfg["default_categories"]["Income"],
                              amount=str(row.amount), comment=f"❓ Возврат Bybit Pay ({row.desc})",
                              notes=["возврат по уже записанной оплате — можно удалить обе записи"], **base))
            continue

        if row.direction == "IN" and "InternalPlatformTopUp" in row.code:
            rec = next((r for r in records("internal deposits", internal_deposits)
                        if Decimal(r["amount"]) == row.amount and near(int(r["createdTime"]), row)), None)
            member = rec and str(rec.get("fromMemberId"))
            who = bc.get("member_labels", {}).get(member) or (f"UID {member}" if member else "другого пользователя Bybit")
            items.append(Item(kind="tx", tx_type="Income", account=acc, category=cfg["default_categories"]["Income"],
                              amount=str(row.amount), comment=f"❓ Перевод на Bybit от {who}",
                              notes=["зачисление без описания — проверь"], pairable=True,
                              hold_minutes=int(HOLD_FOR_CRYPTO.total_seconds() // 60), **base))
            continue

        if row.direction == "IN" and "Deposit" in row.code:
            rec = next((r for r in records("deposits", chain_deposits)
                        if Decimal(r["amount"]) == row.amount and near(int(r["successAt"]) / 1000, row, 120)), None)
            src = rec and rec.get("fromAddress")
            if src in wallets:  # from own Trust Wallet: the wallet side writes the transfer
                state["processed"][key] = {"uid": None, "note": f"пополнение с кошелька {wallets[src]['title']}"}
                continue
            who = labels.get(src, {}).get("title") or (f"{src[:6]}…{src[-4:]}" if src else "внешнего адреса")
            items.append(Item(kind="tx", tx_type="Income", account=acc, category=cfg["default_categories"]["Income"],
                              amount=str(row.amount), comment=f"❓ Пополнение Bybit от {who} · USDT",
                              notes=["криптоперевод без метки — проверь"], pairable=True,
                              hold_minutes=int(HOLD_FOR_CRYPTO.total_seconds() // 60), **base))
            continue

        if row.direction == "OUT":
            rec = next((r for r in records("withdrawals", withdrawals)
                        if row.amount in (Decimal(r["amount"]), Decimal(r["amount"]) + Decimal(r.get("withdrawFee") or 0))
                        and near(int(r["createTime"]) / 1000, row, 120)), None)
            if rec:
                to = rec.get("toAddress", "")
                if to in wallets:
                    if rec.get("txID"):  # the wallet sees this as incoming: don't count it twice
                        state["processed"][f"tron:{rec['txID']}:{to}"] = {"uid": None, "note": "вывод с Bybit"}
                    items.append(Item(kind="transfer", from_account=acc, to_account=wallets[to]["account"],
                                      amount=str(row.amount), to_amount=rec["amount"],
                                      comment=f"Вывод USDT с Bybit, комиссия {rec.get('withdrawFee') or 0}", **base))
                else:
                    label = labels.get(to, {})
                    cat = (label.get("category_expense") or label.get("category")
                           or cfg.get("crypto_default_categories", {}).get("Expense") or cfg["default_categories"]["Expense"])
                    items.append(Item(kind="tx", tx_type="Expense", account=acc, category=cat, amount=str(row.amount),
                                      comment=f"→ {label.get('title') or to[:6] + '…' + to[-4:]} · вывод с Bybit", **base))
                continue

        tx_type = "Income" if row.direction == "IN" else "Expense"
        items.append(Item(kind="tx", tx_type=tx_type, account=acc, category=cfg["default_categories"][tx_type],
                          amount=str(row.amount), comment=f"❓ Bybit: {row.busi} / {row.desc}",
                          notes=["незнакомый тип операции Bybit — проверь"], **base))
    return problems


def check_bybit_key(cfg: dict, state: dict) -> None:
    """Once a day: warn 10 days before the read-only key expires (keys without IP binding live 3 months)."""
    if not bybit_on(cfg) or state.get("bybit_key_checked") == date.today().isoformat():
        return
    try:
        exp = key_expiry(Bybit())
    except BybitError as e:
        notify(f"Bybit: {e}")
        return
    except Exception:
        return
    state["bybit_key_checked"] = date.today().isoformat()
    if exp and exp - datetime.now(timezone.utc) < timedelta(days=10):
        notify(f"Ключ Bybit API истекает {exp.astimezone():%d.%m}. Создай новый и сохрани в Связку ключей")


def pair_transit(items: list[Item]) -> list[Item]:
    """USDT comes in and almost the same amount leaves the same wallet within an hour:
    the money only passed through (the user's choice: don't record it). What stays
    behind becomes a small income so the balance still matches."""
    used: set[int] = set()
    out: list[Item] = []
    for o_i, o in enumerate(items):
        if not (o.kind == "tx" and o.crypto and o.tx_type == "Expense" and o.guids[0].startswith("tron:")):
            continue
        for i_i, i in enumerate(items):
            if i_i in used or o_i in used or not (i.kind == "tx" and i.crypto and i.tx_type == "Income"
                                                   and i.account == o.account and i.guids[0].startswith("tron:")):
                continue
            keep = Decimal(i.amount) - Decimal(o.amount)
            if timedelta(0) <= o.time - i.time <= TRANSIT_WINDOW and 0 <= keep <= Decimal(i.amount) * TRANSIT_MAX_KEEP:
                used.update({o_i, i_i})
                note = f"транзит {i.amount} → {o.amount} USDT"
                if keep > 0:
                    out.append(Item(kind="tx", tx_type="Income", account=i.account, category=i.category,
                                    amount=str(keep), comment=f"Остаток от транзита {i.amount} → {o.amount} USDT",
                                    guids=i.guids + o.guids, texts=i.texts + o.texts, bank_time=i.bank_time,
                                    day=i.day, crypto=True))
                else:
                    out.append(Item(kind="skip", guids=i.guids + o.guids, texts=i.texts + o.texts,
                                    bank_time=i.bank_time, day=i.day, comment=note))
                break
    out += [it for k, it in enumerate(items) if k not in used]
    return out


def pair_crypto(db: FinanceDB, items: list[Item]) -> list[Item]:
    """BYN card debit(s) + USDT arriving on an own wallet / Bybit soon after at a
    plausible rate -> transfer(s) (paid a friend in BYN, got USDT back).
    Several debits and several deposits can make one exchange (e.g. two cards ->
    two transfers from the friend); the USDT is then split in proportion."""
    used: set[int] = set()
    exps = [i for i, e in enumerate(items) if e.kind == "tx" and e.pairable and not e.crypto
            and e.tx_type == "Expense" and db.account(e.account)["currencyCode"] == "BYN"]
    incs = [i for i, c in enumerate(items) if c.kind == "tx" and c.crypto and c.pairable and c.tx_type == "Income"]

    usdt_per_byn = db.rate("BYN", "ct:USDT")
    ref = (1 / usdt_per_byn if usdt_per_byn else Decimal(3)) * Decimal("1.02")  # market + friend's margin

    def fits(es, cs, band):
        if len({items[c].account for c in cs}) != 1:
            return None
        lags = [items[c].time - items[e].time for e in es for c in cs]
        if not all(CRYPTO_WINDOW[0] <= lag <= CRYPTO_WINDOW[1] for lag in lags):
            return None
        rate = sum(Decimal(items[e].amount) for e in es) / sum(Decimal(items[c].amount) for c in cs)
        return rate if band[0] <= rate <= band[1] else None

    def best(sizes, band, score):
        cands = []
        for a, b in sizes:
            for es in combinations([i for i in exps if i not in used], a):
                for cs in combinations([i for i in incs if i not in used], b):
                    rate = fits(es, cs, band)
                    if rate is not None:
                        cands.append((score(es, cs, rate), es, cs, rate))
        return min(cands, key=lambda c: c[0]) if cands else None

    groups = []
    # 1) one debit <-> one deposit: closest in time, any plausible rate
    # 2) several debits / deposits (two cards, friend sends in parts): only near the market
    #    rate, since random sums match more easily
    passes = [([(1, 1)], BYN_PER_USDT,
               lambda es, cs, r: abs(items[cs[0]].time - items[es[0]].time)),
              ([(a, b) for a in range(1, 4) for b in range(1, 4) if (a, b) != (1, 1)],
               (ref * Decimal("0.95"), ref * Decimal("1.05")),
               lambda es, cs, r: (len(es) + len(cs), abs(r - ref)))]  # smallest group that fits
    for sizes, band, score in passes:
        while (found := best(sizes, band, score)):
            _, es, cs, rate = found
            used.update(es + cs)
            groups.append((es, cs, rate))

    out: list[Item] = []
    for es, cs, rate in groups:
        to_acc = items[cs[0]].account
        total_byn = sum(Decimal(items[e].amount) for e in es)
        total_usdt = sum(Decimal(items[c].amount) for c in cs)
        left = total_usdt
        c_guids = [g for c in cs for g in items[c].guids]
        c_texts = [t for c in cs for t in items[c].texts]
        for k, e in enumerate(es):
            it = items[e]
            share = left if k == len(es) - 1 else (Decimal(it.amount) * total_usdt / total_byn).quantize(Decimal("1e-8"))
            left -= share
            out.append(Item(kind="transfer", from_account=it.account, to_account=to_acc, amount=it.amount,
                            to_amount=str(share), guids=it.guids + (c_guids if k == 0 else []),
                            texts=it.texts + (c_texts if k == 0 else []),
                            bank_time=min([it.bank_time] + [items[c].bank_time for c in cs]), day=it.day,
                            comment=f"Обмен BYN → USDT, курс {rate:.3f}"))
    out += [it for k, it in enumerate(items) if k not in used]
    return out


def pair_transfers(db: FinanceDB, items: list[Item]) -> list[Item]:
    """Debit on one own card + credit of the same amount on another within
    30 min -> one transfer instead of an expense and an income."""
    used: set[int] = set()
    out: list[Item] = []
    for e_i, e in enumerate(items):
        if e_i in used or not (e.kind == "tx" and e.pairable and e.tx_type == "Expense"):
            continue
        e_ccy = db.account(e.account)["currencyCode"]
        for i_i, inc in enumerate(items):
            if i_i in used or not (inc.kind == "tx" and inc.pairable and inc.tx_type == "Income") or inc.crypto:
                continue
            if inc.account == e.account or db.account(inc.account)["currencyCode"] != e_ccy:
                continue
            if abs(Decimal(inc.amount) - Decimal(e.amount)) > Decimal("0.01"):
                continue
            if abs(inc.time - e.time) > PAIR_WINDOW:
                continue
            used.update({e_i, i_i})
            out.append(Item(kind="transfer", from_account=e.account, to_account=inc.account,
                            amount=e.amount, guids=e.guids + inc.guids, texts=e.texts + inc.texts,
                            bank_time=min(e.bank_time, inc.bank_time), day=e.day,
                            comment="Перевод между картами"))
            break
    out += [it for k, it in enumerate(items) if k not in used]
    return out


def describe(db: FinanceDB, it: Item) -> str:
    title = lambda uid: db.account(uid)["title"]  # noqa: E731
    if it.kind == "skip":
        return f"{it.day} не записываю: {it.comment}"
    if it.kind == "transfer":
        to = f" → {it.to_amount}" if it.to_amount else ""
        s = f"{it.day} ПЕРЕВОД {title(it.from_account)} → {title(it.to_account)}  {it.amount}{to} · {it.comment}"
    else:
        sign = "−" if it.tx_type == "Expense" else "+"
        real = f" ({it.real_amount} {it.real_currency})" if it.real_amount else ""
        s = (f"{it.day} {title(it.account):15} {sign}{it.amount}{real} · "
             f"{db.category(it.category)['title']} · {it.comment}")
    if it.notes:
        s += "  [" + "; ".join(it.notes) + "]"
    return s


# ---------------------------------------------------------------- app control

def app_running(name: str) -> bool:
    out = subprocess.run(["osascript", "-e", f'application "{name}" is running'],
                         capture_output=True, text=True)
    return out.stdout.strip() == "true"


def quit_app(name: str, timeout: float = 20) -> bool:
    if not app_running(name):
        return True
    subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'], capture_output=True)
    end = time.time() + timeout
    while time.time() < end:
        if not app_running(name):
            time.sleep(1)
            return True
        time.sleep(0.5)
    return False


def launch_app(name: str) -> None:
    subprocess.run(["open", "-g", "-a", name], check=False)


def wait_for_sync(db_path: str, target_usn: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(3)
        try:
            db = FinanceDB(db_path)
            st = db.sync_state()
            db.close()
            if int(st.get("lastSyncedUsn", 0)) >= target_usn:
                return True
        except Exception as e:  # db locked by the app
            log.debug("sync poll: %s", e)
    return False


def notify(text: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "Финансы"'],
                   capture_output=True)


def backup(db_path: str) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    dst = BACKUP_DIR / f"MyFinance-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(db_path, dst)
    for old in sorted(BACKUP_DIR.glob("MyFinance-*.db"))[:-30]:
        old.unlink()
    return dst


# ---------------------------------------------------------------- commands

def sms_from_file(path: str) -> list[Sms]:
    blocks = [b.strip() for b in Path(path).read_text().split("\n---\n") if b.strip()]
    now = datetime.now(timezone.utc)
    return [Sms(rowid=0, guid=f"file:{i}:{hash(b)}", sender="?", received=now, text=b, exact_time=False)
            for i, b in enumerate(blocks)]


def run_test_tx(cfg: dict, db_path: str) -> int:
    app = cfg["app_name"]
    if not quit_app(app):
        log.error("Не удалось закрыть «%s»", app)
        return 1
    log.info("Резервная копия: %s", backup(db_path))
    db = FinanceDB(db_path)
    with db.write():
        uid = db.add_transaction(
            account_uid="main", category_uid=cfg["default_categories"]["Expense"],
            tx_type="Expense", day=date.today(), amount_account=Decimal("0.01"),
            comment="🧪 Тест автодобавления — удали меня")
        target = db._usn
    db.close()
    log.info("Тестовая операция %s записана, запускаю приложение", uid)
    launch_app(app)
    ok = wait_for_sync(db_path, target, timeout=150)
    log.info("Синхронизация: %s", "отправлено на сервер" if ok else "НЕ подтвердилась за 150 c")
    return 0 if ok else 2


def real_balances(cfg: dict, state: dict) -> dict[str, tuple[Decimal, str]]:
    """account uid -> (balance from the source, where it comes from).
    Cards: balance printed in the latest bank SMS. TRON wallets: USDT on chain."""
    out = {}
    for acc, hist in state.get("balance_history", {}).items():
        if hist:
            when, bal, _ccy = max(hist, key=lambda h: h[0])
            t = datetime.fromisoformat(when).astimezone()
            out[acc] = (Decimal(bal), f"последняя SMS банка {t:%d.%m %H:%M}")
    for w in cfg.get("tron_wallets", []):
        out[w["account"]] = (tron_usdt_balance(w["address"]), "USDT в блокчейне TRON")
    if bybit_on(cfg):
        b = Bybit()
        unified = sum((Decimal(c["walletBalance"] or 0) for a in b.get("/v5/account/wallet-balance", accountType="UNIFIED")["list"]
                       for c in a["coin"] if c["coin"] == "USDT"), Decimal(0))
        out[cfg["bybit"]["account"]] = (funding_balance(b) + unified, "баланс Bybit (Funding + Unified)")
    return out


def run_fix_balances(cfg: dict, db_path: str, state: dict, dry_run: bool, use_app: bool) -> int:
    """Make app balances equal to the real ones with one correction per account."""
    if state.get("pending"):
        log.error("Есть операции в ожидании пары (%d), сверка сейчас даст двойной учёт. Попробуй позже.",
                  len(state["pending"]))
        return 1
    real = real_balances(cfg, state)
    db = FinanceDB(db_path)
    plan = []
    for acc, (bal, source) in real.items():
        a = db.account(acc)
        ccy = a["currencyCode"]
        have = db.balance(acc)
        diff = to_minor(bal, ccy) - to_minor(have, ccy)
        log.info("  %-20s в приложении %12s, реально %12s %s (%s)%s", a["title"], f"{have:.2f}",
                 f"{bal:.2f}", ccy, source, "" if diff == 0 else f" → корректировка {diff / 10 ** decimals(ccy):+.2f}")
        if diff:
            plan.append((acc, bal, source))
    db.close()
    if dry_run or not plan:
        return 0

    app = cfg["app_name"]
    was_running = use_app and app_running(app)
    if use_app and not quit_app(app):
        log.error("Не удалось закрыть «%s»", app)
        return 1
    log.info("Резервная копия: %s", backup(db_path))
    db = FinanceDB(db_path)
    with db.write():
        for acc, bal, source in plan:
            db.add_correction(account_uid=acc, day=date.today(), new_balance=bal,
                              comment=f"Сверка баланса: {source}")
        target = db._usn
    db.close()
    log.info("Корректировок записано: %d", len(plan))
    if use_app:
        launch_app(app)
        ok = wait_for_sync(db_path, target, timeout=150)
        log.info("Синхронизация: %s", "готово" if ok else "не дождался, приложение досинхронизирует позже")
        if ok and not was_running:
            quit_app(app)
    return 0


def write_items(db: FinanceDB, state: dict, ready: list[Item]) -> tuple[int, int]:
    """Returns (added, needs review)."""
    added = review = 0
    created = {v["uid"] for v in state["processed"].values() if v.get("uid")}
    for it in ready:
        if any(g in state["processed"] for g in it.guids):
            # already written (or handled from the other side) — never write twice
            log.info("  пропуск, уже обработано: %s", describe(db, it))
            for g in it.guids:
                state["processed"].setdefault(g, {"uid": None, "note": "повтор"})
            continue
        if it.kind == "skip":
            for g in it.guids:
                state["processed"][g] = {"uid": None, "note": it.comment}
            continue
        if it.kind == "transfer":
            acc, typ = it.from_account, "Expense"
        else:
            acc, typ = it.account, it.tx_type
        minor = to_minor(Decimal(it.amount), db.account(acc)["currencyCode"])
        dup = db.find_similar(acc, typ, date.fromisoformat(it.day), minor, created)
        if dup:
            log.info("  пропуск, уже есть вручную: %s", describe(db, it))
            created.add(dup)
            for g in it.guids:
                state["processed"][g] = {"uid": None, "duplicate_of": dup}
            continue
        if it.kind == "transfer":
            uid = db.add_transfer(from_account=it.from_account, to_account=it.to_account,
                                  day=date.fromisoformat(it.day), from_amount=Decimal(it.amount),
                                  to_amount=Decimal(it.to_amount) if it.to_amount else None,
                                  comment=it.comment)
        else:
            uid = db.add_transaction(
                account_uid=it.account, category_uid=it.category, tx_type=it.tx_type,
                day=date.fromisoformat(it.day), amount_account=Decimal(it.amount),
                real_amount=Decimal(it.real_amount) if it.real_amount else None,
                real_currency=it.real_currency, comment=it.comment)
            review += it.comment.startswith("❓")
        created.add(uid)
        for g in it.guids:
            state["processed"][g] = {"uid": uid, "kind": it.kind, "day": it.day}
        added += 1
    return added, review


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", help="other Finance DB file (for tests)")
    ap.add_argument("--sms-file")
    ap.add_argument("--since", help="ISO datetime, overrides start_from")
    ap.add_argument("--no-app", action="store_true", help="don't quit/launch the app")
    ap.add_argument("--list-senders", action="store_true")
    ap.add_argument("--state", help="other state file (for tests)")
    ap.add_argument("--test-tx", action="store_true")
    ap.add_argument("--fix-balances", action="store_true",
                    help="align card and wallet balances with the bank SMS / blockchain")
    ap.add_argument("--no-crypto", action="store_true", help="skip TRON wallets / exchanges")
    ap.add_argument("--check-config", action="store_true",
                    help="check that the accounts and categories in config.json exist in the app")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    global STATE_PATH
    if args.state:
        STATE_PATH = Path(args.state)

    handlers = [logging.FileHandler(HERE / "bridge.log")]
    if sys.stdout.isatty() or args.dry_run or args.sms_file:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO,
                        format="%(asctime)s %(message)s", handlers=handlers)

    if not (HERE / "config.json").exists():
        log.error("Нет config.json: скопируй config.example.json в config.json и заполни (см. CLAUDE.md)")
        return 1

    lock = open(HERE / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Уже запущен другой экземпляр, выхожу")
        return 0

    cfg = json.loads((HERE / "config.json").read_text())
    db_path = str(Path(args.db or cfg["finance_db"]).expanduser())
    msg_db = str(Path(cfg["messages_db"]).expanduser())

    if args.check_config:
        db = FinanceDB(db_path)
        bad = check_config(cfg, db)
        db.close()
        for p in bad:
            print("⚠️ ", p)
        print(f"Проблем в config.json: {len(bad)}" if bad else "config.json в порядке")
        return 1 if bad else 0
    if args.list_senders:
        for sender, n in list_senders(msg_db):
            print(f"{n:6}  {sender}")
        return 0
    if args.test_tx:
        return run_test_tx(cfg, db_path)

    state = load_state()
    if args.fix_balances:
        return run_fix_balances(cfg, db_path, state, args.dry_run, use_app=not args.no_app and not args.db)
    since = datetime.fromisoformat(args.since or cfg["start_from"])
    if args.sms_file:
        sms_list = sms_from_file(args.sms_file)
    else:
        if not app_running("Messages"):  # keeps iCloud syncing SMS to this Mac
            subprocess.run(["open", "-g", "-j", "-a", "Messages"], check=False)
        sms_list = read_sms(msg_db, cfg["senders"], after_rowid=state["last_rowid"])
        status = {"checked_at": datetime.now().astimezone().isoformat(), **sms_stats(msg_db, cfg["senders"])}
        (HERE / "status.json").write_text(json.dumps(status, indent=1))

    db = FinanceDB(db_path)
    bad = check_config(cfg, db)
    if bad:  # stop before reading anything: a wrong uid would break writing halfway
        for p in bad:
            log.error("config.json: %s", p)
        if not args.dry_run and state.get("config_warned") != date.today().isoformat():
            notify("Ошибка в config.json — запусти bridge.py --check-config")
            state["config_warned"] = date.today().isoformat()
            save_state(state)
        db.close()
        return 1
    items, keys = [], set()
    for d in state["pending"]:  # one copy per source operation
        k = tuple(d["guids"])
        if k not in keys:
            keys.add(k)
            items.append(Item(**d))
    problems = plan_sms(cfg, db, state, sms_list, since, items)
    refine_estimates(state, items)
    if not args.sms_file and not args.no_crypto:
        problems += plan_bybit(cfg, db, state, since, items)
        problems += plan_crypto(cfg, db, state, since, items)
        if not args.dry_run:
            check_bybit_key(cfg, state)
    for p in problems:
        log.warning("⚠️  %s", p)
    items = pair_transfers(db, items)
    items = pair_transit(items)
    items = pair_crypto(db, items)
    now = datetime.now(timezone.utc)
    ready = [it for it in items if now - it.time >= timedelta(minutes=it.wait_minutes)
             and (not it.pairable or now - it.time >= timedelta(minutes=it.hold_minutes))]
    waiting = [it for it in items if it not in ready]

    if not ready:
        log.info("Новых операций нет" + (f" (ждут пару: {len(waiting)})" if waiting else ""))
        db.close()
        if not args.dry_run:
            state["pending"] = [asdict(i) for i in waiting]
            save_state(state)
        return 0

    log.info("Найдено операций: %d%s", len(ready), f", ждут пару: {len(waiting)}" if waiting else "")
    for it in ready:
        log.info("  %s", describe(db, it))
    db.close()
    if args.dry_run:
        return 0

    app = cfg["app_name"]
    use_app = not args.no_app and not args.db
    was_running = use_app and app_running(app)
    if use_app and not quit_app(app):
        log.error("Не удалось закрыть «%s», попробую в следующий раз", app)
        return 1
    if not args.db:
        log.info("Резервная копия: %s", backup(db_path))

    db = FinanceDB(db_path)
    with db.write():
        added, review = write_items(db, state, ready)
        target_usn = db._usn
    db.close()
    state["pending"] = [asdict(i) for i in waiting]
    save_state(state)
    log.info("Добавлено: %d", added)

    if use_app and added:
        launch_app(app)
        ok = wait_for_sync(db_path, target_usn, timeout=150)
        log.info("Синхронизация: %s", "готово" if ok else "не дождался, приложение досинхронизирует позже")
        if ok and not was_running:
            quit_app(app)
        msg = f"Добавлено операций: {added}" + (f", проверь ❓: {review}" if review else "")
        notify(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
