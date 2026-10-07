import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_rules import calculate_rule_radius, refresh_asset_radii
from app.db.models import MediaRule
from app.domain.rules import AssetForAnalysis, calculate_distance_meters, evaluate_conflicts, get_required_radius


class RuleTests(unittest.TestCase):
    def test_classification_and_legacy_area_use_configured_radii(self):
        rule = MediaRule(base_radius_meters=250, area_threshold_m2=5, radius_above_threshold_meters=1000)
        self.assertEqual(calculate_rule_radius(rule, 5), 250)
        self.assertEqual(calculate_rule_radius(rule, 6), 1000)
        self.assertEqual(calculate_rule_radius(rule, None, "within_limit"), 250)
        self.assertEqual(calculate_rule_radius(rule, None, "above_limit"), 1000)
        with self.assertRaises(HTTPException):
            calculate_rule_radius(rule, None)

    def test_recalculation_preserves_historical_band_after_threshold_change(self):
        rule = MediaRule(media_type="painel de led", base_radius_meters=300,
                         area_threshold_m2=8, radius_above_threshold_meters=900)
        old = SimpleNamespace(area_m2=6, area_rule_classification=None, radius_meters=0)
        within = SimpleNamespace(area_m2=None, area_rule_classification="within_limit", radius_meters=0)
        above = SimpleNamespace(area_m2=None, area_rule_classification="above_limit", radius_meters=0)
        session = SimpleNamespace(scalars=AsyncMock(return_value=[old, within, above]))
        asyncio.run(refresh_asset_radii(rule, session))
        self.assertEqual((old.radius_meters, within.radius_meters, above.radius_meters), (300, 300, 900))

    def test_new_threshold_does_not_guess_band_for_unclassified_asset(self):
        rule = MediaRule(media_type="outdoor", base_radius_meters=80,
                         area_threshold_m2=5, radius_above_threshold_meters=250)
        asset = SimpleNamespace(area_m2=None, area_rule_classification=None, radius_meters=80)
        session = SimpleNamespace(scalars=AsyncMock(return_value=[asset]))
        asyncio.run(refresh_asset_radii(rule, session))
        self.assertEqual(asset.radius_meters, 80)

    def test_led_radius_depends_on_area(self):
        self.assertEqual(get_required_radius("painel de led", 4), 250)
        self.assertEqual(get_required_radius("painel de led", 6), 1000)

    def test_modular_electronic_panel_has_fixed_radius(self):
        self.assertEqual(get_required_radius("painel eletronico modular", 1), 1000)
        self.assertEqual(get_required_radius("painel eletronico modular", 100), 1000)

    def test_distance_between_same_point_is_zero(self):
        self.assertLess(calculate_distance_meters(-20.464, -54.612, -20.464, -54.612), 0.01)

    def test_same_type_inside_radius_conflicts(self):
        candidate = AssetForAnalysis(
            id="new",
            process_code="PROC-TEST",
            media_type="outdoor",
            status="análise",
            latitude=-20.464,
            longitude=-54.612,
            area_m2=27,
            radius_meters=80,
        )
        existing = AssetForAnalysis(
            id="old",
            process_code="PROC-OLD",
            media_type="outdoor",
            status="aprovado",
            latitude=-20.4642,
            longitude=-54.6122,
            area_m2=27,
            radius_meters=80,
        )

        analysis = evaluate_conflicts(candidate, [existing])

        self.assertTrue(analysis["has_conflict"])
        self.assertEqual(analysis["conflicting_asset_id"], "old")
        self.assertEqual(len(analysis["conflicts"]), 1)

    def test_exact_minimum_distance_is_allowed(self):
        candidate = AssetForAnalysis("new", "PROC-NEW", "outdoor", "análise", 0, 0, 27, 80)
        existing = AssetForAnalysis("old", "PROC-OLD", "outdoor", "aprovado", 0.00071946, 0, 27, 80)
        analysis = evaluate_conflicts(candidate, [existing])
        self.assertFalse(analysis["has_conflict"])

    def test_rejected_assets_are_ignored(self):
        candidate = AssetForAnalysis("new", "PROC-NEW", "outdoor", "análise", -20.464, -54.612, 27, 80)
        rejected = AssetForAnalysis("old", "PROC-OLD", "outdoor", "irregular", -20.464, -54.612, 27, 80)
        self.assertFalse(evaluate_conflicts(candidate, [rejected])["has_conflict"])


if __name__ == "__main__":
    unittest.main()
