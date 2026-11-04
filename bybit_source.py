"""Read-only access to the user's Bybit account (API v5).

The key lives in the macOS Keychain (service `finbridge-bybit`, accounts
`key` and `secret`) and is never written to files or logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

BASE = "https://api.bybit.com"
RECV_WINDOW = "10000"


class BybitError(Exception):
    pass


def _keychain(account: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", "finbridge-bybit", "-a", account, "-w"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise BybitError(f"в Связке ключей нет finbridge-bybit/{account}")
    return out.stdout.strip()


class Bybit:
    def __init__(self):
        self._key = _keychain("key")
        self._secret = _keychain("secret")

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        ts = str(int(time.time() * 1000))
        sign = hmac.new(self._secret.encode(), (ts + self._key + RECV_WINDOW + query).encode(),
                        hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"{BASE}{path}?{query}", headers={
            "X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign, "User-Agent": "finbridge/1.0"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.load(r)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        if data.get("retCode") != 0:
            raise BybitError(f"{path}: {data.get('retCode')} {data.get('retMsg')}")
        return data.get("result") or {}

    def pages(self, path: str, list_key: str = "list", **params) -> list[dict]:
        """All pages of a cursor-paginated endpoint."""
        out, cursor = [], None
        for _ in range(50):
            res = self.get(path, cursor=cursor, **params)
            rows = res.get(list_key) or []
            out += rows
            nxt = res.get("nextPageCursor")
            if not rows or not nxt or nxt == cursor:
                break
            cursor = nxt
        return out

    def windows(self, path: str, since: datetime, days: int, list_key: str = "list",
                start_key: str = "startTime", end_key: str = "endTime", seconds: bool = False,
                **params) -> list[dict]:
        """Endpoints that only accept a limited time span: walk from `since` to now."""
        out, start, now = [], since, datetime.now(timezone.utc)
        while start < now:
            end = min(start + timedelta(days=days) - timedelta(seconds=1), now)
            conv = (lambda d: int(d.timestamp())) if seconds else (lambda d: int(d.timestamp() * 1000))
            out += self.pages(path, list_key=list_key, **{start_key: conv(start), end_key: conv(end)}, **params)
            start = end + timedelta(seconds=1)
        return out


# ---------------------------------------------------------------- typed helpers

@dataclass
class FundingRow:
    """One line of the Funding account history (what the Bybit app shows under Assets → History)."""
    id: str
    time: datetime              # aware, UTC
    coin: str
    direction: str              # "IN" | "OUT"
    amount: Decimal
    after: Decimal              # balance after
    busi: str                   # e.g. "Bybit Pay", "Deposit"
    desc: str                   # e.g. "Purchase", "Canceled Purchase"
    code: str                   # e.g. fundingAccountRecordBybitpayFiatFreeze


def funding_history(b: Bybit, since: datetime) -> list[FundingRow]:
    rows = b.windows("/v5/asset/fundinghistory", since, 7, start_key="createTimeFrom",
                     end_key="createTimeTo", seconds=True)
    out = {}
    for r in rows:
        rid = r.get("currcCursor") or f"{r['createTime']}:{r['txnAmt']}:{r['afterAmt']}"
        out[rid] = FundingRow(
            id=rid, time=datetime.fromtimestamp(int(r["createTime"]), tz=timezone.utc), coin=r["currency"],
            direction="IN" if r["ioDirection"] == "I" else "OUT", amount=Decimal(r["txnAmt"]),
            after=Decimal(r["afterAmt"]), busi=r.get("showBusiTypeEn", ""), desc=r.get("descriptionEn", ""),
            code=r.get("description", ""))
    def order(x: FundingRow):
        try:  # the cursor is the record number in base64 -> keeps same-second rows in order
            return (x.time, int(base64.b64decode(x.id)))
        except Exception:
            return (x.time, 0)
    return sorted(out.values(), key=order)


def internal_deposits(b: Bybit, since: datetime) -> list[dict]:
    return b.windows("/v5/asset/deposit/query-internal-record", since, 30, list_key="rows")


def chain_deposits(b: Bybit, since: datetime) -> list[dict]:
    return b.windows("/v5/asset/deposit/query-record", since, 30, list_key="rows")


def withdrawals(b: Bybit, since: datetime) -> list[dict]:
    return b.windows("/v5/asset/withdraw/query-record", since, 30, list_key="rows", withdrawType=2)


def funding_balance(b: Bybit, coin: str = "USDT") -> Decimal:
    res = b.get("/v5/asset/transfer/query-account-coins-balance", accountType="FUND", coin=coin)
    for c in res.get("balance", []):
        if c["coin"] == coin:
            return Decimal(c["walletBalance"] or 0)
    return Decimal(0)


def key_expiry(b: Bybit) -> Optional[datetime]:
    exp = b.get("/v5/user/query-api").get("expiredAt")
    return datetime.fromisoformat(exp.replace("Z", "+00:00")) if exp else None
