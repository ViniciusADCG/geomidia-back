"""Annual public protocol formatting in Campo Grande local time."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

CAMPO_GRANDE_TIMEZONE = ZoneInfo("America/Campo_Grande")


def protocol_year(instant: datetime | None = None) -> int:
    instant = instant or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("O horário do protocolo deve incluir fuso horário.")
    return instant.astimezone(CAMPO_GRANDE_TIMEZONE).year


def format_protocol(prefix: str, number: int, year: int) -> str:
    if number < 1:
        raise ValueError("O número do protocolo deve ser positivo.")
    return f"{prefix}-{number:02d}-{year}"
