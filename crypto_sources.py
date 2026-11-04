"""USDT transfers of TRON wallets (Trust Wallet) from the public TronGrid API.

No key is needed: the blockchain is public. Only confirmed USDT `Transfer`
events are used; `Approval` events (permissions for swaps) are ignored.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
API = "https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20"


@dataclass
class ChainTx:
    txid: str
    time: datetime          # aware, UTC
    wallet: str             # our address
    direction: str          # "IN" | "OUT"
    amount: Decimal         # USDT
    counterparty: str


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "finbridge/1.0", "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def tron_usdt_transfers(address: str, since: datetime) -> list[ChainTx]:
    params = {"limit": 200, "only_confirmed": "true", "contract_address": USDT_TRC20,
              "min_timestamp": int(since.timestamp() * 1000)}
    out, fingerprint = [], None
    for _ in range(20):  # pages
        if fingerprint:
            params["fingerprint"] = fingerprint
        data = _get(API.format(addr=address) + "?" + urllib.parse.urlencode(params))
        for r in data.get("data", []):
            if r.get("type") != "Transfer" or r.get("token_info", {}).get("address") != USDT_TRC20:
                continue
            incoming = r["to"] == address
            out.append(ChainTx(
                txid=r["transaction_id"],
                time=datetime.fromtimestamp(r["block_timestamp"] / 1000, tz=timezone.utc),
                wallet=address,
                direction="IN" if incoming else "OUT",
                amount=Decimal(r["value"]) / Decimal(10) ** int(r["token_info"]["decimals"]),
                counterparty=r["from"] if incoming else r["to"],
            ))
        fingerprint = data.get("meta", {}).get("fingerprint")
        if not fingerprint:
            break
    # One transaction can pay several addresses at once (payment + service fees):
    # that is one operation for the user -> one entry, named after the main recipient.
    merged: dict[tuple, ChainTx] = {}
    biggest: dict[tuple, Decimal] = {}
    for t in out:
        k = (t.txid, t.direction)
        if k not in merged:
            merged[k], biggest[k] = t, t.amount
            continue
        m = merged[k]
        if t.amount > biggest[k]:
            biggest[k], m.counterparty = t.amount, t.counterparty
        m.amount += t.amount
    res = sorted(merged.values(), key=lambda t: t.time)
    return res


def tron_usdt_balance(address: str) -> Decimal:
    """Current USDT balance of a TRON address."""
    data = _get(f"https://api.trongrid.io/v1/accounts/{address}").get("data") or [{}]
    for token in data[0].get("trc20", []):
        if USDT_TRC20 in token:
            return Decimal(token[USDT_TRC20]) / Decimal(10) ** 6
    return Decimal(0)
