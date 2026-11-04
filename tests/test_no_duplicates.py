import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402
from bridge import Item, pair_transit  # noqa: E402
from crypto_sources import ChainTx  # noqa: E402

WALLET = "TWa11etXXXXXXXXXXXXXXXXXXXXXXXXXXX"
EXCHANGE = "TExchangeXXXXXXXXXXXXXXXXXXXXXXXXX"
T = datetime(2026, 10, 29, 19, 37, tzinfo=timezone.utc)
CFG = {"tron_wallets": [{"address": WALLET, "account": "krypta", "title": "Крипта"}],
       "address_labels": {EXCHANGE: {"title": "биржа"}},
       "crypto_default_categories": {"Expense": "work"},
       "default_categories": {"Expense": "other_expense", "Income": "other_income"}}


class FakeDB:
    def category(self, uid):
        return {"type": "Income" if uid == "other_income" else "Expense"}


class NoDuplicates(unittest.TestCase):
    def test_waiting_transfer_is_not_planned_again(self):
        # a bug once planned the same waiting incoming transfer again on every run -> many copies
        tx = ChainTx("abc", T, WALLET, "IN", Decimal("500"), EXCHANGE)
        orig = bridge.tron_usdt_transfers
        bridge.tron_usdt_transfers = lambda addr, since: [tx]
        try:
            state, items = {"processed": {}}, []
            for _ in range(5):  # five background runs
                bridge.plan_crypto(CFG, FakeDB(), state, T - timedelta(days=1), items)
        finally:
            bridge.tron_usdt_transfers = orig
        self.assertEqual(len(items), 1)

    def test_transit_keeps_only_the_rest(self):
        i = Item(kind="tx", tx_type="Income", account="krypta", category="other_income", amount="500",
                 crypto=True, guids=["tron:in"], texts=[], bank_time=T.isoformat(), day="2026-10-30")
        o = Item(kind="tx", tx_type="Expense", account="krypta", category="work", amount="498.5", crypto=True,
                 guids=["tron:out"], texts=[], bank_time=(T + timedelta(minutes=13)).isoformat(), day="2026-10-30")
        out = pair_transit([i, o])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].tx_type, Decimal(out[0].amount)), ("Income", Decimal("1.5")))
        self.assertEqual(sorted(out[0].guids), ["tron:in", "tron:out"])

    def test_not_transit_when_out_is_bigger_or_late(self):
        i = Item(kind="tx", tx_type="Income", account="krypta", category="other_income", amount="100",
                 crypto=True, guids=["tron:in"], texts=[], bank_time=T.isoformat(), day="2026-10-30")
        big = Item(kind="tx", tx_type="Expense", account="krypta", category="work", amount="150", crypto=True,
                   guids=["tron:o1"], texts=[], bank_time=(T + timedelta(minutes=5)).isoformat(), day="2026-10-30")
        late = Item(kind="tx", tx_type="Expense", account="krypta", category="work", amount="99", crypto=True,
                    guids=["tron:o2"], texts=[], bank_time=(T + timedelta(hours=3)).isoformat(), day="2026-10-30")
        self.assertEqual(len(pair_transit([i, big])), 2)
        self.assertEqual(len(pair_transit([i, late])), 2)


if __name__ == "__main__":
    unittest.main()
