"""Reads bank SMS from the macOS Messages database (needs Full Disk Access).

SMS reach the Mac through «Сообщения в iCloud» / SMS forwarding from the iPhone.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass
class Sms:
    rowid: int
    guid: str
    sender: str
    received: datetime   # aware, UTC
    text: str
    exact_time: bool = True   # False: `received` is unknown, use the time printed in the SMS


def _decode_attributed_body(blob: bytes) -> str | None:
    """Newer macOS often leaves `text` empty and keeps it in a typedstream blob."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    i += len(b"NSString") + 5
    n = blob[i]
    i += 1
    if n == 0x81:
        n = int.from_bytes(blob[i:i + 2], "little")
        i += 2
    elif n == 0x82:
        n = int.from_bytes(blob[i:i + 3], "little")
        i += 3
    return blob[i:i + n].decode("utf-8", "replace")


def _apple_time(value: int) -> datetime:
    # Since High Sierra the value is in nanoseconds.
    secs = value / 1e9 if value > 1e11 else value
    return APPLE_EPOCH + timedelta(seconds=secs)


def read_sms(db_path: str, senders: list[str], after_rowid: int = 0) -> list[Sms]:
    uri = Path(db_path).expanduser().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        q = f"""
            select m.ROWID, m.guid, h.id, m.date, m.text, m.attributedBody
            from message m join handle h on h.ROWID = m.handle_id
            where m.is_from_me = 0 and m.ROWID > ?
              and h.id in ({",".join("?" * len(senders))})
            order by m.date, m.ROWID"""
        out = []
        for rowid, guid, sender, d, text, body in con.execute(q, (after_rowid, *senders)):
            text = text or _decode_attributed_body(body) or ""
            out.append(Sms(rowid, guid, sender, _apple_time(d or 0), text))
        return out
    finally:
        con.close()


def stats(db_path: str, senders: list[str]) -> dict:
    """What the Mac currently sees: used to check that SMS still reach it."""
    uri = Path(db_path).expanduser().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        total, last = con.execute("select count(*), max(date) from message").fetchone()
        bank_last = con.execute(
            f"""select max(m.date) from message m join handle h on h.ROWID=m.handle_id
                where h.id in ({",".join("?" * len(senders))})""", senders).fetchone()[0]
        return {"messages_total": total,
                "last_message": _apple_time(last or 0).isoformat(),
                "last_bank_sms": _apple_time(bank_last or 0).isoformat()}
    finally:
        con.close()


def list_senders(db_path: str, like: str = "%") -> list[tuple[str, int]]:
    """Helper to find the exact sender names banks use."""
    uri = Path(db_path).expanduser().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return con.execute(
            """select h.id, count(*) from message m join handle h on h.ROWID=m.handle_id
               where h.id like ? group by h.id order by 2 desc""", (like,)).fetchall()
    finally:
        con.close()
