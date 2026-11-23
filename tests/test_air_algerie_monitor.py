from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.monitor_air_algerie import (
    qualifying_economy_offer,
    run_monitor,
)
from swoop.decoder import BookingOption, Itinerary, Segment
from swoop.exceptions import SwoopUpstreamError
from swoop.models import PriceResult, ResolvedLeg

BOOKING_URL = "https://www.google.com/travel/clk/f?u=https%3A%2F%2Fexample.com"


def make_result(
    *,
    date: str = "2026-12-27",
    flight_number: str = "6354",
    airline: str = "AH",
    origin: str = "CZL",
    destination: str = "DJG",
    cabin: str = "economy",
    brand: str = "Economy Classic",
    booking_url: str = BOOKING_URL,
    estimate: bool = False,
    basic: bool = False,
    resolved_legs: int = 1,
    segments: int = 1,
    option_price: int = 8500,
) -> PriceResult:
    year, month, day = (int(part) for part in date.split("-"))
    itinerary = Itinerary(
        segments=[
            Segment(
                airline=airline,
                flight_number=flight_number,
                departure_airport_code=origin,
                arrival_airport_code=destination,
                departure_date=(year, month, day),
            )
            for _ in range(segments)
        ],
    )
    return PriceResult(
        price=option_price,
        currency="DZD",
        is_basic_economy=basic,
        is_estimate=estimate,
        booking_options=[
            BookingOption(
                price=option_price,
                brand_label=brand,
                is_basic=basic,
                booking_url=booking_url,
                _cabin_bucket=cabin,
            )
        ],
        itinerary=itinerary,
        resolved_legs=[
            ResolvedLeg(
                flight_summary="AH 6354",
                origin=origin,
                destination=destination,
                date=date,
                itinerary=itinerary,
            )
            for _ in range(resolved_legs)
        ],
    )


def test_accepts_only_confirmed_bookable_economy_offer():
    result = make_result()

    assert qualifying_economy_offer(result, "2026-12-27") is result.booking_options[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"estimate": True},
        {"cabin": ""},
        {"cabin": "business", "brand": "Business"},
        {"cabin": "first", "brand": "First Class"},
        {"brand": "First Plus"},
        {"brand": "Economy Plus"},
        {"brand": "Premium Economy"},
        {"brand": "Economy Basic", "basic": True},
        {"booking_url": ""},
        {"booking_url": "https://example.com/book"},
        {"option_price": 0},
        {"date": "2026-12-23"},
        {"flight_number": "9999"},
        {"airline": "ZZ"},
        {"origin": "ALG"},
        {"destination": "ALG"},
        {"resolved_legs": 2},
        {"segments": 2},
    ],
)
def test_rejects_unconfirmed_or_nonmatching_options(overrides):
    result = make_result(**overrides)

    assert qualifying_economy_offer(result, "2026-12-27") is None


def test_rejects_round_trip_context_and_unapproved_date():
    result = make_result()
    result.resolved_legs[0].date = "2026-12-23"

    assert qualifying_economy_offer(result, "2026-12-27") is None
    assert qualifying_economy_offer(make_result(), "2026-12-24") is None


def test_monitor_does_not_send_or_require_secrets_for_estimate(tmp_path: Path):
    with patch(
        "scripts.monitor_air_algerie.check_price",
        return_value=make_result(estimate=True),
    ), patch("scripts.monitor_air_algerie._send_telegram") as send:
        assert run_monitor("2026-12-27", tmp_path) is False

    send.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_monitor_does_not_send_after_upstream_error(tmp_path: Path):
    with patch(
        "scripts.monitor_air_algerie.check_price",
        side_effect=SwoopUpstreamError(13),
    ), patch("scripts.monitor_air_algerie._send_telegram") as send:
        with pytest.raises(SwoopUpstreamError):
            run_monitor("2026-12-27", tmp_path)

    send.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_monitor_sends_once_and_records_only_after_success(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    result = make_result()
    state_dir = tmp_path / "state"

    with patch("scripts.monitor_air_algerie.check_price", return_value=result), patch(
        "scripts.monitor_air_algerie._send_telegram"
    ) as send:
        assert run_monitor("2026-12-27", state_dir) is True
        assert run_monitor("2026-12-27", state_dir) is False

    send.assert_called_once()
    assert (state_dir / "2026-12-27.sent").read_text() == "sent\n"


def test_missing_telegram_secrets_prevent_send_and_state(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with patch("scripts.monitor_air_algerie.check_price", return_value=make_result()), patch(
        "scripts.monitor_air_algerie._send_telegram"
    ) as send:
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            run_monitor("2026-12-27", tmp_path)

    send.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_monitor_does_not_record_when_telegram_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    state_dir = tmp_path / "state"

    with patch("scripts.monitor_air_algerie.check_price", return_value=make_result()), patch(
        "scripts.monitor_air_algerie._send_telegram",
        side_effect=RuntimeError("Telegram unavailable"),
    ):
        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            run_monitor("2026-12-27", state_dir)

    assert not list(state_dir.iterdir())


def test_monitor_rejects_unsupported_date_without_search(tmp_path: Path):
    with patch("scripts.monitor_air_algerie.check_price") as check:
        with pytest.raises(ValueError, match="unsupported travel date"):
            run_monitor("2026-12-24", tmp_path)

    check.assert_not_called()
