import sys
import unittest
from pathlib import Path


sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.domain.rules import AssetForAnalysis, calculate_distance_meters, evaluate_conflicts, get_required_radius


class RuleTests(unittest.TestCase):
    def test_led_radius_depends_on_area(self):
        self.assertEqual(get_required_radius("painel de led", 4), 250)
        self.assertEqual(get_required_radius("painel de led", 6), 1000)

    def test_distance_between_same_point_is_zero(self):
        self.assertLess(calculate_distance_meters(-20.464, -54.612, -20.464, -54.612), 0.01)

    def test_same_type_inside_radius_conflicts(self):
        candidate = AssetForAnalysis(
            id="new",
            process_code="PROC-TEST",
            media_type="outdoor",
            status="Pendente",
            latitude=-20.464,
            longitude=-54.612,
            area_m2=27,
            radius_meters=80,
        )
        existing = AssetForAnalysis(
            id="old",
            process_code="PROC-OLD",
            media_type="outdoor",
            status="Aprovado",
            latitude=-20.4642,
            longitude=-54.6122,
            area_m2=27,
            radius_meters=80,
        )

        analysis = evaluate_conflicts(candidate, [existing])

        self.assertTrue(analysis["has_conflict"])
        self.assertEqual(analysis["conflicting_asset_id"], "old")


if __name__ == "__main__":
    unittest.main()
