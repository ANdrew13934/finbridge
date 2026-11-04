import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import Item, pair_crypto  # noqa: E402


class FakeDB:
    CCY = {"lich": "BYN", "prior": "BYN", "bybit": "ct:USDT"}

    def account(self, uid):
        return {"currencyCode": self.CCY[uid]}

    def rate(self, a, b):
        return Decimal("0.3397")  # BYN -> USDT, the app's rate


T0 = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)


def debit(acc, amount, minutes):
    return Item(kind="tx", tx_type="Expense", account=acc, amount=amount, pairable=True, guids=[f"{acc}{amount}"],
                texts=[], bank_time=(T0 + timedelta(minutes=minutes)).isoformat(), day="2026-10-23")


def deposit(amount, minutes):
    return Item(kind="tx", tx_type="Income", account="bybit", amount=amount, pairable=True, crypto=True,
                guids=[f"in{amount}"], texts=[], bank_time=(T0 + timedelta(minutes=minutes)).isoformat(),
                day="2026-10-23")


class PairCrypto(unittest.TestCase):
    def test_one_to_one(self):
        out = pair_crypto(FakeDB(), [debit("lich", "300", 0), deposit("100", 30)])
        self.assertEqual([(o.kind, o.to_amount) for o in out], [("transfer", "100")])

    def test_two_cards_two_deposits(self):
        # 900 BYN + 1200 BYN from two cards, the friend sends the USDT in two parts: 600 + 100
        out = pair_crypto(FakeDB(), [debit("lich", "900", 0), debit("prior", "1200", 5),
                                     deposit("600", 50), deposit("100", 55)])
        self.assertTrue(all(o.kind == "transfer" for o in out))
        self.assertEqual(len(out), 2)
        self.assertEqual(sum(Decimal(o.to_amount) for o in out), Decimal("700"))
        self.assertEqual(sorted(g for o in out for g in o.guids), sorted(["lich900", "prior1200", "in600", "in100"]))

    def test_extra_small_debit_is_left_out(self):
        # a 3 BYN fee next to the exchange must stay a separate expense
        out = pair_crypto(FakeDB(), [debit("lich", "3", -10), debit("lich", "900", 0), debit("prior", "1200", 5),
                                     deposit("600", 50), deposit("100", 55)])
        self.assertEqual(sorted((o.kind, o.amount) for o in out),
                         [("transfer", "1200"), ("transfer", "900"), ("tx", "3")])

    def test_rate_out_of_range_stays_separate(self):
        out = pair_crypto(FakeDB(), [debit("lich", "100", 0), deposit("100", 30)])
        self.assertEqual([o.kind for o in out], ["tx", "tx"])


if __name__ == "__main__":
    unittest.main()
