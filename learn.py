#!/usr/bin/env python3
"""Learns from history: matches old bank SMS with entries made by hand in the app and
shows which category each merchant usually got. Use it to write category_rules.
Reads only. Needs Full Disk Access for the terminal (Messages database) and
`cards` filled in config.json.

  python3 learn.py
"""
import collections
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from messages_source import read_sms  # noqa: E402
from sms_parsers import UnknownOperation, clean_merchant, parse_sms  # noqa: E402

cfg = json.loads((HERE / "config.json").read_text())
bank_tz = ZoneInfo(cfg["bank_timezone"])
fin = sqlite3.connect(Path(cfg["finance_db"]).expanduser().as_uri() + "?mode=ro", uri=True)

tx = fin.execute("""select t.uid, t.type, t.date, coalesce(t.amountInAccountCurrency, t.amountInRealCurrency),
                           l.otherUid, c.title
                    from `transaction` t
                    join sync_link l on l.entityUid = t.uid and l.otherType = 'Account' and l.isRemoved = 0
                    left join sync_link lc on lc.entityUid = t.uid and lc.otherType = 'Category' and lc.isRemoved = 0
                    left join category c on c.uid = lc.otherUid
                    where t.isRemoved = 0""").fetchall()
trf = fin.execute("""select tr.uid, tr.date, tr.fromAmount, coalesce(tr.toAmount, tr.fromAmount),
                            f.otherUid, t.otherUid, fa.title, ta.title
                     from transfer tr
                     join sync_link f on f.entityUid = tr.uid and f.otherType = 'FromAccount' and f.isRemoved = 0
                     join account fa on fa.uid = f.otherUid
                     join sync_link t on t.entityUid = tr.uid and t.otherType = 'ToAccount' and t.isRemoved = 0
                     join account ta on ta.uid = t.otherUid
                     where tr.isRemoved = 0""").fetchall()

used = set()
learned = collections.defaultdict(collections.Counter)
prev_bal = {}
matched = unmatched = 0
for s in read_sms(cfg["messages_db"], cfg["senders"]):
    try:
        op = parse_sms(s.sender, s.text, s.received.astimezone(bank_tz).replace(tzinfo=None))
    except UnknownOperation:
        continue
    card = op and cfg["cards"].get(op.card)
    if not card:
        continue
    acc = card["account"]
    if op.currency == op.balance_currency:
        amount = op.amount
    elif acc in prev_bal and op.balance is not None:
        amount = abs(prev_bal[acc] - op.balance)  # foreign currency: what the bank charged
    else:
        amount = None
    if op.balance is not None:
        prev_bal[acc] = op.balance
    if amount is None:
        continue
    day = s.received.astimezone().date()
    minor = int(amount * 100)
    merchant = clean_merchant(op.merchant)
    near = lambda d: abs((date.fromisoformat(d[:10]) - day).days) <= 1  # noqa: E731
    fits = lambda a: a is not None and abs(a - minor) <= max(1, minor * 0.01)  # noqa: E731
    hit = next((r for r in tx if r[0] not in used and r[4] == acc and r[1] == op.kind
                and near(r[2]) and fits(r[3])), None)
    if hit:
        label = hit[5] or "(без категории)"
    else:
        hit = next((r for r in trf if r[0] not in used and near(r[1])
                    and ((r[4] == acc and op.kind == "Expense" and fits(r[2]))
                         or (r[5] == acc and op.kind == "Income" and fits(r[3])))), None)
        label = hit and f"ПЕРЕВОД {hit[6]} → {hit[7]}"
    if hit:
        used.add(hit[0])
        matched += 1
    else:
        unmatched += 1
    learned[merchant][label or "(нет в приложении)"] += 1

print(f"Нашлось в приложении: {matched}, не нашлось: {unmatched}\n")
for m, c in sorted(learned.items(), key=lambda kv: -sum(kv[1].values())):
    print(f"{sum(c.values()):4}  {m[:40]:40} {dict(c.most_common(4))}")
