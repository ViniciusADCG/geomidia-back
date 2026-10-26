import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.api.routes.process_tracking import analyst_identity, status_change, tracked_process
from app.core.security import get_current_user
from app.db.session import get_session
from app.main import app


class ProcessTrackingTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    async def test_only_admin_can_open_tracking_routes(self):
        def user_for(role):
            async def user():
                return SimpleNamespace(role=role)
            return user

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for role in ("analyst", "viewer"):
                app.dependency_overrides[get_current_user] = user_for(role)
                for path in ("/api/process-tracking", "/api/process-tracking/deleted",
                             "/api/process-tracking/analysts", "/api/process-tracking/analysts/summary"):
                    with self.subTest(role=role, path=path):
                        self.assertEqual((await client.get(path)).status_code, 403)

    async def test_admin_receives_process_timeline_and_analyst_options(self):
        created = datetime(2026, 10, 20, 12, tzinfo=UTC)
        asset_id, analyst_id = uuid4(), uuid4()
        asset = SimpleNamespace(id=asset_id, process_code="VEI-03-2026", official_process_code=None,
                                status="análise", created_at=created)
        analyst = SimpleNamespace(id=analyst_id, full_name="Ana", role="analyst")
        log = SimpleNamespace(id=uuid4(), asset_id=asset_id, created_at=created + timedelta(days=2),
                              actor_role="analyst", actor_user_id=analyst_id, actor_name="Ana",
                              activity_type="edicao", changes={"status": {"before": "novos processos", "after": "análise"}})

        class Session:
            async def scalar(self, statement):
                return 1

            async def scalars(self, statement):
                if "users.role" in str(statement):
                    return [analyst]
                return [asset]

            async def execute(self, statement):
                if "GROUP BY" in str(statement):
                    return [(analyst_id, "Ana")]
                return [(log, analyst)]

        async def user():
            return SimpleNamespace(role="admin")

        async def session():
            yield Session()

        app.dependency_overrides[get_current_user] = user
        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/process-tracking", params={"analyst_id": str(analyst_id)})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["seconds_to_analysis"], 2 * 86400)
            self.assertEqual(data["items"][0]["timeline"][1]["analyst_name"], "Ana")
            options = await client.get("/api/process-tracking/analysts")
            self.assertEqual(options.json(), [{"id": str(analyst_id), "name": "Ana"}])

    async def test_deleted_process_remains_visible_with_reason_and_history(self):
        created = datetime(2026, 10, 20, 12, tzinfo=UTC)
        analyst_id = uuid4()
        registration = SimpleNamespace(
            id=uuid4(), asset_id=None, process_code="VEI-04-2026", activity_type="cadastro",
            created_at=created, actor_role=None, actor_user_id=None, actor_name=None,
            changes={"after": {"status": "novos processos"}},
        )
        start = SimpleNamespace(
            id=uuid4(), asset_id=None, process_code="VEI-04-2026", activity_type="edicao",
            created_at=created + timedelta(days=2), actor_role="analyst", actor_user_id=analyst_id,
            actor_name="Ana", changes={"status": {"before": "novos processos", "after": "análise"}},
        )
        removal = SimpleNamespace(
            id=uuid4(), asset_id=None, process_code="VEI-04-2026", activity_type="remocao",
            created_at=created + timedelta(days=4), actor_role="admin", actor_user_id=uuid4(),
            actor_name="Gestor", changes={"before": {"status": "análise", "official_process_code": "OF-04"},
                                           "created_at": created.isoformat(), "reason": "Registro duplicado confirmado."},
        )

        class Session:
            async def scalar(self, statement):
                return 1

            async def scalars(self, statement):
                return [removal]

            async def execute(self, statement):
                return [(registration, None), (start, SimpleNamespace(role="analyst", full_name="Ana")),
                        (removal, SimpleNamespace(role="admin", full_name="Gestor"))]

        async def admin():
            return SimpleNamespace(role="admin")

        async def session():
            yield Session()

        app.dependency_overrides[get_current_user] = admin
        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/process-tracking/deleted")
        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["process_code"], "VEI-04-2026")
        self.assertEqual(item["deletion_reason"], "Registro duplicado confirmado.")
        self.assertEqual(item["seconds_to_analysis"], 2 * 86400)
        self.assertEqual(item["timeline"][-1]["is_deletion"], True)
        self.assertIsNone(item["timeline"][-1]["analyst_name"])

    async def test_analyst_summary_counts_actions_without_assigning_process_duration(self):
        analyst_id = uuid4()
        analyst = SimpleNamespace(id=analyst_id, full_name="Ana", role="analyst")

        class Session:
            async def execute(self, statement):
                return [(analyst_id, "Ana", 3, 7, 2, 1)]

            async def scalars(self, statement):
                return [analyst]

        async def admin():
            return SimpleNamespace(role="admin")

        async def session():
            yield Session()

        app.dependency_overrides[get_current_user] = admin
        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/process-tracking/analysts/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": str(analyst_id), "name": "Ana", "processes_touched": 3,
                                            "actions": 7, "analyses_started": 2, "approvals": 1}])

    def test_process_uses_first_analysis_and_approval_and_tracks_reopen(self):
        created = datetime(2026, 10, 20, 12, tzinfo=UTC)
        asset = SimpleNamespace(id=uuid4(), process_code="VEI-01-2026", official_process_code=None,
                                status="cartografia", created_at=created)
        analyst_id = uuid4()
        analyst = SimpleNamespace(full_name="Ana", role="analyst")

        def log(at, before, after, role="analyst"):
            return SimpleNamespace(
                created_at=at, actor_role=role, actor_user_id=analyst_id,
                actor_name="Ana" if role == "analyst" else "Gestor",
                activity_type="edicao",
                changes={"status": {"before": before, "after": after}},
            )

        logs = [
            (log(created + timedelta(days=1), "novos processos", "análise"), analyst),
            (log(created + timedelta(days=5), "análise", "cartografia"), analyst),
            (log(created + timedelta(days=8), "cartografia", "aprovado"), analyst),
            (log(created + timedelta(days=9), "aprovado", "cartografia", "admin"), analyst),
        ]
        result = tracked_process(asset, logs, created + timedelta(days=10))
        self.assertEqual(result.seconds_to_analysis, 86400)
        self.assertEqual(result.seconds_analysis_to_approval, 7 * 86400)
        self.assertEqual(result.seconds_to_approval, 8 * 86400)
        self.assertEqual(result.seconds_in_current_status, 86400)
        self.assertEqual(result.timeline[2].analyst_name, "Ana")
        self.assertIsNone(result.timeline[-1].analyst_name)
        self.assertEqual(result.timeline[-1].after, "cartografia")

    def test_nested_change_and_legacy_role_are_recognized_without_guessing_missing_dates(self):
        created = datetime(2026, 10, 20, tzinfo=UTC)
        asset = SimpleNamespace(id=uuid4(), process_code="VEI-02-2026", official_process_code=None,
                                status="novos processos", created_at=created)
        empty = tracked_process(asset, [], created + timedelta(days=2))
        self.assertIsNone(empty.analysis_started_at)
        self.assertIsNone(empty.first_approved_at)
        self.assertEqual(empty.seconds_in_current_status, 2 * 86400)

        legacy = SimpleNamespace(actor_role=None, actor_user_id=uuid4(), actor_name=None,
                                 changes={"asset": {"status": {"before": "análise", "after": "jurídico"}}})
        self.assertEqual(status_change(legacy), ("análise", "jurídico"))
        self.assertEqual(analyst_identity(legacy, SimpleNamespace(role="analyst", full_name="Bruna"))[1:],
                         ("Bruna", True))
        self.assertEqual(analyst_identity(legacy, SimpleNamespace(role="viewer", full_name="Bruna")),
                         (None, None, False))
