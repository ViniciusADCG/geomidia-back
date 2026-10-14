import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from app.core.security import get_current_user
from app.db.models import ApplicationForm
from app.db.session import get_session
from app.main import app
from tests.test_application_forms import linked_form


class ListingSession:
    def __init__(self, connection, forms):
        self.connection = connection
        self.forms = forms
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.connection.execute(statement).scalar_one()

    async def scalars(self, statement):
        self.statements.append(statement)
        ids_query = select(ApplicationForm.id)
        for join in statement._setup_joins:
            ids_query = ids_query.join(join[0], join[1])
        if statement.whereclause is not None:
            ids_query = ids_query.where(statement.whereclause)
        ids_query = ids_query.order_by(*statement._order_by_clauses)
        ids_query = ids_query.limit(statement._limit_clause.value).offset(statement._offset_clause.value)
        ids = self.connection.execute(ids_query).scalars().all()
        return [self.forms[str(form_id)] for form_id in ids]


class ApplicationFormListingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.connection = self.engine.connect()
        self.connection.execute(text("ATTACH DATABASE ':memory:' AS public"))
        self.connection.execute(text("""
            CREATE TABLE public.media_assets (id TEXT PRIMARY KEY, process_code TEXT)
        """))
        self.connection.execute(text("""
            CREATE TABLE public.application_forms (
                id TEXT PRIMARY KEY, asset_id TEXT, company_responsible TEXT, company_cnpj TEXT,
                municipal_registration TEXT, property_registration TEXT, street TEXT, district TEXT,
                created_at TEXT
            )
        """))
        self.forms = {}
        same_time = datetime(2026, 10, 12, 12, tzinfo=UTC)
        self.alpha = self.add_form("PROC-ALPHA", "Empresa Alfa", "Centro", same_time)
        self.beta = self.add_form("PROC-BETA", "Empresa Beta", "Jardim", same_time)
        self.gamma = self.add_form("PROC-GAMMA", "Empresa Alfa", "Centro", same_time - timedelta(days=1))
        self.delta = self.add_form("PROC-DELTA", "Empresa Delta", "Lagoa", same_time - timedelta(days=2))
        self.connection.commit()
        self.session = ListingSession(self.connection, self.forms)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.connection.close()
        self.engine.dispose()

    def add_form(self, process_code, company, district, created_at):
        form, asset = linked_form()
        form.id = uuid.uuid4()
        form.asset_id = asset.id
        form.company_responsible = company
        form.district = district
        form.created_at = created_at
        asset.process_code = process_code
        self.forms[str(form.id)] = form
        self.connection.execute(text("INSERT INTO public.media_assets (id, process_code) VALUES (:id, :code)"),
                                {"id": str(asset.id), "code": process_code})
        self.connection.execute(text("""
            INSERT INTO public.application_forms
            (id, asset_id, company_responsible, company_cnpj, municipal_registration,
             property_registration, street, district, created_at)
            VALUES (:id, :asset_id, :company, :cnpj, :municipal, :property, :street, :district, :created_at)
        """), {
            "id": str(form.id), "asset_id": str(asset.id), "company": company,
            "cnpj": form.company_cnpj, "municipal": form.municipal_registration,
            "property": form.property_registration, "street": form.street,
            "district": district, "created_at": created_at.isoformat(),
        })
        return str(form.id)

    async def request(self, params=None, role="analyst"):
        async def user():
            return SimpleNamespace(role=role)

        async def session():
            yield self.session

        app.dependency_overrides[get_current_user] = user
        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/api/application-forms", params=params)

    async def test_pages_total_last_page_and_stable_order(self):
        first = (await self.request({"limit": 2})).json()
        second = (await self.request({"limit": 2, "offset": 2})).json()
        last = (await self.request({"limit": 2, "offset": 4})).json()
        self.assertEqual((first["total"], first["limit"], first["offset"]), (4, 2, 0))
        self.assertEqual([item["id"] for item in first["items"]], sorted([self.alpha, self.beta], reverse=True))
        self.assertEqual([item["id"] for item in second["items"]], [self.gamma, self.delta])
        self.assertEqual(last["items"], [])
        self.assertEqual(last["total"], 4)
        partial = (await self.request({"limit": 3, "offset": 3})).json()
        self.assertEqual([item["id"] for item in partial["items"]], [self.delta])

    async def test_search_matches_existing_fields_process_and_district(self):
        for search, expected in [
            ("PROC-BETA", [self.beta]), ("Jardim", [self.beta]),
            ("Empresa Alfa", [self.alpha, self.gamma]), ("11222333000144", list(self.forms)),
            ("12345", list(self.forms)), ("12345678901", list(self.forms)),
            ("Afonso Pena", list(self.forms)), ("inexistente", []),
        ]:
            with self.subTest(search=search):
                result = (await self.request({"search": search})).json()
                self.assertEqual(set(item["id"] for item in result["items"]), set(expected))
                self.assertEqual(result["total"], len(expected))

    async def test_filtered_page_and_permissions(self):
        filtered = (await self.request({"search": "Empresa Alfa", "limit": 1, "offset": 1}, role="admin")).json()
        self.assertEqual(filtered["total"], 2)
        self.assertEqual([item["id"] for item in filtered["items"]], [self.gamma])
        self.assertEqual((await self.request(role="viewer")).status_code, 403)
        self.assertEqual((await self.request({"limit": 101})).status_code, 422)
        self.assertEqual((await self.request({"offset": -1})).status_code, 422)

    async def test_page_loads_attachments_without_per_row_queries(self):
        self.session.statements.clear()
        response = await self.request({"limit": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.session.statements), 2)
        self.assertEqual(response.json()["items"][0]["attachments"], [])
