import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.services.orphan_diagnostics import (
    AssetRecord,
    AttachmentRecord,
    DraftRecord,
    FormRecord,
    Snapshot,
    StorageInventoryError,
    StorageObject,
    analyze,
    list_bucket_objects,
)
from scripts.diagnose_orphans import load_database_snapshot

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=3)
RECENT = NOW - timedelta(hours=1)
WINDOW = timedelta(hours=24)


def key(draft_id: uuid.UUID, category: str = "artRrt") -> str:
    return f"public-submissions/{draft_id}/{category}/documento.pdf"


def draft(
    draft_id: uuid.UUID, *, created_at: datetime = OLD, finalized_at: datetime | None = None,
    process_code: str | None = None, process_type: str | None = "PROCESSO_NOVO",
    paths: tuple[str | None, ...] = (),
) -> DraftRecord:
    return DraftRecord(draft_id, created_at, finalized_at, process_code, process_type, paths)


def obj(path: str, *, created_at: datetime = OLD, updated_at: datetime | None = None) -> StorageObject:
    return StorageObject(path, created_at, updated_at)


def categories(report) -> list[str]:
    return [item.category for item in report.findings]


class OrphanClassificationTests(unittest.TestCase):
    def report(self, snapshot: Snapshot, *, window: timedelta = WINDOW, error: str | None = None):
        return analyze(snapshot, now=NOW, min_age=window, storage_error=error)

    def test_recent_draft_and_upload_in_progress_are_not_candidates(self):
        draft_id = uuid.uuid4()
        path = key(draft_id)
        report = self.report(Snapshot((draft(draft_id, created_at=RECENT, paths=(path,)),), (), (), (), (obj(path),)))
        self.assertEqual(report.findings, [])
        self.assertGreater(report.ignored_recent, 0)

    def test_old_unfinished_draft_and_uploaded_object_are_candidates(self):
        draft_id = uuid.uuid4()
        path = key(draft_id)
        report = self.report(Snapshot((draft(draft_id, paths=(path,)),), (), (), (), (obj(path),)))
        self.assertEqual(categories(report), ["DRAFT_ABANDONED", "STORAGE_ORPHAN_CANDIDATE"])
        self.assertEqual(report.findings[1].confidence, "medium")
        self.assertEqual(report.findings[1].draft_id, str(draft_id))

    def test_finalized_new_process_and_persisted_attachment_are_valid(self):
        draft_id, asset_id, form_id, attachment_id = (uuid.uuid4() for _ in range(4))
        path = key(draft_id)
        snapshot = Snapshot(
            (draft(draft_id, finalized_at=OLD, process_code="VEI-1-2026", paths=()),),
            (AssetRecord(asset_id, "VEI-1-2026"),),
            (FormRecord(form_id, asset_id),),
            (AttachmentRecord(attachment_id, form_id, path, OLD),),
            (obj(path),),
        )
        report = self.report(snapshot)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.valid, 3)

    def test_finalized_requirement_keeps_manifest_and_object_valid(self):
        draft_id = uuid.uuid4()
        path = key(draft_id, "respostaExigencia")
        snapshot = Snapshot((draft(
            draft_id, finalized_at=OLD, process_code="HESP-1-2026",
            process_type="RESPOSTA_COMUNICADO_EXIGENCIA", paths=(path,),
        ),), (), (), (), (obj(path),))
        report = self.report(snapshot)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.valid, 2)

    def test_old_unlinked_object_is_candidate_but_media_asset_link_protects_it(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        orphan, linked = key(first), key(second)
        asset = AssetRecord(uuid.uuid4(), "PROC-1", f"https://example.test/storage/v1/object/authenticated/application-form-attachments/{linked}")
        report = self.report(Snapshot((), (asset,), (), (), (obj(orphan), obj(linked))))
        self.assertEqual(categories(report), ["STORAGE_ORPHAN_CANDIDATE"])
        self.assertEqual(report.findings[0].storage_path, orphan)

    def test_form_legacy_link_protects_object_even_without_attachment_row(self):
        path = key(uuid.uuid4())
        asset_id = uuid.uuid4()
        report = self.report(Snapshot(
            (), (AssetRecord(asset_id, "PROC-1"),),
            (FormRecord(uuid.uuid4(), asset_id, f"https://example.test/{path}"),),
            (), (obj(path),),
        ))
        self.assertEqual(report.findings, [])

    def test_missing_storage_object_and_invalid_relation_are_attachment_inconsistencies(self):
        asset_id, form_id = uuid.uuid4(), uuid.uuid4()
        path = key(uuid.uuid4())
        missing_id, broken_id = uuid.uuid4(), uuid.uuid4()
        report = self.report(Snapshot(
            (), (AssetRecord(asset_id, "PROC-1"),), (FormRecord(form_id, asset_id),),
            (AttachmentRecord(missing_id, form_id, path, OLD),
             AttachmentRecord(broken_id, uuid.uuid4(), key(uuid.uuid4()), OLD)),
        ))
        self.assertEqual(categories(report), ["ATTACHMENT_INCONSISTENT", "ATTACHMENT_INCONSISTENT"])
        self.assertEqual({item.attachment_id for item in report.findings}, {str(missing_id), str(broken_id)})

    def test_finalized_requirement_missing_object_is_attachment_inconsistency(self):
        draft_id = uuid.uuid4()
        path = key(draft_id, "respostaExigencia")
        report = self.report(Snapshot((draft(
            draft_id, finalized_at=OLD, process_code="HESP-2-2026",
            process_type="RESPOSTA_COMUNICADO_EXIGENCIA", paths=(path,),
        ),), (), (), ()))
        self.assertEqual(categories(report), ["ATTACHMENT_INCONSISTENT"])

    def test_finalized_draft_without_result_or_protocol_is_inconsistent(self):
        missing_result_id, missing_code_id = uuid.uuid4(), uuid.uuid4()
        report = self.report(Snapshot((
            draft(missing_result_id, finalized_at=OLD, process_code="VEI-2-2026"),
            draft(missing_code_id, finalized_at=OLD),
        ), (), (), ()))
        self.assertEqual(categories(report), ["FINALIZED_DRAFT_INCONSISTENT"] * 2)

    def test_storage_error_never_infers_missing_objects(self):
        draft_id = uuid.uuid4()
        path = key(draft_id)
        asset_id, form_id = uuid.uuid4(), uuid.uuid4()
        report = self.report(Snapshot(
            (draft(draft_id, paths=(path,)),), (AssetRecord(asset_id, "PROC-1"),),
            (FormRecord(form_id, asset_id),), (AttachmentRecord(uuid.uuid4(), form_id, path, OLD),),
        ), error="Storage indisponivel")
        self.assertFalse(report.complete)
        self.assertEqual(categories(report), ["DRAFT_ABANDONED"])
        self.assertEqual(report.to_dict()["summary"]["errors"], 1)

    def test_window_and_recent_object_update_protect_against_false_positive(self):
        draft_id = uuid.uuid4()
        path = key(draft_id)
        snapshot = Snapshot((draft(draft_id, created_at=NOW - timedelta(hours=12)),), (), (), (),
                            (obj(path, updated_at=RECENT),))
        self.assertEqual(self.report(snapshot, window=timedelta(hours=24)).findings, [])
        self.assertEqual(categories(self.report(snapshot, window=timedelta(hours=6))), ["DRAFT_ABANDONED"])

    def test_window_cannot_be_shorter_than_active_draft_lifetime(self):
        with self.assertRaisesRegex(ValueError, "exceder o prazo"):
            self.report(Snapshot((), (), (), ()), window=timedelta(hours=2))

    def test_attachment_and_hesp_manifest_cannot_point_to_another_draft(self):
        new_id, hesp_id, other_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        asset_id, form_id = uuid.uuid4(), uuid.uuid4()
        wrong_new_path = key(other_id)
        wrong_hesp_path = key(uuid.uuid4(), "respostaExigencia")
        report = self.report(Snapshot(
            (draft(new_id, finalized_at=OLD, process_code="VEI-3-2026"),
             draft(hesp_id, finalized_at=OLD, process_code="HESP-3-2026",
                   process_type="RESPOSTA_COMUNICADO_EXIGENCIA", paths=(wrong_hesp_path,)),
             draft(other_id, finalized_at=OLD, process_code="VEI-99-2026")),
            (AssetRecord(asset_id, "VEI-3-2026"),), (FormRecord(form_id, asset_id),),
            (AttachmentRecord(uuid.uuid4(), form_id, wrong_new_path, OLD),),
            (obj(wrong_new_path), obj(wrong_hesp_path)),
        ))
        self.assertEqual(categories(report).count("ATTACHMENT_INCONSISTENT"), 2)
        self.assertTrue(any("processo diferente" in item.reason for item in report.findings))
        self.assertTrue(any("outro draft" in item.reason for item in report.findings))

    def test_both_public_flows_are_recognized_and_recent_retry_is_protected(self):
        new_id, hesp_id = uuid.uuid4(), uuid.uuid4()
        new_path = key(new_id)
        hesp_path = key(hesp_id, "respostaExigencia")
        report = self.report(Snapshot((
            draft(new_id, paths=(new_path,)),
            draft(hesp_id, process_type="RESPOSTA_COMUNICADO_EXIGENCIA", paths=(hesp_path,)),
        ), (), (), (), (obj(new_path), obj(hesp_path, updated_at=RECENT))))
        self.assertEqual(categories(report).count("DRAFT_ABANDONED"), 2)
        self.assertEqual(categories(report).count("STORAGE_ORPHAN_CANDIDATE"), 1)
        self.assertEqual(report.findings[-1].storage_path, new_path)

    def test_malformed_manifest_suppresses_all_storage_orphan_candidates(self):
        draft_id = uuid.uuid4()
        report = self.report(Snapshot((draft(draft_id, paths=(None,)),), (), (), (), (obj(key(draft_id)),)))
        self.assertFalse(report.complete)
        self.assertNotIn("STORAGE_ORPHAN_CANDIDATE", categories(report))


class StorageInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recursive_pagination_uses_only_listing_and_never_removes(self):
        draft_id = uuid.uuid4()
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            body = __import__("json").loads(request.content)
            prefix, offset = body["prefix"], body["offset"]
            if prefix == "" and offset == 0:
                data = [{"name": "public-submissions", "id": None, "metadata": None}]
            elif prefix == "public-submissions" and offset == 0:
                data = [{"name": str(draft_id), "id": None, "metadata": None}]
            elif prefix.endswith(str(draft_id)) and offset == 0:
                data = [{"name": "artRrt", "id": None, "metadata": None}]
            elif prefix.endswith("artRrt") and offset == 0:
                data = [{"name": "a.pdf", "id": str(uuid.uuid4()), "created_at": OLD.isoformat()}]
            elif prefix.endswith("artRrt") and offset == 1:
                data = [{"name": "b.pdf", "id": str(uuid.uuid4()), "created_at": OLD.isoformat()}]
            else:
                data = []
            return httpx.Response(200, json=data)

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        storage = SimpleNamespace(storage_url="https://storage.test/storage/v1", bucket="application-form-attachments", headers={})
        with patch("app.services.orphan_diagnostics.httpx.AsyncClient",
                   side_effect=lambda **kwargs: real_client(transport=transport)):
            objects = await list_bucket_objects(storage, page_size=1)
        self.assertEqual(len(objects), 2)
        self.assertTrue(all(method == "POST" and "/object/list/" in path for method, path in seen))

    async def test_failed_listing_rejects_partial_inventory(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json=[{"name": "folder", "id": None, "metadata": None}])
            return httpx.Response(503)

        real_client = httpx.AsyncClient
        storage = SimpleNamespace(storage_url="https://storage.test/storage/v1", bucket="bucket", headers={})
        with patch("app.services.orphan_diagnostics.httpx.AsyncClient",
                   side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler))):
            with self.assertRaises(StorageInventoryError):
                await list_bucket_objects(storage)


class ReadOnlyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_read_only_transaction_and_selects_only(self):
        class FakeResult:
            def all(self):
                return []

        class FakeSession:
            def __init__(self):
                self.statements = []

            async def execute(self, statement):
                self.statements.append(str(statement))
                return FakeResult()

        session = FakeSession()
        snapshot = await load_database_snapshot(session)
        self.assertEqual(snapshot.drafts, ())
        self.assertEqual(session.statements[0], "SET TRANSACTION READ ONLY")
        self.assertTrue(all(statement.lstrip().startswith("SELECT") for statement in session.statements[1:]))
        self.assertFalse(any("DELETE" in statement.upper() for statement in session.statements))
