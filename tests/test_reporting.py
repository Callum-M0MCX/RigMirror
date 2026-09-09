import json
from pathlib import Path
import tempfile
import unittest

from driver_runtime import load_driver
from reporting import write_test_report


class ReportingTests(unittest.TestCase):
    def test_failure_report_embeds_exact_driver_and_cat_traffic(self):
        root = Path(__file__).parents[1]
        driver_path = root / "drivers" / "Kenwood-TS-590SG.rmradio"
        driver = load_driver(driver_path)
        with tempfile.TemporaryDirectory() as folder:
            report_path = write_test_report(
                folder,
                category="driver_test",
                endpoint="SUB / SLAVE",
                port="COM8",
                baud=57600,
                outcome="FAILED",
                detail="Expected ID023; radio replied ID022;",
                traffic=["12:00:00.0  R2  TX  ID;", "12:00:00.1  R2  RX  ID022;"],
                driver=driver,
                driver_filename=driver_path.name,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report_path.suffix, ".rmreport")
        self.assertEqual(report["outcome"], "FAILED")
        self.assertEqual(report["serial"]["port"], "COM8")
        self.assertEqual(report["driver"], driver)
        self.assertIn("ID022;", report["cat_traffic"][-1])
