from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


MOSCOW_TZ = "Europe/Moscow"
_REGION_ALIASES = {
    "москва": "77",
    "moscow": "77",
    "санкт-петербург": "78",
    "санкт петербург": "78",
    "spb": "78",
    "saint petersburg": "78",
    "питер": "78",
    "московская область": "50",
    "mo": "50",
    "самарская область": "63",
}


class EisParameterError(ValueError):
    pass


def normalize_eis_region_code(value: object) -> str:
    raw = str(value).strip().lower()
    if raw in _REGION_ALIASES:
        return _REGION_ALIASES[raw]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 1:
        digits = f"0{digits}"
    if len(digits) == 2 and digits != "00":
        return digits
    if len(digits) >= 11 and digits[:2] != "00":
        return digits[:2]
    raise EisParameterError("orgRegion must be a valid 2-char KLADR region code")


def format_eis_exact_date(value: date | datetime | str, timezone: str | ZoneInfo | None = None) -> str:
    if timezone is None:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[+-]\d{2}:\d{2}", value.strip()):
            return value.strip()
        if isinstance(value, datetime) and value.tzinfo is not None:
            offset = value.strftime("%z")
            return f"{value.date().isoformat()}{offset[:3]}:{offset[3:]}"
        raise EisParameterError("exactDate requires an explicit timezone")

    tz = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(str(timezone))
    if isinstance(value, datetime):
        dt = value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
        day = dt.date()
        offset = dt.strftime("%z")
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=tz)
        day = value
        offset = dt.strftime("%z")
    else:
        raw = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[+-]\d{2}:\d{2}", raw):
            return raw
        try:
            day = date.fromisoformat(raw)
        except ValueError as exc:
            raise EisParameterError("exactDate must be YYYY-MM-DD or YYYY-MM-DD+HH:MM") from exc
        dt = datetime(day.year, day.month, day.day, tzinfo=tz)
        offset = dt.strftime("%z")
    return f"{day.isoformat()}{offset[:3]}:{offset[3:]}"


def format_eis_create_datetime(now: datetime | None = None) -> str:
    dt = now or datetime.now(UTC)
    if dt.tzinfo is None:
        raise EisParameterError("createDateTime must be timezone-aware")
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
