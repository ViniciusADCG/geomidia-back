import asyncio

from sqlalchemy import select

from app.api.routes.media_assets import log_activity
from app.db.models import MediaAsset
from app.db.session import SessionLocal, init_models
from app.domain.rules import get_required_radius
from app.schemas import ActivityType


SEED_ASSETS = [
    {
        "process_code": "PROC-2026-101",
        "media_type": "outdoor",
        "address": "Av. Afonso Pena, 2300",
        "district": "Centro",
        "latitude": -20.4612,
        "longitude": -54.6145,
        "area_m2": 27,
        "width_m": 9,
        "bottom_height_m": 5,
        "status": "Aprovado",
        "justification": "Atende as distancias regulamentares da Avenida Afonso Pena.",
        "contact_name": "Lucia Pereira",
        "contact_email": "lucia@outdoormidia.com.br",
    },
    {
        "process_code": "PROC-2026-102",
        "media_type": "painel de led",
        "address": "Av. Consul Assaf Trad, 1200",
        "district": "Coronel Antonino",
        "latitude": -20.395,
        "longitude": -54.589,
        "area_m2": 45,
        "bottom_height_m": 7,
        "status": "Aprovado",
        "justification": "Painel de LED de grande porte aprovado em via de trafego rapido.",
        "contact_name": "Fernando Silva",
        "contact_email": "fernando@ledtech.com.br",
    },
    {
        "process_code": "PROC-2026-103",
        "media_type": "front light",
        "address": "Rua Ceara, 1500",
        "district": "Santa Fe",
        "latitude": -20.448,
        "longitude": -54.592,
        "area_m2": 36,
        "bottom_height_m": 6,
        "top_height_m": 12,
        "status": "Pendente",
        "contact_name": "Roberto Santos",
        "contact_email": "roberto@propaganda.com.br",
    },
    {
        "process_code": "PROC-2026-104",
        "media_type": "empena de led",
        "address": "Av. Mato Grosso, 3200",
        "district": "Coophafe",
        "latitude": -20.432,
        "longitude": -54.601,
        "area_m2": 120,
        "bottom_height_m": 15,
        "status": "Pendente",
        "contact_name": "Amanda Costa",
        "contact_email": "amanda@empenasled.com.br",
    },
    {
        "process_code": "PROC-2026-105",
        "media_type": "triface",
        "address": "Av. Duque de Caxias, 800",
        "district": "Vila Alba",
        "latitude": -20.468,
        "longitude": -54.642,
        "area_m2": 32,
        "bottom_height_m": 6,
        "top_height_m": 11,
        "status": "Reprovado",
        "justification": "Divergencia: raio de protecao municipal insuficiente.",
        "contact_name": "Marcos Oliveira",
        "contact_email": "marcos@signcomunicacao.com.br",
    },
]


async def seed() -> None:
    await init_models()
    async with SessionLocal() as session:
        existing = await session.scalar(select(MediaAsset).limit(1))
        if existing:
            return

        for item in SEED_ASSETS:
            asset = MediaAsset(
                **item,
                radius_meters=get_required_radius(item["media_type"], item["area_m2"]),
            )
            session.add(asset)
            await session.flush()
            session.add(
                log_activity(
                    asset,
                    ActivityType.cadastro,
                    f"Carga inicial do processo {asset.process_code}.",
                )
            )

        await session.commit()


if __name__ == "__main__":
    asyncio.run(seed())
