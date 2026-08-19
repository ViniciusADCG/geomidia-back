from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from typing import Any

REJECTED_STATUS = "irregular"


@dataclass(frozen=True)
class AssetForAnalysis:
    id: Any
    process_code: str
    media_type: str
    status: str
    latitude: float
    longitude: float
    area_m2: float
    radius_meters: int


def calculate_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_meters = 6_371_000
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return earth_radius_meters * c


def get_required_radius(media_type: str, area_m2: float) -> int:
    match media_type:
        case "painel de led":
            return 1000 if area_m2 > 5 else 250
        case "painel eletronico modular" | "empena de led":
            return 1000
        case "outdoor" | "front light" | "triface" | "empena":
            return 80
        case _:
            return 80


def evaluate_conflicts(
    candidate: AssetForAnalysis,
    assets: list[AssetForAnalysis],
) -> dict[str, Any]:
    # The persisted radius is calculated from the administrator-managed rule.
    candidate_radius = candidate.radius_meters
    conflicts: list[dict[str, Any]] = []

    for other in assets:
        if str(other.id) == str(candidate.id) or other.status == REJECTED_STATUS:
            continue

        distance = calculate_distance_meters(
            candidate.latitude,
            candidate.longitude,
            other.latitude,
            other.longitude,
        )

        if other.media_type == candidate.media_type:
            minimum_distance = max(candidate_radius, other.radius_meters)
            if distance < minimum_distance:
                conflicts.append({
                    "conflicting_asset_id": str(other.id),
                    "process_code": other.process_code,
                    "media_type": other.media_type,
                    "distance_meters": round(distance, 2),
                    "minimum_distance_meters": minimum_distance,
                })

        led_panel_pair = {
            candidate.media_type,
            other.media_type,
        } == {"painel de led", "empena de led"}

        if led_panel_pair and distance < 500:
            conflicts.append({
                "conflicting_asset_id": str(other.id),
                "process_code": other.process_code,
                "media_type": other.media_type,
                "distance_meters": round(distance, 2),
                "minimum_distance_meters": 500,
            })

    conflicts.sort(key=lambda item: item["distance_meters"])
    if conflicts:
        first = conflicts[0]
        return {
            "has_conflict": True,
            "message": (
                f"Divergencia com {len(conflicts)} processo(s). O mais proximo e "
                f"{first['process_code']} a {round(first['distance_meters'])}m; "
                f"minimo exigido: {first['minimum_distance_meters']}m."
            ),
            "conflicting_asset_id": first["conflicting_asset_id"],
            "distance_meters": first["distance_meters"],
            "minimum_distance_meters": first["minimum_distance_meters"],
            "conflicts": conflicts,
        }

    return {
        "has_conflict": False,
        "message": "Sem divergencias territoriais para os parametros informados.",
        "conflicting_asset_id": None,
        "distance_meters": None,
        "minimum_distance_meters": None,
        "conflicts": [],
    }
