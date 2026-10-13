import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.security import get_current_user
from app.db.models import ApplicationForm, MediaAsset
from app.db.session import get_session
from app.main import app

VIEWPORT = {"north": -20.459, "south": -20.461, "east": -54.609, "west": -54.611}


class MapRequestValidationTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    async def request(self, params, *, authenticated=True, path="/api/media-assets/map"):
        if authenticated:
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role="viewer")

        async def session():
            yield object()

        app.dependency_overrides[get_session] = session
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params)

    async def test_invalid_and_nonfinite_viewports_are_rejected(self):
        invalid = (
            {**VIEWPORT, "north": VIEWPORT["south"]},
            {**VIEWPORT, "east": VIEWPORT["west"]},
            {**VIEWPORT, "west": 179, "east": -179},
            {**VIEWPORT, "north": 91},
            {**VIEWPORT, "west": -181},
            {**VIEWPORT, "north": "nan"},
            {**VIEWPORT, "east": "inf"},
        )
        for params in invalid:
            with self.subTest(params=params):
                self.assertEqual((await self.request(params)).status_code, 422)

    async def test_authentication_is_required(self):
        self.assertEqual((await self.request(VIEWPORT, authenticated=False)).status_code, 401)

    def test_openapi_contract(self):
        paths = app.openapi()["paths"]
        operation = paths["/api/media-assets/map"]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in operation["parameters"]},
            {"north", "south", "east", "west", "media_type", "status", "company"},
        )
        self.assertIn("/api/application-forms/by-asset/{asset_id}", paths)
        cluster = paths["/api/media-assets/map/clusters"]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in cluster["parameters"]},
            {"north", "south", "east", "west", "zoom", "media_type", "status", "company"},
        )
        self.assertIn("/api/media-assets/map/company-options", paths)

    async def test_cluster_viewport_validation_and_authentication(self):
        self.assertEqual(
            (await self.request(
                {**VIEWPORT, "zoom": 12, "north": VIEWPORT["south"]},
                path="/api/media-assets/map/clusters",
            )).status_code,
            422,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            app.dependency_overrides.clear()
            response = await client.get("/api/media-assets/map/clusters", params={**VIEWPORT, "zoom": 12})
            self.assertEqual(response.status_code, 401)


class MapPostgisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database_url = os.getenv("DATABASE_URL", "")
        database_name = database_url.rsplit("/", 1)[-1].split("?", 1)[0]
        if not database_name.endswith("_test"):
            self.skipTest("PostGIS integration requires an explicit *_test DATABASE_URL")

        self.engine = create_async_engine(database_url)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.session = AsyncSession(bind=self.connection, expire_on_commit=False)
        self.serial = 0

        async def session_override():
            yield self.session

        app.dependency_overrides[get_session] = session_override

    async def asyncTearDown(self):
        app.dependency_overrides.clear()
        if hasattr(self, "session"):
            await self.session.close()
            await self.transaction.rollback()
            await self.connection.close()
            await self.engine.dispose()

    async def add_asset(
        self,
        *,
        latitude=-20.46,
        longitude=-54.61,
        radius=80,
        media_type="outdoor",
        status="aprovado",
        contact_name=None,
        company=None,
        created_at=None,
    ):
        self.serial += 1
        asset = MediaAsset(
            id=uuid4(),
            process_code=f"MAP-{uuid4().hex[:20]}",
            media_type=media_type,
            status=status,
            address="Rua de teste, 1",
            district="Centro",
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius,
            contact_name=contact_name,
            contact_email="contact@example.com" if contact_name else None,
            created_at=created_at or datetime(2026, 10, 13, 12, self.serial % 60, tzinfo=UTC),
        )
        self.session.add(asset)
        await self.session.flush()
        if company:
            self.session.add(ApplicationForm(
                id=uuid4(),
                asset_id=asset.id,
                company_responsible=company,
                municipal_registration="12345",
                property_registration="12345678901",
                street="Rua de teste",
                number="1",
                district="Centro",
                postal_code="79000-000",
                latitude=latitude,
                longitude=longitude,
                media_type=media_type,
                requester_email="company@example.com",
            ))
            await self.session.flush()
        return asset

    async def request(self, path="/api/media-assets/map", params=None, role="analyst"):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role=role)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, params=params if params is not None else VIEWPORT)

    async def test_center_inside_viewport(self):
        asset = await self.add_asset()
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["id"], str(asset.id))
        self.assertEqual(body["items"][0]["radius_meters"], 80)
        self.assertEqual(set(body["items"][0]), {
            "id", "latitude", "longitude", "radius_meters", "media_type", "status",
            "process_code", "official_process_code", "company_responsible", "contact_name",
        })

    async def test_radius_intersects_from_every_side_and_corner(self):
        cases = (
            (-20.4579, -54.61, 250),
            (-20.4621, -54.61, 250),
            (-20.46, -54.6079, 250),
            (-20.46, -54.6121, 250),
            (-20.4583, -54.6083, 250),
        )
        assets = [await self.add_asset(latitude=lat, longitude=lon, radius=radius) for lat, lon, radius in cases]
        body = (await self.request()).json()
        self.assertEqual(body["total"], len(assets))
        self.assertEqual({item["id"] for item in body["items"]}, {str(asset.id) for asset in assets})

    async def test_nonintersecting_circle_is_excluded(self):
        await self.add_asset(latitude=-20.455, radius=80)
        body = (await self.request()).json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["items"], [])
        self.assertFalse(body["too_many"])

    async def test_small_and_large_individual_radii(self):
        small = await self.add_asset(latitude=-20.458995, radius=1)
        large = await self.add_asset(latitude=-20.453, radius=1000)
        await self.add_asset(latitude=-20.45898, radius=1, longitude=-54.6088)
        body = (await self.request()).json()
        self.assertEqual({item["id"] for item in body["items"]}, {str(small.id), str(large.id)})

    async def test_same_center_uses_each_asset_radius(self):
        short = await self.add_asset(latitude=-20.457, radius=80)
        long = await self.add_asset(latitude=-20.457, radius=250)
        body = (await self.request()).json()
        self.assertEqual([item["id"] for item in body["items"]], [str(long.id)])
        self.assertNotEqual(short.id, long.id)

    async def test_wide_viewport_uses_latitude_parallel_not_geodesic_chord(self):
        asset = await self.add_asset(latitude=-20.4595, radius=80)
        viewport = {"north": -20.46, "south": -20.47, "east": -53.5, "west": -55.5}
        body = (await self.request(params=viewport)).json()
        self.assertEqual([item["id"] for item in body["items"]], [str(asset.id)])

    async def test_outlier_coordinate_falls_back_to_exact_spatial_match(self):
        asset = await self.add_asset(latitude=60.001, longitude=10, radius=250)
        viewport = {"north": 60, "south": 59.99, "east": 10.01, "west": 9.99}
        body = (await self.request(params=viewport)).json()
        self.assertEqual([item["id"] for item in body["items"]], [str(asset.id)])

    async def test_radius_above_prefilter_range_uses_exact_match(self):
        asset = await self.add_asset(radius=200_000)
        viewport = {"north": -19, "south": -19.01, "east": -54.6, "west": -54.62}
        body = (await self.request(params=viewport)).json()
        self.assertEqual([item["id"] for item in body["items"]], [str(asset.id)])

    async def test_type_status_and_company_filters(self):
        linked = await self.add_asset(media_type="outdoor", company="Empresa Aurora")
        manual = await self.add_asset(media_type="triface", status="análise", contact_name="Oficina Manual")
        await self.add_asset(media_type="front light", status="irregular", company="Outra Empresa")
        cases = (
            ({"media_type": "outdoor"}, linked.id),
            ({"status": "análise"}, manual.id),
            ({"company": "empresa aurora"}, linked.id),
            ({"company": "oficina manual"}, manual.id),
        )
        for extra, expected_id in cases:
            with self.subTest(extra=extra):
                body = (await self.request(params={**VIEWPORT, **extra})).json()
                self.assertEqual(body["total"], 1)
                self.assertEqual(body["items"][0]["id"], str(expected_id))

    async def test_manual_and_linked_assets_and_viewer_permissions(self):
        manual = await self.add_asset(contact_name="Oficina Manual")
        linked = await self.add_asset(company="Empresa Aurora")
        analyst = (await self.request()).json()
        by_id = {item["id"]: item for item in analyst["items"]}
        self.assertEqual(by_id[str(manual.id)]["contact_name"], "Oficina Manual")
        self.assertEqual(by_id[str(linked.id)]["company_responsible"], "Empresa Aurora")
        self.assertIsNone(by_id[str(linked.id)]["contact_name"])
        viewer = (await self.request(role="viewer")).json()
        self.assertEqual(viewer["total"], 2)
        self.assertTrue(all(item["contact_name"] is None for item in viewer["items"]))
        self.assertEqual(
            (await self.request(params={**VIEWPORT, "company": "Oficina Manual"}, role="viewer")).json()["total"],
            0,
        )
        self.assertEqual(
            (await self.request(params={**VIEWPORT, "company": "Empresa Aurora"}, role="viewer")).json()["total"],
            1,
        )

    async def test_response_order_is_deterministic(self):
        created_at = datetime(2026, 10, 13, tzinfo=UTC)
        assets = [await self.add_asset(created_at=created_at) for _ in range(3)]
        body = (await self.request()).json()
        self.assertEqual(
            [item["id"] for item in body["items"]],
            sorted((str(asset.id) for asset in assets), reverse=True),
        )

    async def test_dense_viewport_reports_exact_total_without_partial_items(self):
        rows = []
        for _ in range(1001):
            rows.append({
                "id": uuid4(),
                "process_code": f"MAP-BULK-{uuid4().hex[:19]}",
                "media_type": "outdoor",
                "status": "aprovado",
                "address": "Rua de teste, 1",
                "district": "Centro",
                "latitude": -20.46,
                "longitude": -54.61,
                "radius_meters": 80,
                "created_at": datetime(2026, 10, 13, tzinfo=UTC),
            })
        await self.session.execute(insert(MediaAsset), rows)
        await self.session.flush()
        body = (await self.request()).json()
        self.assertEqual(body, {"items": [], "total": 1001, "too_many": True, "max_items": 1000})

    async def test_detail_by_id_and_selected_form_on_demand(self):
        linked = await self.add_asset(company="Empresa Aurora", contact_name="Contato")
        manual = await self.add_asset(contact_name="Oficina Manual")
        asset_detail = await self.request(path=f"/api/media-assets/{linked.id}", role="viewer")
        self.assertEqual(asset_detail.status_code, 200)
        self.assertIsNone(asset_detail.json()["contact_name"])
        form = await self.request(path=f"/api/application-forms/by-asset/{linked.id}")
        self.assertEqual(form.status_code, 200)
        self.assertEqual(form.json()["company_responsible"], "Empresa Aurora")
        self.assertEqual(
            (await self.request(path=f"/api/application-forms/by-asset/{manual.id}")).status_code, 404,
        )
        self.assertEqual(
            (await self.request(path=f"/api/application-forms/by-asset/{linked.id}", role="viewer")).status_code,
            403,
        )

    async def test_clusters_include_edge_radii_and_exclude_nonintersections(self):
        for latitude, longitude, radius in (
            (-20.46, -54.61, 80),
            (-20.4579, -54.61, 250),
            (-20.4621, -54.61, 250),
            (-20.46, -54.6079, 250),
            (-20.46, -54.6121, 250),
            (-20.4583, -54.6083, 250),
        ):
            await self.add_asset(latitude=latitude, longitude=longitude, radius=radius)
        await self.add_asset(latitude=-20.455, radius=80)
        response = await self.request(path="/api/media-assets/map/clusters", params={**VIEWPORT, "zoom": 12})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 6)
        self.assertEqual(sum(cluster["count"] for cluster in body["clusters"]), 6)
        self.assertTrue(all(set(cluster) == {"id", "count", "latitude", "longitude"} for cluster in body["clusters"]))

    async def test_clusters_filters_zoom_empty_and_determinism(self):
        await self.add_asset(media_type="outdoor", company="Empresa Aurora")
        await self.add_asset(media_type="triface", status="análise", contact_name="Oficina Manual")
        await self.add_asset(media_type="outdoor", company="Empresa Aurora", longitude=-54.6107)
        path = "/api/media-assets/map/clusters"
        params = {**VIEWPORT, "zoom": 12}
        first = (await self.request(path=path, params=params)).json()
        self.assertEqual(first, (await self.request(path=path, params=params)).json())
        self.assertEqual(first["total"], 3)
        self.assertGreaterEqual(len(first["clusters"]), 2)
        self.assertEqual(sum(cluster["count"] for cluster in first["clusters"]), 3)
        self.assertNotEqual(first["clusters"], (await self.request(path=path, params={**params, "zoom": 15})).json()["clusters"])
        for extra, expected in (
            ({"media_type": "outdoor"}, 2),
            ({"status": "análise"}, 1),
            ({"company": "Empresa Aurora"}, 2),
            ({"company": "Oficina Manual"}, 1),
        ):
            body = (await self.request(path=path, params={**params, **extra})).json()
            self.assertEqual(body["total"], expected)
            self.assertEqual(sum(cluster["count"] for cluster in body["clusters"]), expected)
        viewer = (await self.request(path=path, params={**params, "company": "Oficina Manual"}, role="viewer")).json()
        self.assertEqual(viewer, {"clusters": [], "total": 0})
        empty = (await self.request(path=path, params={**params, "company": "Ausente"})).json()
        self.assertEqual(empty, {"clusters": [], "total": 0})

    async def test_company_options_respect_manual_contact_permissions(self):
        await self.add_asset(company="Empresa Aurora")
        await self.add_asset(contact_name="Oficina Manual")
        path = "/api/media-assets/map/company-options"
        self.assertEqual((await self.request(path=path)).json(), ["Empresa Aurora", "Oficina Manual"])
        self.assertEqual((await self.request(path=path, role="viewer")).json(), ["Empresa Aurora"])

    async def test_clustering_counts_all_dense_results_without_limit(self):
        rows = [
            {
                "id": uuid4(),
                "process_code": f"MAP-CLUSTER-{uuid4().hex[:18]}",
                "media_type": "outdoor",
                "status": "aprovado",
                "address": "Rua de teste, 1",
                "district": "Centro",
                "latitude": -20.46,
                "longitude": -54.61,
                "radius_meters": 80,
            }
            for _ in range(1001)
        ]
        await self.session.execute(insert(MediaAsset), rows)
        await self.session.flush()
        body = (await self.request(path="/api/media-assets/map/clusters", params={**VIEWPORT, "zoom": 12})).json()
        self.assertEqual(body["total"], 1001)
        self.assertEqual(sum(cluster["count"] for cluster in body["clusters"]), 1001)
