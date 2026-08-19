import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role in ('admin', 'analyst', 'viewer')", name="ck_users_role"),
    )


class ProcessCounter(Base):
    __tablename__ = "process_counters"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class MediaRule(Base):
    __tablename__ = "media_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_type: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_radius_meters: Mapped[int] = mapped_column(Integer, nullable=False)
    area_threshold_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_above_threshold_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "media_type in ('outdoor', 'front light', 'triface', 'painel de led', "
            "'painel eletronico modular', 'empena', 'empena de led')",
            name="ck_media_rules_media_type",
        ),
        CheckConstraint("base_radius_meters > 0", name="ck_media_rules_base_radius"),
        CheckConstraint("area_threshold_m2 is null or area_threshold_m2 > 0", name="ck_media_rules_area_threshold"),
        CheckConstraint(
            "radius_above_threshold_meters is null or radius_above_threshold_meters > 0",
            name="ck_media_rules_threshold_radius",
        ),
        CheckConstraint(
            "(area_threshold_m2 is null) = (radius_above_threshold_meters is null)",
            name="ck_media_rules_threshold_pair",
        ),
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    process_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    district: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)", persisted=True),
        nullable=False,
    )
    area_m2: Mapped[float] = mapped_column(Float, nullable=False)
    width_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    bottom_height_m: Mapped[float] = mapped_column(Float, nullable=False)
    top_height_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_meters: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="análise")
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_links: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    activities: Mapped[list["ActivityLog"]] = relationship(back_populates="asset", passive_deletes=True)
    application_form: Mapped["ApplicationForm | None"] = relationship(
        back_populates="asset",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        CheckConstraint(
            "media_type in ('outdoor', 'front light', 'triface', 'painel de led', "
            "'painel eletronico modular', 'empena', 'empena de led')",
            name="ck_media_assets_media_type",
        ),
        CheckConstraint(
            "status in ('aprovado', 'irregular', 'análise', 'exigência', 'vencido', 'cartografia', 'jurídico', 'vistoria')",
            name="ck_media_assets_status",
        ),
        CheckConstraint("latitude between -90 and 90", name="ck_media_assets_latitude"),
        CheckConstraint("longitude between -180 and 180", name="ck_media_assets_longitude"),
        CheckConstraint("area_m2 > 0", name="ck_media_assets_area"),
        CheckConstraint("width_m is null or width_m > 0", name="ck_media_assets_width"),
        CheckConstraint("bottom_height_m >= 0", name="ck_media_assets_bottom_height"),
        CheckConstraint("top_height_m is null or top_height_m >= bottom_height_m", name="ck_media_assets_height_order"),
        Index("ix_media_assets_geom", "geom", postgresql_using="gist"),
    )


class ApplicationForm(Base):
    __tablename__ = "application_forms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    company_responsible: Mapped[str] = mapped_column(String(120), nullable=False)
    municipal_registration: Mapped[str] = mapped_column(String(60), nullable=False)
    property_registration: Mapped[str] = mapped_column(String(60), nullable=False)
    street: Mapped[str] = mapped_column(String(180), nullable=False)
    number: Mapped[str] = mapped_column(String(30), nullable=False)
    district: Mapped[str] = mapped_column(String(120), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(9), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    area_m2: Mapped[float] = mapped_column(Float, nullable=False)
    bottom_height_m: Mapped[float] = mapped_column(Float, nullable=False)
    requester_email: Mapped[str] = mapped_column(String(160), nullable=False)
    attachment_links: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now()
    )

    asset: Mapped[MediaAsset] = relationship(back_populates="application_form", lazy="joined")

    @property
    def process_code(self) -> str:
        return self.asset.process_code

    __table_args__ = (
        CheckConstraint(
            "media_type in ('outdoor', 'front light', 'triface', 'painel de led', "
            "'painel eletronico modular', 'empena', 'empena de led')",
            name="ck_application_forms_media_type",
        ),
        CheckConstraint("latitude between -90 and 90", name="ck_application_forms_latitude"),
        CheckConstraint("longitude between -180 and 180", name="ck_application_forms_longitude"),
        CheckConstraint("area_m2 > 0", name="ck_application_forms_area"),
        CheckConstraint("bottom_height_m >= 0", name="ck_application_forms_bottom_height"),
    )


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    process_code: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())

    asset: Mapped[MediaAsset | None] = relationship(back_populates="activities")

    __table_args__ = (
        CheckConstraint(
            "activity_type in ('cadastro', 'aprovacao', 'reprovacao', 'edicao', 'remocao')",
            name="ck_activity_logs_type",
        ),
    )
