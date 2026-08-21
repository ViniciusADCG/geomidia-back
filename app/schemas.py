from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.config import get_settings


class MediaType(str, Enum):
    outdoor = "outdoor"
    front_light = "front light"
    triface = "triface"
    painel_de_led = "painel de led"
    painel_eletronico_modular = "painel eletronico modular"
    empena = "empena"
    empena_de_led = "empena de led"


class MediaStatus(str, Enum):
    new_process = "novos processos"
    approved = "aprovado"
    irregular = "irregular"
    analysis = "análise"
    exigency = "exigência"
    expired = "vencido"
    cartography = "cartografia"
    legal = "jurídico"
    inspection = "vistoria"


class ActivityType(str, Enum):
    cadastro = "cadastro"
    aprovacao = "aprovacao"
    reprovacao = "reprovacao"
    edicao = "edicao"
    remocao = "remocao"


class UserRole(str, Enum):
    admin = "admin"
    analyst = "analyst"
    viewer = "viewer"


DISTRICT_ALIASES = {
    "santa fe": "Santa Fé",
    "santa fé": "Santa Fé",
    "coophafe": "Coophafé",
    "coophafé": "Coophafé",
}


class MediaAssetBase(BaseModel):
    media_type: MediaType
    address: str = Field(min_length=3, max_length=255)
    district: str = Field(min_length=2, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    area_m2: float = Field(gt=0)
    width_m: float | None = Field(default=None, gt=0)
    bottom_height_m: float = Field(ge=0)
    top_height_m: float | None = Field(default=None, ge=0)
    status: MediaStatus = MediaStatus.new_process
    justification: str | None = None
    attachment_links: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None

    @field_validator("address", "district", "contact_name", "attachment_links", mode="before")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("district")
    @classmethod
    def normalize_district(cls, value: str) -> str:
        return DISTRICT_ALIASES.get(value.casefold(), value)

    @model_validator(mode="after")
    def validate_measurements_and_location(self) -> "MediaAssetBase":
        if self.top_height_m is not None and self.top_height_m < self.bottom_height_m:
            raise ValueError("A borda superior deve ser maior ou igual a borda inferior.")
        settings = get_settings()
        if not (settings.city_min_latitude <= self.latitude <= settings.city_max_latitude):
            raise ValueError("Latitude fora da area operacional de Campo Grande.")
        if not (settings.city_min_longitude <= self.longitude <= settings.city_max_longitude):
            raise ValueError("Longitude fora da area operacional de Campo Grande.")
        return self


class MediaAssetCreate(MediaAssetBase):
    @model_validator(mode="after")
    def must_start_as_new_process(self) -> "MediaAssetCreate":
        if self.status != MediaStatus.new_process:
            raise ValueError("Novos processos devem iniciar como Novos Processos.")
        return self


class MediaAssetUpdate(BaseModel):
    media_type: MediaType | None = None
    address: str | None = Field(default=None, min_length=3, max_length=255)
    district: str | None = Field(default=None, min_length=2, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    area_m2: float | None = Field(default=None, gt=0)
    width_m: float | None = Field(default=None, gt=0)
    bottom_height_m: float | None = Field(default=None, ge=0)
    top_height_m: float | None = Field(default=None, ge=0)
    status: MediaStatus | None = None
    justification: str | None = None
    attachment_links: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None


class MediaAssetRead(MediaAssetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    process_code: str
    radius_meters: int
    created_at: datetime
    updated_at: datetime


class MediaAssetPage(BaseModel):
    items: list[MediaAssetRead]
    total: int
    limit: int
    offset: int


class ApplicationFormBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_responsible: str = Field(min_length=2, max_length=120)
    municipal_registration: str = Field(min_length=1, max_length=60)
    property_registration: str = Field(min_length=1, max_length=60)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    street: str = Field(min_length=2, max_length=180)
    number: str = Field(min_length=1, max_length=30)
    district: str = Field(min_length=2, max_length=120)
    postal_code: str = Field(pattern=r"^\d{5}-?\d{3}$")
    media_type: MediaType
    area_m2: float = Field(gt=0)
    bottom_height_m: float = Field(ge=0)
    requester_email: EmailStr
    attachment_links: str | None = None

    @field_validator(
        "company_responsible",
        "municipal_registration",
        "property_registration",
        "street",
        "number",
        "district",
        "postal_code",
        "attachment_links",
        mode="before",
    )
    @classmethod
    def strip_form_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("district")
    @classmethod
    def normalize_form_district(cls, value: str) -> str:
        return DISTRICT_ALIASES.get(value.casefold(), value)

    @model_validator(mode="after")
    def validate_form_location(self) -> "ApplicationFormBase":
        settings = get_settings()
        if not (settings.city_min_latitude <= self.latitude <= settings.city_max_latitude):
            raise ValueError("Latitude fora da area operacional de Campo Grande.")
        if not (settings.city_min_longitude <= self.longitude <= settings.city_max_longitude):
            raise ValueError("Longitude fora da area operacional de Campo Grande.")
        return self


class ApplicationFormCreate(ApplicationFormBase):
    pass


class ApplicationFormUpdate(BaseModel):
    company_responsible: str | None = Field(default=None, min_length=2, max_length=120)
    municipal_registration: str | None = Field(default=None, min_length=1, max_length=60)
    property_registration: str | None = Field(default=None, min_length=1, max_length=60)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    street: str | None = Field(default=None, min_length=2, max_length=180)
    number: str | None = Field(default=None, min_length=1, max_length=30)
    district: str | None = Field(default=None, min_length=2, max_length=120)
    postal_code: str | None = Field(default=None, pattern=r"^\d{5}-?\d{3}$")
    media_type: MediaType | None = None
    area_m2: float | None = Field(default=None, gt=0)
    bottom_height_m: float | None = Field(default=None, ge=0)
    requester_email: EmailStr | None = None
    attachment_links: str | None = None


class ApplicationFormRead(ApplicationFormBase):
    id: UUID
    asset_id: UUID
    process_code: str
    status: MediaStatus
    created_at: datetime
    updated_at: datetime


class ActivityLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID | None
    actor_user_id: UUID | None
    process_code: str
    activity_type: ActivityType
    message: str
    changes: dict | None
    request_id: str | None
    created_at: datetime


class ActivityLogPage(BaseModel):
    items: list[ActivityLogRead]
    total: int
    limit: int
    offset: int


class ConflictItemRead(BaseModel):
    conflicting_asset_id: str
    process_code: str
    media_type: str
    distance_meters: float
    minimum_distance_meters: int


class ConflictAnalysisRead(BaseModel):
    has_conflict: bool
    message: str
    conflicting_asset_id: str | None = None
    distance_meters: float | None = None
    minimum_distance_meters: int | None = None
    conflicts: list[ConflictItemRead] = Field(default_factory=list)


class MediaStatsRead(BaseModel):
    total: int
    new_processes: int
    pending: int
    approved: int
    rejected: int
    by_type: dict[str, int]


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_name: str
    user_id: UUID
    role: UserRole
    expires_at: datetime


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    full_name: str
    email: EmailStr | None
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9._-]+$")
    full_name: str = Field(min_length=3, max_length=160)
    email: EmailStr | None = None
    password: str = Field(min_length=12, max_length=128)
    role: UserRole = UserRole.viewer

    @field_validator("username")
    @classmethod
    def normalize_new_username(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=3, max_length=160)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)
    role: UserRole | None = None
    is_active: bool | None = None


class MediaRuleBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    media_type: MediaType
    name: str = Field(min_length=3, max_length=120)
    base_radius_meters: int = Field(gt=0, le=100_000)
    area_threshold_m2: float | None = Field(default=None, gt=0)
    radius_above_threshold_meters: int | None = Field(default=None, gt=0, le=100_000)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool = True

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_rule_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_threshold_pair(self) -> "MediaRuleBase":
        if (self.area_threshold_m2 is None) != (self.radius_above_threshold_meters is None):
            raise ValueError("Limite de area e raio acima do limite devem ser informados juntos.")
        return self


class MediaRuleCreate(MediaRuleBase):
    pass


class MediaRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    base_radius_meters: int | None = Field(default=None, gt=0, le=100_000)
    area_threshold_m2: float | None = Field(default=None, gt=0)
    radius_above_threshold_meters: int | None = Field(default=None, gt=0, le=100_000)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None


class MediaRuleRead(MediaRuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
