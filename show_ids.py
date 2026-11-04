#!/usr/bin/env python3
"""Accounts and categories of the «Финансы» app with their uid: these go into config.json.
Reads only, changes nothing.

  python3 show_ids.py
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
cfg_file = HERE / "config.json" if (HERE / "config.json").exists() else HERE / "config.example.json"
db_path = Path(json.loads(cfg_file.read_text())["finance_db"]).expanduser()
if not db_path.exists():
    sys.exit(f"Нет базы приложения: {db_path}\n"
             "Установи «Финансы» на этот Mac из App Store, войди в свой аккаунт и дождись синхронизации.")

con = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("СЧЕТА (uid → в cards / cash_accounts / tron_wallets / bybit.account)")
for a in con.execute("""select a.uid, a.title, a.currencyCode, a.isArchived, b.value from account a
                        left join account_balance b on b.uid = a.uid
                        where a.isRemoved = 0 order by a.isArchived, a.position, a.title"""):
    digits = 8 if (a["currencyCode"] or "").startswith("ct:") else 2
    bal = (a["value"] or 0) / 10 ** digits
    arch = "  (в архиве)" if a["isArchived"] else ""
    print(f"  {a['title'][:28]:28} {bal:>14,.2f} {a['currencyCode']:8} {a['uid']}{arch}")

print("\nКАТЕГОРИИ (uid → в category_rules / default_categories / pay_category)")
for kind, name in (("Expense", "Расходы"), ("Income", "Доходы")):
    print(f"  {name}:")
    for c in con.execute("select uid, title from category where isRemoved = 0 and type = ? "
                         "order by position, title", (kind,)):
        print(f"    {c['title'][:30]:30} {c['uid']}")
