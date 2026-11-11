"""Day summary of a STREAM system as the EcoFlow app shows it.

The app does not compute its "Energy value" amount: it asks the cloud for a
list of ready-made summary rows and prints them. This module builds the same
signed request and reads the answer. No Home Assistant dependency.

Request layout and signing were read from the app's own JavaScript (6.17.7);
they are not a documented interface and may change without notice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

SUMMARY_PATH = "/app/commonQuery/query"
STREAM_DAY_SUMMARY_CODE = "ES002-APP-summary-ALL-DATA-DAY-MASTER_DATA"
# The row the app labels "Energy value".
ENERGY_VALUE_PAGE = "SOLARVALUE"

_SIGN_SALT = b"wgvcJFbtdao4Qywe"
_SIGN_SYSTEM = "app_rn"
_SIGN_PLATFORM = "web-app_rn"
_SIGN_VERSION = "6.17.7"


def encode_body(body: dict[str, Any]) -> str:
    """Serialize exactly once: the signature covers these bytes."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def signature_headers(
    method: str,
    path: str,
    params: dict[str, Any] | None,
    data: str,
    *,
    timestamp_ms: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return the app's signature headers for one request.

    message = METHOD + path + sorted "k=v&..." of ``params`` + upper-case
    SHA-256 of the body (or the text ``null`` without one) + timestamp + nonce,
    signed with HMAC-SHA256 and sent as upper-case hex.
    """
    body_hash = hashlib.sha256(data.encode()).hexdigest().upper() if data else "null"
    query = "&".join(f"{key}={params[key]}" for key in sorted(params)) if params else ""
    stamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    once = nonce if nonce is not None else f"{secrets.randbelow(10**8):08d}"
    message = f"{method.upper()}{path}{query}{body_hash}{stamp}{once}"
    sign = hmac.new(_SIGN_SALT, message.encode(), hashlib.sha256).hexdigest().upper()
    return {
        "x-timestamp": stamp,
        "x-nonce": once,
        "systemName": _SIGN_SYSTEM,
        "platform": _SIGN_PLATFORM,
        "version": _SIGN_VERSION,
        "x-sign": sign,
    }


def day_summary_body(serial: str, day: str, timezone: str) -> dict[str, Any]:
    """Request body for one local calendar day (``day`` as YYYY-MM-DD)."""
    return {
        "code": STREAM_DAY_SUMMARY_CODE,
        "params": {
            "beginTime": f"{day} 00:00:00",
            "endTime": f"{day} 23:59:59",
            "sn": serial,
            "timezone": timezone,
        },
        "lang": "en_US",
    }


def parse_day_summary(rows: Any) -> dict[str, dict[str, Any]]:
    """Map each summary row to ``{value, unit, name}`` by its page key.

    Rows without a page key or a numeric value are skipped, so one odd row
    cannot hide the others.
    """
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        page = row.get("skipPage")
        if not isinstance(page, str) or not page:
            continue
        try:
            value = float(row.get("indexValue"))
        except (TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        unit = row.get("unit")
        name = row.get("indexName")
        result[page.upper()] = {
            "value": value,
            "unit": unit if isinstance(unit, str) else "",
            "name": name if isinstance(name, str) else "",
        }
    return result


# Rows that can be non-zero on a day the system recorded nothing: a tariff and
# a forecast exist without any measurement.
_NOT_MEASURED = frozenset({"ELECTRICITY_PRICE", "SOLAR_PREDICT"})


def day_is_empty(rows: dict[str, dict[str, Any]]) -> bool:
    """True when a day holds no measured row at all or nothing but zeros."""
    return all(
        row["value"] == 0 for page, row in rows.items() if page not in _NOT_MEASURED
    )


def month_totals(
    days: dict[str, float], today: str | None, today_value: float | None
) -> dict[str, float]:
    """Sum per calendar month (YYYY-MM), to compare with the app's calendar."""
    totals: dict[str, float] = {}
    for day, value in days.items():
        if day != today:
            totals[day[:7]] = totals.get(day[:7], 0.0) + value
    if today is not None and today_value is not None:
        totals[today[:7]] = totals.get(today[:7], 0.0) + today_value
    return {month: round(totals[month], 2) for month in sorted(totals)}


def energy_value_total(days: dict[str, float], today: str, today_value: float) -> float:
    """Finished days plus today; today is never counted from the stored days."""
    return sum(value for day, value in days.items() if day != today) + today_value


def valid_saved_days(saved: Any) -> bool:
    """Reject a damaged store instead of publishing a wrong total from it."""
    if not isinstance(saved, dict):
        return False
    days, provisional = saved.get("days"), saved.get("provisional")
    seen = saved.get("last_seen")

    def amount(value: Any) -> bool:
        return (
            type(value) in (int, float)
            and value == value
            and abs(value) != float("inf")
        )

    return (
        isinstance(days, dict)
        and all(isinstance(day, str) and amount(value) for day, value in days.items())
        and (
            seen is None
            or (
                isinstance(seen, dict)
                and set(seen) == {"day", "value"}
                and isinstance(seen["day"], str)
                and amount(seen["value"])
            )
        )
        and isinstance(provisional, list)
        and all(isinstance(day, str) for day in provisional)
        and type(saved.get("complete")) is bool
    )
