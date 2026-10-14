"""Run a read-only, JSON dry-run of public draft and Storage consistency."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Allow `python scripts/diagnose_orphans.py` without installing the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.public_submissions import DRAFT_LIFETIME  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.models import (  # noqa: E402
    ApplicationForm,
    ApplicationFormAttachment,
    MediaAsset,
    PublicSubmissionDraft,
)
from app.db.session import SessionLocal  # noqa: E402
from app.services.orphan_diagnostics import (  # noqa: E402
    AssetRecord,
    AttachmentRecord,
    DraftRecord,
    FormRecord,
    Snapshot,
    StorageInventoryError,
    analyze,
    list_bucket_objects,
)
from app.services.storage import StorageConfigurationError, SupabaseStorage  # noqa: E402


async def load_database_snapshot(session: AsyncSession) -> Snapshot:
    """Load only columns needed for diagnosis in a read-only transaction."""
    await session.execute(text("SET TRANSACTION READ ONLY"))
    draft_rows = (await session.execute(select(
        PublicSubmissionDraft.id,
        PublicSubmissionDraft.created_at,
        PublicSubmissionDraft.finalized_at,
        PublicSubmissionDraft.process_code,
        PublicSubmissionDraft.payload["process_type"].astext.label("process_type"),
        PublicSubmissionDraft.attachments,
        PublicSubmissionDraft.receipt_sent_at,
    ))).all()
    asset_rows = (await session.execute(select(
        MediaAsset.id, MediaAsset.process_code, MediaAsset.attachment_links,
    ))).all()
    form_rows = (await session.execute(select(
        ApplicationForm.id, ApplicationForm.asset_id, ApplicationForm.attachment_links,
    ))).all()
    attachment_rows = (await session.execute(select(
        ApplicationFormAttachment.id, ApplicationFormAttachment.application_form_id,
        ApplicationFormAttachment.object_path, ApplicationFormAttachment.created_at,
    ))).all()

    drafts = []
    for row in draft_rows:
        manifest = row.attachments
        paths = (
            tuple(item.get("object_path") if isinstance(item, dict) else None for item in manifest)
            if isinstance(manifest, list) else (None,)
        )
        drafts.append(DraftRecord(
            row.id, row.created_at, row.finalized_at, row.process_code,
            row.process_type, paths, row.receipt_sent_at,
        ))
    return Snapshot(
        drafts=tuple(drafts),
        assets=tuple(AssetRecord(row.id, row.process_code, row.attachment_links) for row in asset_rows),
        forms=tuple(FormRecord(row.id, row.asset_id, row.attachment_links) for row in form_rows),
        attachments=tuple(AttachmentRecord(
            row.id, row.application_form_id, row.object_path, row.created_at,
        ) for row in attachment_rows),
    )


async def run(min_age_hours: float, max_entries: int) -> dict:
    try:
        async with SessionLocal() as session:
            snapshot = await load_database_snapshot(session)
    except Exception:
        return {
            "dry_run": True, "complete": False,
            "errors": ["Falha na leitura do banco; nenhum candidato foi inferido"],
            "findings": [],
        }

    storage_error = None
    try:
        storage = SupabaseStorage(get_settings())
        objects = await list_bucket_objects(storage, max_entries=max_entries)
        snapshot = replace(snapshot, objects=objects)
    except (StorageConfigurationError, StorageInventoryError) as exc:
        storage_error = str(exc)

    return analyze(
        snapshot, now=datetime.now(UTC), min_age=timedelta(hours=min_age_hours),
        storage_error=storage_error,
    ).to_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnostico dry-run de drafts e uploads publicos")
    parser.add_argument("--min-age-hours", required=True, type=float, help="Janela minima positiva definida pelo operador")
    parser.add_argument("--max-entries", type=int, default=100_000, help="Limite de seguranca da listagem Storage")
    args = parser.parse_args()
    if not math.isfinite(args.min_age_hours) or args.max_entries <= 0:
        parser.error("--min-age-hours deve ser finito e --max-entries positivo")
    if timedelta(hours=args.min_age_hours) <= DRAFT_LIFETIME:
        parser.error("--min-age-hours deve exceder o prazo de finalizacao de 2 horas")
    report = asyncio.run(run(args.min_age_hours, args.max_entries))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
