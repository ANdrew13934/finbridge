import sys, unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sms_parsers import parse_sms, clean_merchant, PRIOR_INCOMING_CARD

B = [b.strip() for b in (Path(__file__).parent / "sample_sms.txt").read_text().split("\n---\n")]

class T(unittest.TestCase):
    def test_prior_foreign(self):
        op = parse_sms("Priorbank", B[1])
        self.assertEqual((op.card, op.kind, op.amount, op.currency, op.balance), ("4***1234", "Expense", Decimal("52000.00"), "VND", Decimal("806.15")))
        self.assertEqual(clean_merchant(op.merchant), "Grab")
    def test_prior_confirmation_ignored(self):
        self.assertIsNone(parse_sms("Priorbank", B[2]))
    def test_prior_byn(self):
        op = parse_sms("Priorbank", B[3])
        self.assertEqual((op.amount, op.currency, clean_merchant(op.merchant)), (Decimal("50.00"), "BYN", "MOBILE BANK"))
    def test_otp_ignored(self):
        self.assertIsNone(parse_sms("BelVEB24.BY", B[5]))
    def test_belveb(self):
        exp = [("Expense", "120.00", "480.00"), ("Expense", "300.00", "180.00"), ("Income", "250.00", "430.00"), ("Expense", "25.00", "405.00")]
        for blk, (k, a, b) in zip(B[6:10], exp):
            op = parse_sms("BelVEB24.BY", blk)
            self.assertEqual((op.kind, op.amount, op.balance, op.card), (k, Decimal(a), Decimal(b), "9.1234"))
    def test_prior_perevod(self):
        op = parse_sms("Priorbank", B[10])
        self.assertEqual((op.kind, op.op_word, op.amount), ("Expense", "Perevod", Decimal("10.00")))
    def test_prior_incoming(self):
        op = parse_sms("Priorbank", B[11], received=datetime(2026, 6, 28, 13, 31))
        self.assertEqual((op.card, op.kind, op.amount, op.balance, op.bank_time), (PRIOR_INCOMING_CARD, "Income", Decimal("20.00"), Decimal("140.50"), datetime(2026, 6, 28, 13, 30)))
    def test_prior_incoming_year_rollover(self):
        txt = "28/12 13:30. Na vashu kartu zachisleno 15.00 BYN. Dostupnaja summa: 31.66 BYN. Tel. 7299090"
        self.assertEqual(parse_sms("Priorbank", txt, received=datetime(2027, 1, 2)).bank_time.year, 2026)
    def test_belveb_rub(self):
        op = parse_sms("BelVEB24.BY", B[12])
        self.assertEqual((op.amount, op.currency, op.balance_currency, clean_merchant(op.merchant)), (Decimal("900.00"), "RUB", "BYN", "AEROEXPRESS"))
    def test_merchant_cleanup(self):
        self.assertEqual(clean_merchant(parse_sms("BelVEB24.BY", B[14]).merchant), "YANDEX TAXI")
        self.assertEqual(clean_merchant('SHOP "SOSEDI" MINSK BLR'), 'SHOP "SOSEDI"')
        self.assertEqual(clean_merchant('CP* WHOOSH.BIKE MOSKVA RUS'), 'CP WHOOSH.BIKE')


class AtmWithdrawal(unittest.TestCase):
    def test_nalichnye_v_bankomate(self):
        op = parse_sms("Priorbank", "Karta 4***1234 01-11-26 20:00:00. Nalichnye v bankomate 1000000.00 VND. "
                                    "VNM ATM CENTER. Balance: 600.00 BYN Tel. 7299090")
        self.assertEqual((op.kind, op.amount, op.currency, op.balance), ("Expense", Decimal("1000000.00"), "VND", Decimal("600.00")))

    def test_unknown_word_keeps_amount_and_balance(self):
        from sms_parsers import UnknownOperation
        with self.assertRaises(UnknownOperation) as cm:
            parse_sms("Priorbank", "Karta 4***1234 01-11-26 10:00:00. Strannaya operaciya 10.00 BYN. X. Balance: 90.00 BYN")
        self.assertEqual(cm.exception.op.balance, Decimal("90.00"))

if __name__ == "__main__":
    unittest.main()
