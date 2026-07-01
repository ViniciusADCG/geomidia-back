from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class MediaType(str, Enum):
    outdoor = "outdoor"
    front_light = "front light"
    triface = "triface"
    painel_de_led = "painel de led"
    empena = "empena"
    empena_de_led = "empena de led"


class MediaStatus(str, Enum):
    approved = "Aprovado"
    rejected = "Reprovado"
    pending = "Pendente"


class ActivityType(str, Enum):
    cadastro = "cadastro"
    aprovacao = "aprovacao"
    reprovacao = "reprovacao"
    edicao = "edicao"
    remocao = "remocao"


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
    status: MediaStatus = MediaStatus.pending
    justification: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None


class MediaAssetCreate(MediaAssetBase):
    pass


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
    contact_name: str | None = Field(default=None, max_length=120)
    contact_email: EmailStr | None = None


class MediaAssetRead(MediaAssetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    process_code: str
    radius_meters: int
    created_at: datetime
    updated_at: datetime


class ActivityLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    asset_id: UUID | None
    process_code: str
    activity_type: ActivityType
    message: str
    created_at: datetime


class ConflictAnalysisRead(BaseModel):
    has_conflict: bool
    message: str
    conflicting_asset_id: str | None = None
    distance_meters: float | None = None
    minimum_distance_meters: int | None = None


class MediaStatsRead(BaseModel):
    total: int
    pending: int
    approved: int
    rejected: int
    by_type: dict[str, int]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_email: EmailStr
