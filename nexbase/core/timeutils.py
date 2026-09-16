"""Time utilities for coercion of source dates into datetimes."""
from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd


def coerce_datetime(value) -> datetime | None:
    """Best-effort conversion of common date representations to datetime.

    Never invents a value; returns None when it cannot be parsed.
    """
    if value is None:
        return None
    # pandas NaT is an instance of datetime and is truthy, so without this it
    # is returned as if it were a real date and raises on the first strftime.
    # A live LinkedIn page with an undated posting crashed the whole run here.
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return pd.to_datetime(stripped, errors="coerce").to_pydatetime()
        except Exception:
            return None
    try:
        return pd.to_datetime(value, errors="coerce").to_pydatetime()
    except Exception:
        return None