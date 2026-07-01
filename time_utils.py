from datetime import datetime, timezone
from zoneinfo import ZoneInfo


LOCAL_TZ_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return utc_now().astimezone(LOCAL_TZ)


def local_today() -> str:
    return local_now().strftime("%Y-%m-%d")


def to_local_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def local_date_key(value: str) -> str:
    dt = to_local_dt(value)
    return dt.strftime("%Y-%m-%d") if dt else ""
