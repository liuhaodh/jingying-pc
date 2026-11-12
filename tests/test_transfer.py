# -*- coding: utf-8 -*-
"""传输速度统计与格式化。"""
import time
import unittest

from tests.support import ROOT  # noqa: F401

import transfer


class TransferMeterTests(unittest.TestCase):
    def test_total_and_reset(self):
        meter = transfer.TransferMeter()
        meter.add(1024)
        meter.add(2048)
        self.assertEqual(meter.total(), 3072)
        meter.reset()
        self.assertEqual(meter.total(), 0)
        self.assertEqual(meter.rate(), 0.0)

    def test_rate_positive_while_transferring(self):
        meter = transfer.TransferMeter()
        meter.add(1024 * 1024)
        time.sleep(0.2)
        meter.add(1024 * 1024)
        self.assertGreater(meter.rate(), 0)
        # 停止传输后一段时间应回到 0
        time.sleep(2.1)
        self.assertEqual(meter.rate(), 0.0)

    def test_invalid_counts_ignored(self):
        meter = transfer.TransferMeter()
        meter.add(None)
        meter.add("bad")
        meter.add(-5)
        self.assertEqual(meter.total(), 0)

    def test_format(self):
        self.assertEqual(transfer.format_rate(0), "0.00 MB/s")
        self.assertEqual(transfer.format_rate(512 * 1024), "0.50 MB/s")
        self.assertEqual(transfer.format_rate(2 * 1024 * 1024), "2.00 MB/s")
        self.assertEqual(transfer.format_bytes(0), "0 B")
        self.assertEqual(transfer.format_bytes(5 * 1024 * 1024), "5.0 MB")


if __name__ == "__main__":
    unittest.main()
