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
