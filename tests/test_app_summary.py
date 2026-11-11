"""STREAM day summary: request signing, row parsing and the client call."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ecoflow_energy.ecoflow.app_api import AppApiClient, SummaryLoginError
from ecoflow_energy.ecoflow.app_summary import (
    ENERGY_VALUE_PAGE,
    SUMMARY_PATH,
    day_summary_body,
    encode_body,
    parse_day_summary,
    signature_headers,
)

SERIAL = "ES21TEST0001"


def _response(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _client():
    session = MagicMock()
    client = AppApiClient(session=session, email="test@example.com", password="pw")
    client._token = "tok"
    return client, session


def test_body_is_compact_and_ordered_like_the_app():
    body = day_summary_body(SERIAL, "2026-11-11", "Europe/Amsterdam")
    assert encode_body(body) == (
        '{"code":"ES002-APP-summary-ALL-DATA-DAY-MASTER_DATA",'
        '"params":{"beginTime":"2026-11-11 00:00:00",'
        '"endTime":"2026-11-11 23:59:59","sn":"ES21TEST0001",'
        '"timezone":"Europe/Amsterdam"},"lang":"en_US"}'
    )


def test_signature_follows_the_app_recipe():
    body = {"code": "x", "params": {"b": 1, "a": 2}, "lang": "en_US"}
    data = encode_body(body)
    headers = signature_headers(
        "post", "/p", body["params"], data, timestamp_ms=1700000000000, nonce="00000042"
    )
    message = (
        "POST/p"
        + "a=2&b=1"
        + hashlib.sha256(data.encode()).hexdigest().upper()
        + "1700000000000"
        + "00000042"
    )
    expected = (
        hmac.new(b"wgvcJFbtdao4Qywe", message.encode(), hashlib.sha256)
        .hexdigest()
        .upper()
    )
    assert headers["x-sign"] == expected
    assert headers["x-timestamp"] == "1700000000000"
    assert headers["x-nonce"] == "00000042"
    assert headers["systemName"] == "app_rn"
    assert headers["platform"] == "web-app_rn"


def test_signature_without_body_or_params_and_random_nonce():
    headers = signature_headers("GET", "/p", None, "", timestamp_ms=1)
    message = "GET/p" + "null" + "1" + headers["x-nonce"]
    assert len(headers["x-nonce"]) == 8 and headers["x-nonce"].isdigit()
    assert headers["x-sign"] == (
        hmac.new(b"wgvcJFbtdao4Qywe", message.encode(), hashlib.sha256)
        .hexdigest()
        .upper()
    )


def test_parse_keeps_numeric_rows_and_skips_the_rest():
    rows = [
        {
            "skipPage": "SOLARVALUE",
            "indexName": "Energy value",
            "indexValue": "-1.66",
            "unit": "€",
        },
        {"skipPage": "saving", "indexValue": 3, "unit": None},
        {"skipPage": "", "indexValue": 1},
        {"skipPage": "BROKEN", "indexValue": "n/a"},
        {"skipPage": "NAN", "indexValue": "nan"},
        "junk",
    ]
    parsed = parse_day_summary(rows)
    assert parsed == {
        "SOLARVALUE": {"value": -1.66, "unit": "€", "name": "Energy value"},
        "SAVING": {"value": 3.0, "unit": "", "name": ""},
    }
    assert ENERGY_VALUE_PAGE in parsed
    assert parse_day_summary(None) == {}
    assert parse_day_summary({"a": 1}) == {}


async def test_client_sends_signed_request_and_parses_rows():
    client, session = _client()
    session.post.return_value = _response(
        {"code": "0", "data": [{"skipPage": "SOLARVALUE", "indexValue": "1.4"}]}
    )
    rows = await client.get_stream_day_summary(SERIAL, "2026-11-11", "Europe/Amsterdam")
    assert rows["SOLARVALUE"]["value"] == 1.4
    (url,), kwargs = session.post.call_args
    assert url.endswith(SUMMARY_PATH)
    sent = kwargs["data"].decode()
    assert json.loads(sent)["params"]["sn"] == SERIAL
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    # The signature must cover the exact bytes that were sent.
    again = signature_headers(
        "POST",
        SUMMARY_PATH,
        json.loads(sent)["params"],
        sent,
        timestamp_ms=int(headers["x-timestamp"]),
        nonce=headers["x-nonce"],
    )
    assert headers["x-sign"] == again["x-sign"]


async def test_client_logs_in_again_once_after_a_refusal():
    client, session = _client()
    session.post.side_effect = [
        _response({}, status=401),
        _response({"code": "0", "data": []}),
    ]

    async def relogin():
        client._token = "fresh"
        return True

    with patch.object(client, "login", side_effect=relogin) as login:
        assert await client.get_stream_day_summary(SERIAL, "2026-11-11", "UTC") == {}
    assert login.call_count == 1
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


async def test_client_gives_up_after_a_second_refusal():
    client, session = _client()
    session.post.side_effect = [
        _response({"code": "401"}),
        _response({}, status=403),
    ]
    with (
        patch.object(client, "login", new=AsyncMock(return_value=True)),
        pytest.raises(SummaryLoginError),
    ):
        await client.get_stream_day_summary(SERIAL, "2026-11-11", "UTC")
    assert client.token is None


async def test_client_raises_on_failed_login_and_on_rejection():
    client, session = _client()
    client._token = None
    with (
        patch.object(client, "login", new=AsyncMock(return_value=False)),
        pytest.raises(SummaryLoginError),
    ):
        await client.get_stream_day_summary(SERIAL, "2026-11-11", "UTC")
    session.post.assert_not_called()

    client._token = "tok"
    session.post.return_value = _response({"code": "6000", "message": "no"})
    with pytest.raises(ValueError, match="6000"):
        await client.get_stream_day_summary(SERIAL, "2026-11-11", "UTC")
