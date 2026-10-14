"""Read-only inventory and conservative classification of public uploads."""

from __future__ import annotations

import re
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote

import httpx

from app.api.routes.public_submissions import DRAFT_LIFETIME, UPLOAD_RULES
from app.services.storage import SupabaseStorage

PUBLIC_PREFIX = "public-submissions"
PROCESS_NEW = "PROCESSO_NOVO"
PROCESS_REQUIREMENT = "RESPOSTA_COMUNICADO_EXIGENCIA"
VEI_CODE = re.compile(r"^VEI-\d+-\d{4}$")
HESP_CODE = re.compile(r"^HESP-\d+-\d{4}$")
KNOWN_CATEGORIES = frozenset((*UPLOAD_RULES, "respostaExigencia"))


class StorageInventoryError(RuntimeError):
    """The Storage inventory is incomplete and cannot establish absence."""


@dataclass(frozen=True)
class DraftRecord:
    id: uuid.UUID
    created_at: datetime
    finalized_at: datetime | None
    process_code: str | None
    process_type: str | None
    attachment_paths: tuple[str | None, ...]
    receipt_sent_at: datetime | None = None


@dataclass(frozen=True)
class AssetRecord:
    id: uuid.UUID
    process_code: str
    attachment_links: str | None = None


@dataclass(frozen=True)
class FormRecord:
    id: uuid.UUID
    asset_id: uuid.UUID
    attachment_links: str | None = None


@dataclass(frozen=True)
class AttachmentRecord:
    id: uuid.UUID
    form_id: uuid.UUID
    path: str
    created_at: datetime


@dataclass(frozen=True)
class StorageObject:
    path: str
    created_at: datetime
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Snapshot:
    drafts: tuple[DraftRecord, ...]
    assets: tuple[AssetRecord, ...]
    forms: tuple[FormRecord, ...]
    attachments: tuple[AttachmentRecord, ...]
    objects: tuple[StorageObject, ...] = ()


@dataclass(frozen=True)
class Finding:
    category: str
    identifier: str
    reason: str
    confidence: str
    age_hours: float | None = None
    draft_id: str | None = None
    attachment_id: str | None = None
    storage_path: str | None = None
    link: str | None = None


@dataclass
class DiagnosticReport:
    generated_at: str
    min_age_hours: float
    complete: bool
    analyzed: dict[str, int]
    valid: int
    ignored_recent: int
    ignored_outside_namespace: int
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        counts = {category: 0 for category in (
            "DRAFT_ABANDONED", "STORAGE_ORPHAN_CANDIDATE",
            "ATTACHMENT_INCONSISTENT", "FINALIZED_DRAFT_INCONSISTENT",
        )}
        counts.update(Counter(item.category for item in self.findings))
        return {
            "dry_run": True,
            "generated_at": self.generated_at,
            "min_age_hours": self.min_age_hours,
            "complete": self.complete,
            "summary": {
                "total_analyzed": sum(self.analyzed.values()),
                "analyzed": self.analyzed,
                "valid": self.valid,
                "candidates_by_category": counts,
                "ignored_recent": self.ignored_recent,
                "ignored_outside_namespace": self.ignored_outside_namespace,
                "errors": len(self.errors),
            },
            "findings": [asdict(item) for item in self.findings],
            "errors": self.errors,
        }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamp sem timezone no inventario")
    return value.astimezone(UTC)


def _key(value: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("Storage key invalida")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Storage key invalida")
    return value


def _draft_id_from_key(path: str) -> uuid.UUID | None:
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != PUBLIC_PREFIX or parts[2] not in KNOWN_CATEGORIES:
        return None
    try:
        draft_id = uuid.UUID(parts[1])
    except ValueError:
        return None
    return draft_id if str(draft_id) == parts[1] else None


def _age_hours(now: datetime, then: datetime) -> float:
    return round(max(0.0, (now - _aware(then)).total_seconds() / 3600), 2)


def _legacy_text(value: str) -> str:
    return unquote(unquote(value))


def analyze(
    snapshot: Snapshot,
    *,
    now: datetime,
    min_age: timedelta,
    storage_error: str | None = None,
) -> DiagnosticReport:
    """Classify a complete inventory; absence claims are disabled on Storage failure."""
    now = _aware(now)
    if min_age <= DRAFT_LIFETIME:
        raise ValueError("A janela minima deve exceder o prazo de finalizacao do draft")
    cutoff = now - min_age
    storage_complete = storage_error is None
    errors = [storage_error] if storage_error else []
    findings: list[Finding] = []
    valid = 0
    recent = 0
    outside = 0
    valid_finalized_drafts: set[uuid.UUID] = set()
    verified_attachment_paths: set[str] = set()
    drafts = {item.id: item for item in snapshot.drafts}
    assets_by_code = {item.process_code: item for item in snapshot.assets}
    assets_by_id = {item.id: item for item in snapshot.assets}
    forms_by_asset: dict[uuid.UUID, list[FormRecord]] = {}
    forms = {item.id: item for item in snapshot.forms}
    for form in snapshot.forms:
        forms_by_asset.setdefault(form.asset_id, []).append(form)
    attachments_by_form = Counter(item.form_id for item in snapshot.attachments)
    objects = {item.path: item for item in snapshot.objects} if storage_complete else {}
    if storage_complete and len(objects) != len(snapshot.objects):
        storage_complete = False
        errors.append("Storage retornou keys duplicadas; ausencias nao serao inferidas")

    draft_refs: dict[str, list[DraftRecord]] = {}
    malformed_manifest = False
    for draft in snapshot.drafts:
        for path in draft.attachment_paths:
            if not isinstance(path, str):
                malformed_manifest = True
                continue
            try:
                _key(path)
            except ValueError:
                malformed_manifest = True
                continue
            draft_refs.setdefault(path, []).append(draft)
    if malformed_manifest:
        errors.append("Manifesto de draft com key ausente/invalida; candidatos Storage foram suprimidos")
    attachment_refs = {item.path for item in snapshot.attachments}
    legacy_links = [
        _legacy_text(link)
        for item in (*snapshot.assets, *snapshot.forms)
        if (link := item.attachment_links)
    ]
    ambiguous_legacy_links = any(
        PUBLIC_PREFIX in link and not any(path in link for path in objects)
        for link in legacy_links
    )
    if ambiguous_legacy_links:
        errors.append("Link legado do namespace publico nao conciliado; candidatos Storage foram suprimidos")
    can_report_orphans = storage_complete and not malformed_manifest and not ambiguous_legacy_links

    code_counts = Counter(item.process_code for item in snapshot.drafts if item.finalized_at and item.process_code)
    for draft in snapshot.drafts:
        age = _age_hours(now, draft.created_at)
        if draft.finalized_at is None:
            if draft.process_code or draft.receipt_sent_at:
                if _aware(draft.created_at) <= cutoff:
                    findings.append(Finding(
                        "FINALIZED_DRAFT_INCONSISTENT", str(draft.id),
                        "Draft sem finalized_at, mas com protocolo ou envio de comprovante registrado", "high",
                        age, str(draft.id), link="public_submission_drafts",
                    ))
                else:
                    recent += 1
            elif _aware(draft.created_at) <= cutoff:
                findings.append(Finding(
                    "DRAFT_ABANDONED", str(draft.id),
                    "Draft nao finalizado alem da janela minima", "medium", age, str(draft.id),
                    link="public_submission_drafts",
                ))
            else:
                recent += 1
            continue

        finalized_age = _age_hours(now, draft.finalized_at)
        if _aware(draft.finalized_at) > cutoff:
            recent += 1
            continue
        code = draft.process_code or ""
        reason = None
        if not code:
            reason = "Draft finalizado sem protocolo"
        elif code_counts[code] > 1:
            reason = "Protocolo repetido entre drafts finalizados"
        elif HESP_CODE.fullmatch(code):
            if draft.process_type != PROCESS_REQUIREMENT:
                reason = "HESP finalizado sem payload de resposta correspondente"
            elif not draft.attachment_paths:
                reason = "HESP finalizado sem manifesto de anexos"
        elif VEI_CODE.fullmatch(code):
            asset = assets_by_code.get(code)
            linked_forms = forms_by_asset.get(asset.id, []) if asset else []
            if asset is None or len(linked_forms) != 1:
                reason = "VEI finalizado sem MediaAsset e ApplicationForm unicos"
            elif attachments_by_form[linked_forms[0].id] == 0:
                reason = "VEI finalizado sem attachments persistidos"
        else:
            reason = "Protocolo finalizado fora dos formatos VEI/HESP"
        if reason:
            findings.append(Finding(
                "FINALIZED_DRAFT_INCONSISTENT", str(draft.id), reason, "high", finalized_age,
                str(draft.id), link=code or "public_submission_drafts",
            ))
        else:
            valid += 1
            valid_finalized_drafts.add(draft.id)

    for attachment in snapshot.attachments:
        age = _age_hours(now, attachment.created_at)
        form = forms.get(attachment.form_id)
        reason = None
        if form is None or form.asset_id not in assets_by_id:
            reason = "Attachment sem ApplicationForm/MediaAsset valido"
        elif storage_complete:
            try:
                _key(attachment.path)
            except ValueError:
                reason = "Attachment com Storage key invalida"
            if reason is None and storage_complete and attachment.path not in objects:
                reason = "Attachment persistido sem objeto no Storage"
            owner_id = _draft_id_from_key(attachment.path) if reason is None else None
            owner = drafts.get(owner_id) if owner_id else None
            if reason is None and owner and owner.process_code and owner.process_code != assets_by_id[form.asset_id].process_code:
                reason = "Attachment associado a processo diferente do draft da Storage key"
        if reason is None:
            if storage_complete:
                valid += 1
                verified_attachment_paths.add(attachment.path)
        elif _aware(attachment.created_at) <= cutoff:
            findings.append(Finding(
                "ATTACHMENT_INCONSISTENT", str(attachment.id), reason, "high", age,
                attachment_id=str(attachment.id), storage_path=attachment.path, link=str(attachment.form_id),
            ))
        else:
            recent += 1

    for draft in snapshot.drafts:
        if not draft.finalized_at or not HESP_CODE.fullmatch(draft.process_code or ""):
            continue
        for index, path in enumerate(draft.attachment_paths):
            reason = None
            if not isinstance(path, str):
                reason = "Manifesto HESP sem Storage key"
            else:
                try:
                    _key(path)
                except ValueError:
                    reason = "Manifesto HESP com Storage key invalida"
                if reason is None and _draft_id_from_key(path) != draft.id:
                    reason = "Manifesto HESP aponta para key de outro draft"
                if reason is None and storage_complete and path not in objects:
                    reason = "Anexo HESP finalizado sem objeto no Storage"
            if reason and _aware(draft.finalized_at) <= cutoff:
                findings.append(Finding(
                    "ATTACHMENT_INCONSISTENT", f"{draft.id}:{index}", reason, "high",
                    _age_hours(now, draft.finalized_at), str(draft.id), storage_path=path,
                    link=draft.process_code,
                ))
            elif reason:
                recent += 1

    for obj in snapshot.objects:
        path = obj.path
        age_from = max(_aware(obj.created_at), _aware(obj.updated_at)) if obj.updated_at else _aware(obj.created_at)
        age = _age_hours(now, age_from)
        if path in attachment_refs:
            if path in verified_attachment_paths:
                valid += 1
            continue
        if any(path in link for link in legacy_links):
            valid += 1
            continue
        linked_drafts = draft_refs.get(path, [])
        if linked_drafts and any(d.finalized_at or _aware(d.created_at) > cutoff for d in linked_drafts):
            if any(d.id in valid_finalized_drafts for d in linked_drafts):
                valid += 1
            elif any(not d.finalized_at and _aware(d.created_at) > cutoff for d in linked_drafts):
                recent += 1
            continue
        draft_id = _draft_id_from_key(path)
        if draft_id is None:
            outside += 1
            continue
        owner = drafts.get(draft_id)
        if age_from > cutoff or (owner and owner.finalized_at and _aware(owner.finalized_at) > cutoff):
            recent += 1
            continue
        if not can_report_orphans:
            continue
        reason = (
            "Objeto ligado somente a draft antigo nao finalizado"
            if linked_drafts else "Objeto antigo sem attachment, manifesto de draft ou link legado conhecido"
        )
        findings.append(Finding(
            "STORAGE_ORPHAN_CANDIDATE", path, reason, "medium" if linked_drafts else "high",
            age, str(draft_id), storage_path=path,
            link="draft abandonado" if linked_drafts else None,
        ))

    return DiagnosticReport(
        generated_at=now.isoformat(), min_age_hours=round(min_age.total_seconds() / 3600, 2),
        complete=not errors, analyzed={
            "drafts": len(snapshot.drafts), "attachments": len(snapshot.attachments),
            "storage_objects": len(snapshot.objects) if storage_complete else 0,
        },
        valid=valid, ignored_recent=recent, ignored_outside_namespace=outside,
        findings=findings, errors=errors,
    )


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise StorageInventoryError("Storage nao forneceu timestamp de criacao")
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise StorageInventoryError("Storage forneceu timestamp invalido") from exc


async def list_bucket_objects(
    storage: SupabaseStorage, *, page_size: int = 100, max_entries: int = 100_000,
) -> tuple[StorageObject, ...]:
    """Recursively list the configured bucket; never return a partial inventory."""
    if page_size <= 0 or max_entries <= 0:
        raise ValueError("Limites de inventario devem ser positivos")
    folders = deque([""])
    visited: set[str] = set()
    objects: dict[str, StorageObject] = {}
    seen_entries = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while folders:
            prefix = folders.popleft()
            if prefix in visited:
                raise StorageInventoryError("Storage retornou pasta duplicada")
            visited.add(prefix)
            offset = 0
            while True:
                try:
                    response = await client.post(
                        f"{storage.storage_url}/object/list/{quote(storage.bucket, safe='')}",
                        headers=storage.headers,
                        json={"prefix": prefix, "limit": page_size, "offset": offset,
                              "sortBy": {"column": "name", "order": "asc"}},
                    )
                    response.raise_for_status()
                    entries = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise StorageInventoryError("Falha ao listar Storage; inventario incompleto") from exc
                if not isinstance(entries, list):
                    raise StorageInventoryError("Resposta de listagem do Storage invalida")
                seen_entries += len(entries)
                if seen_entries > max_entries:
                    raise StorageInventoryError("Inventario excedeu max_entries; sem conclusao sobre orfaos")
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                        raise StorageInventoryError("Entrada invalida na listagem do Storage")
                    name = entry["name"]
                    if prefix and name.startswith(prefix + "/"):
                        path = name
                    elif prefix:
                        path = f"{prefix}/{name}"
                    else:
                        path = name
                    try:
                        _key(path)
                    except ValueError as exc:
                        raise StorageInventoryError("Storage retornou key invalida") from exc
                    if entry.get("bucket_id") not in {None, storage.bucket}:
                        raise StorageInventoryError("Storage retornou objeto de outro bucket")
                    if entry.get("id") is None and entry.get("metadata") is None:
                        folders.append(path)
                        continue
                    if not isinstance(entry.get("id"), str):
                        raise StorageInventoryError("Objeto do Storage sem id")
                    if path in objects:
                        raise StorageInventoryError("Storage retornou objeto duplicado")
                    created_at = _parse_time(entry.get("created_at"))
                    updated_at = _parse_time(entry["updated_at"]) if entry.get("updated_at") else None
                    objects[path] = StorageObject(path, created_at, updated_at)
                if len(entries) < page_size:
                    break
                offset += len(entries)
    return tuple(objects.values())
