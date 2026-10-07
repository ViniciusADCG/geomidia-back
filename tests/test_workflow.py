import asyncio
import sys
import unittest
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_assets import (
    ANALYSIS_STATUS_VALUES,
    analyze_media_asset,
    ensure_direct_status_change_allowed,
    expiration_window,
    start_media_asset_analysis,
    update_media_asset,
)
from app.db.models import MediaAsset, User
from app.schemas import MediaAssetUpdate


def new_asset() -> MediaAsset:
    now = datetime.now(UTC)
    return MediaAsset(
        id=uuid.uuid4(),
        process_code="PROC-2026-999",
        media_type="outdoor",
        address="Av. Teste, 100",
        district="Centro",
        latitude=-20.46,
        longitude=-54.61,
        area_m2=27,
        bottom_height_m=5,
        radius_meters=80,
        status="novos processos",
        created_at=now,
        updated_at=now,
    )


class MediaAssetWorkflowTests(unittest.TestCase):
    def test_official_protocol_preserves_origin_and_is_logged(self):
        asset = new_asset()
        asset.process_code = "VEI-0110-2026"
        user = User(id=uuid.uuid4(), username="admin", full_name="Admin", password_hash="unused", role="admin")
        session = MagicMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=asset)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))

        with (
            patch("app.api.routes.media_assets.active_rule_for_type", new=AsyncMock(return_value=object())),
            patch("app.api.routes.media_assets.calculate_rule_radius", return_value=80),
        ):
            result = asyncio.run(update_media_asset(
                asset.id, MediaAssetUpdate(official_process_code=" 12345/2026 "), request, session, user,
            ))

        self.assertEqual(asset.process_code, "VEI-0110-2026")
        self.assertEqual(result.official_process_code, "12345/2026")
        self.assertEqual((asset.area_m2, asset.bottom_height_m), (27, 5))
        self.assertEqual(session.add.call_args.args[0].changes["official_process_code"], {
            "before": None, "after": "12345/2026",
        })
        session.commit.assert_awaited_once()

    def test_expiration_window_includes_today_and_the_ninetieth_day(self):
        reference_date, window_end_date = expiration_window(date(2026, 8, 20))

        self.assertEqual(reference_date, date(2026, 8, 20))
        self.assertEqual(window_end_date, date(2026, 11, 18))

    def test_dashboard_separates_new_processes_from_analysis(self):
        self.assertNotIn("novos processos", ANALYSIS_STATUS_VALUES)
        self.assertIn("análise", ANALYSIS_STATUS_VALUES)

    def test_new_process_status_cannot_change_through_regular_update(self):
        with self.assertRaises(HTTPException) as context:
            ensure_direct_status_change_allowed("novos processos", "aprovado")

        self.assertEqual(context.exception.status_code, 409)

    def test_new_process_status_cannot_be_reapplied_later(self):
        with self.assertRaises(HTTPException) as context:
            ensure_direct_status_change_allowed("análise", "novos processos")

        self.assertEqual(context.exception.status_code, 409)

    def test_started_process_can_follow_regular_workflow(self):
        ensure_direct_status_change_allowed("análise", "vistoria")

    def test_start_analysis_moves_new_process_and_registers_activity(self):
        asset = new_asset()
        user = User(id=uuid.uuid4(), username="analista", full_name="Analista", password_hash="unused", role="analyst")
        session = MagicMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=asset)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        request = SimpleNamespace(state=SimpleNamespace(request_id="request-123"))

        result = asyncio.run(start_media_asset_analysis(asset.id, request, session, user))

        self.assertEqual(result.status.value, "análise")
        self.assertEqual(asset.status, "análise")
        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    def test_new_process_cannot_run_conflict_analysis_before_start(self):
        asset = new_asset()
        user = User(id=uuid.uuid4(), username="viewer", full_name="Viewer", password_hash="unused", role="viewer")
        session = MagicMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=asset)

        with self.assertRaises(HTTPException) as context:
            asyncio.run(analyze_media_asset(asset.id, session, user))

        self.assertEqual(context.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
