from dataclasses import dataclass
from urllib.parse import quote, urljoin

import httpx

from app.core.config import Settings, get_settings


class StorageConfigurationError(RuntimeError):
    pass


class StorageRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignedUpload:
    path: str
    signed_url: str


@dataclass(frozen=True)
class ObjectMetadata:
    size_bytes: int | None
    content_type: str | None


class SupabaseStorage:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.supabase_url or not self.settings.supabase_service_role_key:
            raise StorageConfigurationError("Supabase Storage nao configurado.")
        supabase_url = self.settings.supabase_url.strip().rstrip("/")
        api_key = self.settings.supabase_service_role_key.strip()
        if not supabase_url.startswith(("https://", "http://")):
            raise StorageConfigurationError("SUPABASE_URL deve incluir http:// ou https://.")
        if not api_key:
            raise StorageConfigurationError("Supabase Storage nao configurado.")
        self.storage_url = f"{supabase_url}/storage/v1"
        self.bucket = self.settings.supabase_storage_bucket
        self.headers = {"apikey": api_key}
        # Legacy service_role keys are JWTs and can be sent as Bearer tokens.
        # Current sb_secret keys must be sent only through the apikey header.
        if not api_key.startswith("sb_secret_"):
            self.headers["Authorization"] = f"Bearer {api_key}"

    async def create_signed_upload(self, object_path: str) -> SignedUpload:
        encoded_path = quote(f"{self.bucket}/{object_path}", safe="/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self.storage_url}/object/upload/sign/{encoded_path}",
                    headers=self.headers,
                    json={},
                )
        except httpx.HTTPError as exc:
            raise StorageRequestError("Nao foi possivel preparar o envio do anexo.") from exc
        data = self._json_or_raise(response, "Nao foi possivel preparar o envio do anexo.")
        relative_url = data.get("url")
        if not isinstance(relative_url, str) or not relative_url:
            raise StorageRequestError("O Storage nao devolveu uma URL de upload valida.")
        return SignedUpload(path=object_path, signed_url=urljoin(f"{self.storage_url}/", relative_url.lstrip("/")))

    async def get_object_metadata(self, object_path: str) -> ObjectMetadata | None:
        encoded_path = quote(f"{self.bucket}/{object_path}", safe="/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.head(
                    f"{self.storage_url}/object/{encoded_path}",
                    headers=self.headers,
                )
        except httpx.HTTPError as exc:
            raise StorageRequestError("Nao foi possivel validar o anexo enviado.") from exc
        if response.status_code == 404:
            return None
        if response.is_error:
            raise StorageRequestError("Nao foi possivel validar o anexo enviado.")
        content_length = response.headers.get("content-length")
        return ObjectMetadata(
            size_bytes=int(content_length) if content_length and content_length.isdigit() else None,
            content_type=response.headers.get("content-type"),
        )

    async def create_download_url(self, object_path: str, expires_in: int = 60) -> str:
        encoded_path = quote(f"{self.bucket}/{object_path}", safe="/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self.storage_url}/object/sign/{encoded_path}",
                    headers=self.headers,
                    json={"expiresIn": expires_in},
                )
        except httpx.HTTPError as exc:
            raise StorageRequestError("Nao foi possivel liberar o download do anexo.") from exc
        data = self._json_or_raise(response, "Nao foi possivel liberar o download do anexo.")
        relative_url = data.get("signedURL") or data.get("signedUrl")
        if not isinstance(relative_url, str) or not relative_url:
            raise StorageRequestError("O Storage nao devolveu uma URL de download valida.")
        return urljoin(f"{self.storage_url}/", relative_url.lstrip("/"))

    @staticmethod
    def _json_or_raise(response: httpx.Response, message: str) -> dict:
        if response.is_error:
            raise StorageRequestError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise StorageRequestError(message) from exc
        if not isinstance(data, dict):
            raise StorageRequestError(message)
        return data
