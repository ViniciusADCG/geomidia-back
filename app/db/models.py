import uuid
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, Computed, DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


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
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="Pendente")
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    __table_args__ = (
        CheckConstraint(
            "media_type in ('outdoor', 'front light', 'triface', 'painel de led', 'empena', 'empena de led')",
            name="ck_media_assets_media_type",
        ),
        CheckConstraint("status in ('Aprovado', 'Reprovado', 'Pendente')", name="ck_media_assets_status"),
        CheckConstraint("latitude between -90 and 90", name="ck_media_assets_latitude"),
        CheckConstraint("longitude between -180 and 180", name="ck_media_assets_longitude"),
        CheckConstraint("area_m2 > 0", name="ck_media_assets_area"),
        CheckConstraint("bottom_height_m >= 0", name="ck_media_assets_bottom_height"),
        Index("ix_media_assets_geom", "geom", postgresql_using="gist"),
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
    process_code: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, server_default=func.now())

    asset: Mapped[MediaAsset | None] = relationship(back_populates="activities")

    __table_args__ = (
        CheckConstraint(
            "activity_type in ('cadastro', 'aprovacao', 'reprovacao', 'edicao', 'remocao')",
            name="ck_activity_logs_type",
        ),
    )
