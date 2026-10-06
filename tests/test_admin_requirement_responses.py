import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.core.security import get_current_user
from app.db.session import get_session
from app.main import app


def saved_response():
    return SimpleNamespace(
        id=uuid4(),
        process_code="HESP-0001-2026",
        finalized_at=datetime(2026, 9, 28, 15, 0, tzinfo=UTC),
        receipt_sent_at=None,
        payload={
            "process_type": "RESPOSTA_COMUNICADO_EXIGENCIA",
            "process_number": "141115/2026-09",
            "notice_number": "2143391",
            "email": "requerente@example.com",
            "acknowledgement": True,
            "started_at": "2026-09-28T14:58:00Z",
            "website": "",
        },
        attachments=[{
            "filename": "resposta.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
            "object_path": "public-submissions/example/resposta.pdf",
        }],
    )


class AdminRequirementResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.clear()

    async def test_viewer_cannot_list_responses_even_by_direct_url(self):
        async def viewer():
            return SimpleNamespace(role="viewer")

        app.dependency_overrides[get_current_user] = viewer
        response_id = uuid4()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for path in (
                "/api/requirement-responses",
                f"/api/requirement-responses/{response_id}",
                f"/api/requirement-responses/{response_id}/comprovante",
                f"/api/requirement-responses/{response_id}/attachments/0/download",
            ):
                response = await client.get(path)
                self.assertEqual(response.status_code, 403, path)

    async def test_analyst_can_list_saved_response_without_storage_paths(self):
        draft = saved_response()
        session = SimpleNamespace(scalar=AsyncMock(return_value=1), scalars=AsyncMock(return_value=[draft]))

        async def analyst():
            return SimpleNamespace(role="analyst")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = analyst
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/requirement-responses")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["protocol"], "HESP-0001-2026")
        self.assertEqual(item["attachments"][0]["filename"], "resposta.pdf")
        self.assertNotIn("object_path", item["attachments"][0])

    async def test_admin_can_download_the_saved_receipt(self):
        draft = saved_response()
        session = SimpleNamespace(scalar=AsyncMock(return_value=draft))

        async def admin():
            return SimpleNamespace(role="admin")

        async def fake_session():
            yield session

        app.dependency_overrides[get_current_user] = admin
        app.dependency_overrides[get_session] = fake_session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/requirement-responses/{draft.id}/comprovante")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertIn("HESP-0001-2026.pdf", response.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
