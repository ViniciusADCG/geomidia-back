import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from app.core.security import get_current_user
from app.db.models import MediaAsset
from app.db.session import get_session
from app.main import app


class ListingSession:
    def __init__(self, connection, assets):
        self.connection = connection
        self.assets = assets
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.connection.execute(statement).scalar_one()

    async def scalars(self, statement):
        self.statements.append(statement)
        page = select(MediaAsset.id)
        if statement.whereclause is not None:
            page = page.where(statement.whereclause)
        page = (page.order_by(*statement._order_by_clauses)
                .limit(statement._limit_clause.value)
                .offset(statement._offset_clause.value))
        ids = self.connection.execute(page).scalars().all()
        return [self.assets[str(asset_id)] for asset_id in ids]


class MediaAssetListingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        self.connection = self.engine.connect()
        self.connection.execute(text("ATTACH DATABASE ':memory:' AS public"))
        self.connection.execute(text("""
            CREATE TABLE public.media_assets (
                id TEXT PRIMARY KEY, process_code TEXT, official_process_code TEXT,
                address TEXT, district TEXT, media_type TEXT, status TEXT,
                contact_name TEXT, created_at TEXT
            )
        """))
        self.connection.execute(text("""
            CREATE TABLE public.application_forms (
                id TEXT PRIMARY KEY, asset_id TEXT, company_responsible TEXT
            )
        """))
        self.assets = {}
        same_time = datetime(2026, 10, 12, 12, tzinfo=UTC)
        self.add_asset("alpha", "PROC-2026-101", "OF-101", "Rua das Flores", "Centro",
                       "outdoor", "aprovado", "Contato Oculto", same_time,
                       company="Empresa Aurora")
        self.add_asset("beta", "PROC-2026-102", None, "Avenida Central", "Jardim",
                       "front light", "análise", "Oficina Manual", same_time)
        self.add_asset("gamma", "PROC-2026-103", None, "Rua do Sol", "Centro",
                       "outdoor", "aprovado", None, same_time - timedelta(days=1),
                       company="Empresa Aurora")
        self.add_asset("delta", "PROC-2026-104", None, "Rua das Palmeiras", "Lagoa",
                       "triface", "irregular", "Outra Manual", same_time - timedelta(days=2))
        self.connection.commit()
        self.session = ListingSession(self.connection, self.assets)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.connection.close()
        self.engine.dispose()

    def add_asset(self, key, code, official, address, district, media_type, status, contact, created_at,
                  company=None):
        asset_id = uuid.uuid4()
        asset = MediaAsset(
            id=asset_id, process_code=code, official_process_code=official, address=address,
            district=district, media_type=media_type, status=status, contact_name=contact,
            contact_email="private@example.com" if contact else None,
            latitude=-20.46, longitude=-54.61, radius_meters=80,
            created_at=created_at, updated_at=created_at,
        )
        if company:
            asset.__dict__["application_form"] = SimpleNamespace(company_responsible=company, company_cnpj=None)
        self.assets[str(asset_id)] = asset
        setattr(self, key, str(asset_id))
        self.connection.execute(text("""
            INSERT INTO public.media_assets
            (id, process_code, official_process_code, address, district, media_type, status, contact_name, created_at)
            VALUES (:id, :code, :official, :address, :district, :media_type, :status, :contact, :created_at)
        """), dict(id=str(asset_id), code=code, official=official, address=address, district=district,
                   media_type=media_type, status=status, contact=contact, created_at=created_at.isoformat()))
        if company:
            self.connection.execute(text("""
                INSERT INTO public.application_forms (id, asset_id, company_responsible)
                VALUES (:id, :asset_id, :company)
            """), dict(id=str(uuid.uuid4()), asset_id=str(asset_id), company=company))

    async def list_assets(self, params=None, role="analyst"):
        async def user():
            return SimpleNamespace(role=role)

        async def session():
            yield self.session

        app.dependency_overrides[get_current_user] = user
        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/media-assets", params=params)

    async def test_multiple_types_use_union_with_correct_total_and_pagination(self):
        response = await self.list_assets([
            ("media_types", "outdoor"), ("media_types", "front light"),
            ("limit", "2"), ("offset", "1"),
        ])
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(all(item["media_type"] in {"outdoor", "front light"} for item in data["items"]))
        self.assertNotIn(self.delta, [item["id"] for item in data["items"]])

    async def test_multiple_types_combine_with_status_and_empty_selection_shows_all(self):
        response = await self.list_assets([
            ("media_types", "outdoor"), ("media_types", "front light"), ("status", "aprovado"),
        ])
        self.assertEqual(response.json()["total"], 2)
        self.assertEqual((await self.list_assets()).json()["total"], 4)
        invalid = await self.list_assets({"media_types": "invalid"})
        self.assertEqual(invalid.status_code, 422)

    async def test_pages_total_and_stable_order(self):
        first = (await self.list_assets({"limit": 2})).json()
        second = (await self.list_assets({"limit": 2, "offset": 2})).json()
        expected_tied = sorted([self.alpha, self.beta], reverse=True)
        self.assertEqual([item["id"] for item in first["items"]], expected_tied)
        self.assertEqual([item["id"] for item in second["items"]], [self.gamma, self.delta])
        self.assertEqual((first["total"], second["total"]), (4, 4))
        self.assertEqual((first["limit"], first["offset"], second["offset"]), (2, 0, 2))
        self.assertEqual(len({item["id"] for page in (first, second) for item in page["items"]}), 4)
        self.assertTrue(all(len(statement.get_final_froms()) == 1 for statement in self.session.statements))

    async def test_search_fields_and_manual_asset(self):
        cases = [
            ("PROC-2026-102", [self.beta]),
            ("OF-101", [self.alpha]),
            ("Flores", [self.alpha]),
            ("Jardim", [self.beta]),
            ("Aurora", [self.alpha, self.gamma]),
            ("Oficina Manual", [self.beta]),
        ]
        for term, expected in cases:
            with self.subTest(term=term):
                response = await self.list_assets({"search": term})
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual({item["id"] for item in body["items"]}, set(expected))
                self.assertEqual(body["total"], len(expected))

    async def test_filters_and_filtered_total(self):
        cases = [
            ({"media_type": "outdoor"}, [self.alpha, self.gamma]),
            ({"status": "aprovado"}, [self.alpha, self.gamma]),
            ({"district": "centro"}, [self.alpha, self.gamma]),
            ({"company": "empresa aurora"}, [self.alpha, self.gamma]),
            ({"company": "oficina manual"}, [self.beta]),
            ({"district": "Centro", "limit": 1}, [self.alpha]),
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                body = (await self.list_assets(params)).json()
                self.assertEqual([item["id"] for item in body["items"]], expected)
                self.assertEqual(body["total"], 2 if params.get("limit") == 1 else len(expected))

    async def test_form_contact_is_not_used_as_manual_company(self):
        body = (await self.list_assets({"search": "Contato Oculto"})).json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)

    async def test_viewer_contact_is_hidden_and_not_searchable(self):
        body = (await self.list_assets(role="viewer")).json()
        self.assertTrue(all(item["contact_name"] is None and item["contact_email"] is None
                            for item in body["items"]))
        self.assertEqual({item["id"] for item in body["items"]}, set(self.assets))
        self.assertEqual((await self.list_assets({"search": "Oficina Manual"}, role="viewer")).json()["total"], 0)
        self.assertEqual((await self.list_assets({"company": "Oficina Manual"}, role="viewer")).json()["total"], 0)
        linked = (await self.list_assets({"company": "Empresa Aurora"}, role="viewer")).json()
        self.assertEqual(linked["total"], 2)
        self.assertTrue(all(item["company_responsible"] == "Empresa Aurora" for item in linked["items"]))

    async def test_existing_limit_500_and_maximum(self):
        self.assertEqual((await self.list_assets({"limit": 500})).status_code, 200)
        self.assertEqual((await self.list_assets({"limit": 501})).status_code, 422)
