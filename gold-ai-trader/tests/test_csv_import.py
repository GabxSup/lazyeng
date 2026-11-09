import unittest
from datetime import datetime, timedelta

from app.importer import CSVParseError, parse_mt5_csv


def rows(count=300):
    start = datetime(2026, 10, 1, 0, 0)
    price = 4000.0
    result = []
    for i in range(count):
        stamp = start + timedelta(minutes=5 * i)
        open_price = price
        close_price = open_price + 0.4
        high = max(open_price, close_price) + 0.2
        low = min(open_price, close_price) - 0.2
        result.append((stamp, open_price, high, low, close_price))
        price = close_price
    return result


class CSVImportTests(unittest.TestCase):
    def test_mt5_tab_export_with_header_and_offset(self):
        data = rows()
        text = "MetaTrader export\n<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
        text += "\n".join(
            f"{t:%Y.%m.%d}\t{t:%H:%M:%S}\t{o:.2f}\t{h:.2f}\t{l:.2f}\t{c:.2f}\t100"
            for t, o, h, l, c in data
        )
        candles, meta = parse_mt5_csv(text, timeframe="M5", utc_offset_minutes=180)
        self.assertEqual(len(candles), 300)
        self.assertEqual(meta["timeframe"], "M5")
        self.assertEqual(meta["bars"], 300)
        self.assertEqual(candles[0].time.hour, 21)  # 00:00 server GMT+3 -> 21:00 UTC prior day
        self.assertAlmostEqual(candles[0].open, 4000.0)

    def test_comma_export_single_timestamp(self):
        data = rows()
        text = "Date,Time,Open,High,Low,Close,Volume\n"
        text += "\n".join(
            f"{t:%Y-%m-%d},{t:%H:%M:%S},{o},{h},{l},{c},100"
            for t, o, h, l, c in data
        )
        candles, meta = parse_mt5_csv(text, utc_offset_minutes=0)
        self.assertEqual(len(candles), 300)
        self.assertEqual(meta["delimiter"], ",")
        self.assertEqual(candles[0].time.hour, 0)

    def test_invalid_or_too_short_file(self):
        with self.assertRaises(CSVParseError):
            parse_mt5_csv("not,a,candle,file\n1,2,3,4,5")
        text = "Date,Time,Open,High,Low,Close\n" + "\n".join(
            f"{t:%Y-%m-%d},{t:%H:%M:%S},{o},{h},{l},{c}"
            for t, o, h, l, c in rows(30)
        )
        with self.assertRaises(CSVParseError):
            parse_mt5_csv(text)


if __name__ == "__main__":
    unittest.main()
