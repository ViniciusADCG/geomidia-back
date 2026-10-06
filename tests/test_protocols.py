import unittest
from datetime import UTC, datetime, timedelta

from app.services.protocols import CAMPO_GRANDE_TIMEZONE, format_protocol, protocol_year


class ProtocolTests(unittest.TestCase):
    def test_public_protocols_start_at_01_and_grow_after_99(self):
        self.assertEqual(format_protocol("VEI", 1, 2026), "VEI-01-2026")
        self.assertEqual(format_protocol("HESP", 1, 2026), "HESP-01-2026")
        self.assertEqual(format_protocol("HESP", 100, 2026), "HESP-100-2026")

    def test_new_year_follows_campo_grande_midnight(self):
        midnight = datetime(2027, 1, 1, tzinfo=CAMPO_GRANDE_TIMEZONE).astimezone(UTC)
        self.assertEqual(protocol_year(midnight - timedelta(minutes=1)), 2026)
        self.assertEqual(protocol_year(midnight), 2027)


if __name__ == "__main__":
    unittest.main()
