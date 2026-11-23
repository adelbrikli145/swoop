"""Fail-closed Economy availability check for Air Algérie AH6354."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from swoop import BookingOption, Passengers, PriceResult, TransportConfig, check_price

ALLOWED_DATES = ("2026-12-23", "2026-12-27")
_ECONOMY = re.compile(r"\b(?:ECONOMY|ECONOMIE|ECONOMIQUE|ECO)\b")
_EXCLUDED_BRAND = re.compile(r"\b(?:BUSINESS|FIRST|PREMIUM|PLUS)\b")


def _is_google_booking_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "www.google.com"
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and parsed.path == "/travel/clk/f"
        and bool(parsed.query)
        and not parsed.fragment
    )


def qualifying_economy_offer(
    result: Optional[PriceResult],
    travel_date: str,
) -> Optional[BookingOption]:
    """Return a strictly verified Economy booking option, otherwise None."""
    if (
        travel_date not in ALLOWED_DATES
        or result is None
        or result.is_estimate
        or result.is_basic_economy
        or result.price <= 0
        or not result.currency
        or len(result.resolved_legs) != 1
    ):
        return None

    leg = result.resolved_legs[0]
    itinerary = result.itinerary
    if (
        itinerary is None
        or leg.origin != "CZL"
        or leg.destination != "DJG"
        or leg.date != travel_date
        or len(itinerary.segments) != 1
    ):
        return None

    segment = itinerary.segments[0]
    if (
        segment.airline.upper() != "AH"
        or str(segment.flight_number).strip() != "6354"
        or segment.departure_airport_code.upper() != "CZL"
        or segment.arrival_airport_code.upper() != "DJG"
        or segment.departure_date != tuple(int(part) for part in travel_date.split("-"))
    ):
        return None

    eligible: list[BookingOption] = []
    for option in result.booking_options:
        brand = unicodedata.normalize(
            "NFKD", f"{option.brand_label} {option.brand_code}"
        ).encode("ascii", "ignore").decode("ascii").upper()
        if (
            option.price == result.price
            and option.price > 0
            and option._cabin_bucket == "economy"
            and not option.is_basic
            and _ECONOMY.search(brand)
            and not _EXCLUDED_BRAND.search(brand)
            and _is_google_booking_url(option.booking_url)
        ):
            eligible.append(option)

    return min(eligible, key=lambda option: option.price) if eligible else None


def _send_telegram(message: str, *, token: str, chat_id: str) -> None:
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Telegram send failed ({type(exc).__name__}); no alert was recorded."
        ) from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram rejected the message; no alert was recorded.")


def run_monitor(travel_date: str, state_dir: Path) -> bool:
    if travel_date not in ALLOWED_DATES:
        raise ValueError(f"unsupported travel date: {travel_date}")

    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{travel_date}.sent"
    if state_file.exists():
        print(f"{travel_date}: notification already sent; skipping.")
        return False

    result = check_price(
        "AH6354",
        origin="CZL",
        destination="DJG",
        date=travel_date,
        cabin="economy",
        passengers=Passengers(adults=1),
        transport=TransportConfig(retries=0, impersonate=""),
    )
    if result is None:
        print(f"{travel_date}: no matching flight; no alert sent.")
        return False

    option = qualifying_economy_offer(result, travel_date)
    if option is None or result.currency is None:
        print(f"{travel_date}: no confirmed, bookable Economy offer; no alert sent.")
        return False

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "A confirmed offer exists, but TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID are required to send it."
        )

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = (
        "عرض Economy مؤكد وقابل للحجز — Air Algérie\n"
        f"CZL → DJG | AH6354 | {travel_date}\n"
        f"شخص بالغ واحد | السعر: {option.price} {result.currency}\n"
        f"وقت الفحص (UTC): {checked_at}\n"
        f"الحجز: {option.booking_url}"
    )
    _send_telegram(message, token=token, chat_id=chat_id)
    state_file.write_text("sent\n", encoding="utf-8")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("sent=true\n")
    print(f"{travel_date}: confirmed Economy notification sent.")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, choices=ALLOWED_DATES)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    run_monitor(args.date, args.state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
