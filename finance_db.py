"""Writes transactions and transfers into the local database of the «Финансы»
(Money manager) app.

The app syncs through a change log (table `sync_action`). Every new or changed
entity gets rows there; on its next sync the app sends rows with a usn above
`lastSyncedUsn` to the server. We create rows exactly like the app does, so the
app itself uploads them and they reach the iPhone.

The app must NOT be running while we write (it keeps the usn counter in memory).
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def _iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def decimals(currency: str) -> int:
    """Crypto ('ct:USDT') amounts are stored with 8 decimals, fiat with 2."""
    return 8 if currency.startswith("ct:") else 2


def to_minor(amount: Decimal, currency: str) -> int:
    q = Decimal(10) ** decimals(currency)
    return int((amount * q).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _val(v):
    """Value + value type the way the app stores them in sync_action."""
    return (None, "none") if v is None else (str(v), "string")


class FinanceDB:
    def __init__(self, path: str):
        self.path = path
        self.con = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.con.row_factory = sqlite3.Row

    def close(self):
        self.con.close()

    # ---------- reading ----------

    def default_currency(self) -> str:
        row = self.con.execute(
            "select value from syncable_settings where uid='defaultCurrencyCode'"
        ).fetchone()
        return row["value"] if row else "BYN"

    def account(self, uid: str) -> sqlite3.Row:
        row = self.con.execute("select * from account where uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"account {uid} not found")
        return row

    def category(self, uid: str) -> sqlite3.Row:
        row = self.con.execute("select * from category where uid=?", (uid,)).fetchone()
        if row is None:
            raise KeyError(f"category {uid} not found")
        return row

    def rate(self, from_ccy: str, to_ccy: str) -> Optional[Decimal]:
        """How many `to_ccy` for 1 `from_ccy`, from the app's own rate table."""
        if from_ccy == to_ccy:
            return Decimal(1)
        row = self.con.execute(
            "select value from rates where fromCurrencyCode=? and toCurrencyCode=?",
            (from_ccy, to_ccy)).fetchone()
        if row and row["value"]:
            return Decimal(str(row["value"]))
        row = self.con.execute(
            "select value from rates where fromCurrencyCode=? and toCurrencyCode=?",
            (to_ccy, from_ccy)).fetchone()
        if row and row["value"]:
            return Decimal(1) / Decimal(str(row["value"]))
        return None

    def find_similar(self, account_uid: str, tx_type: str, day: date,
                     amount_acc_minor: int, exclude: set[str]) -> Optional[str]:
        """An entry (transaction or transfer) that looks like the same operation:
        same account and direction, date ±1 day, amount within 1%."""
        lo = int(amount_acc_minor * 0.99)
        hi = int(amount_acc_minor * 1.01) + 1
        rows = self.con.execute(
            """select t.uid from `transaction` t
               join sync_link l on l.entityType='Transaction' and l.entityUid=t.uid
                    and l.otherType='Account' and l.isRemoved=0
               where l.otherUid=? and t.isRemoved=0 and t.type=?
                 and abs(julianday(t.date) - julianday(?)) <= 1
                 and coalesce(t.amountInAccountCurrency, t.amountInRealCurrency) between ? and ?""",
            (account_uid, tx_type, day.isoformat(), lo, hi)).fetchall()
        side, amount_col = (("FromAccount", "tr.fromAmount") if tx_type == "Expense"
                            else ("ToAccount", "coalesce(tr.toAmount, tr.fromAmount)"))
        rows += self.con.execute(
            f"""select tr.uid from transfer tr
                join sync_link l on l.entityType='Transfer' and l.entityUid=tr.uid
                     and l.otherType=? and l.isRemoved=0
                where l.otherUid=? and tr.isRemoved=0
                  and abs(julianday(tr.date) - julianday(?)) <= 1
                  and {amount_col} between ? and ?""",
            (side, account_uid, day.isoformat(), lo, hi)).fetchall()
        for r in rows:
            if r["uid"] not in exclude:
                return r["uid"]
        return None

    def recent_income(self, account_uid: str, category_uid: str, day: date, days: int = 3) -> list[int]:
        """Amounts (minor units) of incomes in this account+category in the last `days` days."""
        rows = self.con.execute(
            """select coalesce(t.amountInAccountCurrency, t.amountInRealCurrency) a from `transaction` t
               join sync_link l on l.entityUid=t.uid and l.otherType='Account' and l.isRemoved=0
               join sync_link c on c.entityUid=t.uid and c.otherType='Category' and c.isRemoved=0
               where l.otherUid=? and c.otherUid=? and t.isRemoved=0 and t.type='Income'
                 and julianday(?) - julianday(t.date) between 0 and ?""",
            (account_uid, category_uid, day.isoformat(), days)).fetchall()
        return [r["a"] for r in rows if r["a"] is not None]

    def category_for_amount(self, account_uid: str, tx_type: str, amount_minor: int) -> Optional[str]:
        """Category the user gave last time to an entry with exactly this amount
        (same price at the same place). «Другое» doesn't count."""
        row = self.con.execute(
            """select c.otherUid from `transaction` t
               join sync_link a on a.entityType='Transaction' and a.entityUid=t.uid
                    and a.otherType='Account' and a.isRemoved=0
               join sync_link c on c.entityType='Transaction' and c.entityUid=t.uid
                    and c.otherType='Category' and c.isRemoved=0
               join category k on k.uid=c.otherUid and k.isRemoved=0 and k.type=t.type
               where a.otherUid=? and t.isRemoved=0 and t.type=?
                 and coalesce(t.amountInAccountCurrency, t.amountInRealCurrency)=?
                 and c.otherUid not in ('other_expense', 'other_income')
               order by c.modified desc limit 1""",
            (account_uid, tx_type, amount_minor)).fetchone()
        return row["otherUid"] if row else None

    def sync_state(self) -> dict:
        return {r["key"]: r["value"] for r in self.con.execute("select key, value from sync_data")}

    # ---------- writing (inside `with db.write():`) ----------

    def _now(self) -> int:
        # strictly increasing so entries created in one run keep their order
        ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._last_ms = max(ms, getattr(self, "_last_ms", 0) + 100)
        return self._last_ms

    def _log(self, entity: str, uid: str, links: list[tuple[str, str]],
             fields: list[tuple[str, object, str]], t: int) -> None:
        """Write sync_link + sync_action rows the way the app does:
        links first, then 'add', then one 'update' per field."""
        rows = []
        for i, (other_type, other_uid) in enumerate(links):
            ts = t - 63 + i * 57
            self.con.execute("insert into sync_link values (?,?,?,?,?,0)",
                             (entity, uid, other_type, other_uid, _iso(ts)))
            rows.append((f"link{entity}{uid}{other_type}{other_uid}", ts, "link", other_type, other_uid, "string"))
        rows.append((f"add{entity}{uid}null", t + 50, "add", None, None, None))
        for name, value, vtype in fields:
            rows.append((f"update{entity}{uid}{name}", t, "update", name, value, vtype))
        for key, ts, typ, name, value, vtype in rows:
            self._usn += 1
            self.con.execute("insert into sync_action values (?,?,?,?,?,?,?,?,?)",
                             (_sha1(key), self._usn, uid, entity, ts, typ, name, value, vtype))
        self._last_modified = _iso(t)

    def _bump_balance(self, account_uid: str, delta_minor: int) -> None:
        self.con.execute("update account_balance set value = value + ? where uid=?",
                         (delta_minor, account_uid))

    def balance(self, account_uid: str) -> Decimal:
        """Current balance in account currency (the app's own cache)."""
        acc = self.account(account_uid)
        row = self.con.execute("select value from account_balance where uid=?", (account_uid,)).fetchone()
        minor = row["value"] if row and row["value"] is not None else 0
        return Decimal(minor) / Decimal(10) ** decimals(acc["currencyCode"])

    def add_correction(self, *, account_uid: str, day: date, new_balance: Decimal,
                       comment: str = "") -> Optional[str]:
        """Balance correction the way the app makes it: a transaction with an
        account but no category. Returns None if the balance is already right."""
        ccy = self.account(account_uid)["currencyCode"]
        delta = to_minor(new_balance, ccy) - to_minor(self.balance(account_uid), ccy)
        if delta == 0:
            return None
        q = Decimal(10) ** decimals(ccy)
        return self.add_transaction(account_uid=account_uid, category_uid=None,
                                    tx_type="Income" if delta > 0 else "Expense", day=day,
                                    amount_account=Decimal(abs(delta)) / q, comment=comment)

    def add_transaction(self, *, account_uid: str, category_uid: Optional[str], tx_type: str, day: date,
                        amount_account: Decimal, real_amount: Optional[Decimal] = None,
                        real_currency: Optional[str] = None, comment: str = "") -> str:
        """category_uid=None makes a balance correction (no category link)."""
        acc = self.account(account_uid)
        if category_uid is not None:
            cat = self.category(category_uid)
            if cat["type"] != tx_type:
                raise ValueError(f"category '{cat['title']}' is {cat['type']}, not {tx_type}")
        acc_ccy, def_ccy = acc["currencyCode"], self.default_currency()

        amount_acc = to_minor(amount_account, acc_ccy)
        if acc_ccy == def_ccy:
            amount_def = amount_acc
        else:
            r = self.rate(def_ccy, acc_ccy)
            amount_def = to_minor(amount_account / r, def_ccy) if r else amount_acc
        real_minor = None
        if real_amount is not None and real_currency and real_currency != acc_ccy:
            real_minor = to_minor(real_amount, real_currency)
        else:
            real_currency = None

        t = self._now()
        uid = str(uuid.uuid4())
        self.con.execute(
            """insert into `transaction` (uid, created, modified, type,
                 amountInDefaultCurrency, amountInRealCurrency, realCurrencyCode,
                 amountInAccountCurrency, accountCurrencyCode, date, comment, v, isRemoved)
               values (?,?,?,?,?,?,?,?,?,?,?,2,0)""",
            (uid, _iso(t), _iso(t), tx_type, amount_def, real_minor, real_currency,
             amount_acc, acc_ccy, day.isoformat(), comment))
        links = [("Account", account_uid)] if category_uid is None else [("Category", category_uid), ("Account", account_uid)]
        self._log("Transaction", uid, links, [
            ("type", tx_type, "string"),
            ("amountInDefaultCurrency", *_val(amount_def)),
            ("realCurrencyCode", *_val(real_currency)),
            ("amountInRealCurrency", *_val(real_minor)),
            ("date", day.isoformat(), "string"),
            ("comment", comment, "string"),
            ("accountCurrencyCode", acc_ccy, "string"),
            ("amountInAccountCurrency", str(amount_acc), "string"),
            ("v", "2", "int"),
            ("isRemoved", "0", "bool"),
        ], t)
        self._bump_balance(account_uid, amount_acc if tx_type == "Income" else -amount_acc)
        return uid

    def add_transfer(self, *, from_account: str, to_account: str, day: date,
                     from_amount: Decimal, to_amount: Optional[Decimal] = None,
                     comment: str = "") -> str:
        """Same-currency transfers store only fromAmount (toAmount stays NULL)."""
        f_ccy = self.account(from_account)["currencyCode"]
        t_ccy = self.account(to_account)["currencyCode"]
        f_minor = to_minor(from_amount, f_ccy)
        if f_ccy == t_ccy:
            t_minor, t_code = None, None
        else:
            if to_amount is None:
                raise ValueError("to_amount is required for a transfer between currencies")
            t_minor, t_code = to_minor(to_amount, t_ccy), t_ccy

        t = self._now()
        uid = str(uuid.uuid4())
        self.con.execute(
            """insert into transfer (uid, created, modified, fromAmount, fromCurrencyCode,
                 toAmount, toCurrencyCode, date, comment, isRemoved) values (?,?,?,?,?,?,?,?,?,0)""",
            (uid, _iso(t), _iso(t), f_minor, f_ccy, t_minor, t_code, day.isoformat(), comment))
        self._log("Transfer", uid, [("FromAccount", from_account), ("ToAccount", to_account)], [
            ("fromAmount", str(f_minor), "string"),
            ("fromCurrencyCode", f_ccy, "string"),
            ("toAmount", *_val(t_minor)),
            ("toCurrencyCode", *_val(t_code)),
            ("date", day.isoformat(), "string"),
            ("comment", comment, "string"),
            ("isRemoved", "0", "bool"),
        ], t)
        self._bump_balance(from_account, -f_minor)
        self._bump_balance(to_account, t_minor if t_minor is not None else f_minor)
        return uid

    def remove_transaction(self, uid: str) -> None:
        """Delete the way the app does: isRemoved=1 plus one 'update isRemoved' action."""
        t = self.con.execute("select * from `transaction` where uid=?", (uid,)).fetchone()
        if t is None or t["isRemoved"]:
            return
        acc = self.con.execute("select otherUid from sync_link where entityType='Transaction' and entityUid=? "
                               "and otherType='Account' and isRemoved=0", (uid,)).fetchone()
        ms = self._now()
        self.con.execute("update `transaction` set isRemoved=1, modified=? where uid=?", (_iso(ms), uid))
        self._usn += 1
        self.con.execute("insert or replace into sync_action values (?,?,?,?,?,?,?,?,?)",
                         (_sha1(f"updateTransaction{uid}isRemoved"), self._usn, uid, "Transaction", ms,
                          "update", "isRemoved", "1", "bool"))
        if acc:
            amount = t["amountInAccountCurrency"] if t["amountInAccountCurrency"] is not None else t["amountInRealCurrency"]
            self._bump_balance(acc["otherUid"], -amount if t["type"] == "Income" else amount)
        self._last_modified = _iso(ms)

    def update_transaction(self, uid: str, *, amount_account: Optional[Decimal] = None,
                           comment: Optional[str] = None, real_amount: Optional[Decimal] = None) -> None:
        """Edit amount and/or comment the way the app does: new value in the row and
        the field's 'update' action replaced with a fresh usn."""
        t = self.con.execute("select * from `transaction` where uid=?", (uid,)).fetchone()
        if t is None or t["isRemoved"] or not t["accountCurrencyCode"]:
            raise ValueError(f"transaction {uid}: can't edit")
        acc = self.con.execute("select otherUid from sync_link where entityType='Transaction' and entityUid=? "
                               "and otherType='Account' and isRemoved=0", (uid,)).fetchone()["otherUid"]
        ccy, ms, fields = t["accountCurrencyCode"], self._now(), []
        if amount_account is not None:
            new = to_minor(amount_account, ccy)
            def_ccy = self.default_currency()
            if ccy == def_ccy:
                new_def = new
            else:
                r = self.rate(def_ccy, ccy)
                new_def = to_minor(amount_account / r, def_ccy) if r else new
            diff = new - t["amountInAccountCurrency"]
            self.con.execute("update `transaction` set amountInAccountCurrency=?, amountInDefaultCurrency=? where uid=?",
                             (new, new_def, uid))
            fields += [("amountInDefaultCurrency", str(new_def)), ("amountInAccountCurrency", str(new))]
            self._bump_balance(acc, diff if t["type"] == "Income" else -diff)
        if real_amount is not None:
            if not t["realCurrencyCode"]:
                raise ValueError(f"transaction {uid} has no payment currency")
            real_minor = to_minor(real_amount, t["realCurrencyCode"])
            self.con.execute("update `transaction` set amountInRealCurrency=? where uid=?", (real_minor, uid))
            fields.append(("amountInRealCurrency", str(real_minor)))
        if comment is not None:
            self.con.execute("update `transaction` set comment=? where uid=?", (comment, uid))
            fields.append(("comment", comment))
        self.con.execute("update `transaction` set modified=? where uid=?", (_iso(ms), uid))
        for name, value in fields:
            self._usn += 1
            self.con.execute("insert or replace into sync_action values (?,?,?,?,?,?,?,?,?)",
                             (_sha1(f"updateTransaction{uid}{name}"), self._usn, uid, "Transaction", ms,
                              "update", name, value, "string"))
        self._last_modified = _iso(ms)

    def set_category(self, uid: str, category_uid: str) -> None:
        """Change a transaction's category the way the app does: 'unlink' the old
        category, 'link' the new one (sync_link row of the old one gets isRemoved=1)."""
        t = self.con.execute("select type from `transaction` where uid=? and isRemoved=0", (uid,)).fetchone()
        if t is None:
            raise ValueError(f"transaction {uid} not found")
        if self.category(category_uid)["type"] != t["type"]:
            raise ValueError(f"category {category_uid} is not {t['type']}")
        old = self.con.execute("select otherUid from sync_link where entityType='Transaction' and entityUid=? "
                               "and otherType='Category' and isRemoved=0", (uid,)).fetchone()
        if old and old["otherUid"] == category_uid:
            return
        ms = self._now()
        if old:
            self.con.execute("update sync_link set isRemoved=1, modified=? where entityType='Transaction' and "
                             "entityUid=? and otherType='Category' and otherUid=?", (_iso(ms), uid, old["otherUid"]))
            self._usn += 1
            self.con.execute("insert or replace into sync_action values (?,?,?,?,?,?,?,?,?)",
                             (_sha1(f"unlinkTransaction{uid}Category{old['otherUid']}"), self._usn, uid,
                              "Transaction", ms, "unlink", "Category", old["otherUid"], "string"))
        self.con.execute("insert or replace into sync_link values ('Transaction',?, 'Category', ?, ?, 0)",
                         (uid, category_uid, _iso(ms + 17)))
        self._usn += 1
        self.con.execute("insert or replace into sync_action values (?,?,?,?,?,?,?,?,?)",
                         (_sha1(f"linkTransaction{uid}Category{category_uid}"), self._usn, uid,
                          "Transaction", ms + 17, "link", "Category", category_uid, "string"))
        self.con.execute("update `transaction` set modified=? where uid=?", (_iso(ms + 17), uid))
        self._last_modified = _iso(ms + 17)

    def write(self):
        return _WriteTx(self)


class _WriteTx:
    def __init__(self, db: FinanceDB):
        self.db = db

    def __enter__(self):
        c = self.db.con
        c.execute("begin immediate")
        self.db._usn = int(c.execute("select value from sync_data where key='usn'").fetchone()[0])
        self.db._last_modified = None
        return self.db

    def __exit__(self, exc_type, exc, tb):
        c = self.db.con
        if exc_type:
            c.execute("rollback")
            return False
        c.execute("update sync_data set value=? where key='usn'", (str(self.db._usn),))
        if self.db._last_modified:
            for k in ("lastModifiedDate", "lastEntitiesModified"):
                c.execute("update sync_data set value=? where key=?", (self.db._last_modified, k))
        c.execute("commit")
        return False
