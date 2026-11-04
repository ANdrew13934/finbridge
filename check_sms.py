#!/usr/bin/env python3
"""Runs every SMS from the banks through the parsers: shows which cards were seen
(for `cards` in config.json), the most frequent merchants and the SMS that were not
understood. Reads only. Needs Full Disk Access for the terminal (Messages database).

  python3 check_sms.py                 senders from config.json
  python3 check_sms.py MTBANK Alfa     other senders (see: python3 bridge.py --list-senders)
  python3 check_sms.py --all           print every SMS with the result (to write a parser)
"""
import argparse
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from messages_source import read_sms  # noqa: E402
from sms_parsers import UnknownOperation, clean_merchant, parse_sms  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("senders", nargs="*")
ap.add_argument("--all", action="store_true")
args = ap.parse_args()
cfg_file = HERE / "config.json" if (HERE / "config.json").exists() else HERE / "config.example.json"
cfg = json.loads(cfg_file.read_text())
senders = args.senders or cfg["senders"]

try:
    sms = read_sms(cfg["messages_db"], senders)
except Exception as e:
    sys.exit(f"Не читается база Сообщений ({e}).\n"
             "Дай Полный доступ к диску приложению, в котором запущен терминал: "
             "Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску.")

stats = collections.Counter()
cards = collections.Counter()
merchants = collections.Counter()
unparsed = collections.defaultdict(list)
for s in sms:
    try:
        op = parse_sms(s.sender, s.text, s.received.replace(tzinfo=None))
        result = "не операция (коды, реклама) или не понято"
    except UnknownOperation as e:
        op, result = None, f"незнакомая операция ({e})"
    if op:
        stats[s.sender, "операций"] += 1
        cards[op.bank, op.card] += 1
        merchants[clean_merchant(op.merchant)] += 1
        result = f"{op.kind} {op.amount} {op.currency} · {clean_merchant(op.merchant)} · баланс {op.balance}"
    else:
        stats[s.sender, result.split(" (")[0] if result.startswith("незнакомая") else result] += 1
        unparsed[s.sender].append(s.text)
    if args.all:
        print(f"{s.received:%Y-%m-%d %H:%M} {s.sender}: {s.text!r}\n    → {result}")

print(f"Всего SMS от {', '.join(senders)}: {len(sms)}")
for (sender, what), n in sorted(stats.items()):
    print(f"  {sender:15} {what}: {n}")
print("\nКарты (ключи для cards в config.json):")
for (bank, card), n in cards.most_common():
    print(f"  {bank:12} {card!r:16} {n} SMS")
print("\nЧастые магазины:")
for m, n in merchants.most_common(40):
    print(f"  {n:4}  {m}")
for sender, texts in unparsed.items():
    kinds = {}
    for t in texts:
        kinds.setdefault(t[:22], t)
    print(f"\nНе распознано у {sender}: {len(texts)} SMS, {len(kinds)} разных видов, примеры:")
    for t in list(kinds.values())[:20]:
        print("   ", t.replace("\n", " | ")[:170])
