import asyncio
import sys
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.api.routes.media_assets import (
    analyze_media_asset,
    ensure_direct_status_change_allowed,
    start_media_asset_analysis,
)
from app.db.models import MediaAsset, User


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
