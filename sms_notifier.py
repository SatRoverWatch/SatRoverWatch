#!/usr/bin/env python3
"""Standalone outbound SMS notifications for SatRoverWatch using Textbelt."""

import argparse
import json
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)
TEXTBELT_URL = "https://textbelt.com/text"
DEFAULT_MESSAGE = "SatRoverWatch SMS test. Your rover notification system is working."
BASE_DIR = Path(__file__).resolve().parent


class SMSConfigurationError(Exception):
    """Missing or invalid SMS configuration."""


def load_rover(callsign, config_path=None):
    """Find a rover by callsign in the private rover configuration."""
    path = Path(config_path) if config_path else BASE_DIR / "rover_config.json"
    try:
        with path.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SMSConfigurationError(f"Unable to load rover configuration: {type(exc).__name__}") from exc

    rovers = config.get("rovers")
    if not isinstance(rovers, list):
        raise SMSConfigurationError("Configuration must contain a rovers array")
    matches = [r for r in rovers if isinstance(r, dict) and str(r.get("callsign", "")).upper() == callsign.upper()]
    if len(matches) != 1:
        raise SMSConfigurationError("Expected exactly one rover with the requested callsign")
    return matches[0]


def send_sms(callsign, message, *, live=False, config_path=None, env_path=None, timeout=15):
    """Send or simulate one rover SMS. Returns sanitized Textbelt response details.

    The rover's main `enabled` setting is intentionally independent of SMS tests.
    """
    if not isinstance(message, str) or not message.strip():
        raise SMSConfigurationError("SMS message cannot be empty")
    rover = load_rover(callsign, config_path)
    sms = rover.get("sms", {})
    if not isinstance(sms, dict) or sms.get("enabled") is not True:
        raise SMSConfigurationError("SMS is not enabled for this rover")
    phone = str(sms.get("phone_number", "")).strip()
    digits = phone.lstrip("+")
    if not (digits.isdigit() and 10 <= len(digits) <= 15):
        raise SMSConfigurationError("Rover phone number is missing or invalid")

    load_dotenv(dotenv_path=env_path or BASE_DIR / ".env", override=False)
    key = os.getenv("TEXTBELT_API_KEY", "").strip()
    if not key:
        raise SMSConfigurationError("TEXTBELT_API_KEY is missing")
    api_key = key if live else f"{key}_test"

    try:
        response = requests.post(
            TEXTBELT_URL,
            data={"phone": phone, "message": message, "key": api_key},
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        LOGGER.warning("Textbelt request failed: %s", type(exc).__name__)
        return {"success": False, "error": "Network or invalid API response", "mode": "live" if live else "test"}

    if not isinstance(result, dict):
        return {"success": False, "error": "Invalid API response", "mode": "live" if live else "test"}
    # Only return known fields; never expose raw API responses or credentials.
    return {
        "success": result.get("success") is True,
        "mode": "live" if live else "test",
        "quotaRemaining": result.get("quotaRemaining"),
        "textId": result.get("textId"),
        "error": str(result.get("error", ""))[:200] if result.get("success") is not True else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Send a rover SMS via Textbelt (test mode by default)")
    parser.add_argument("callsign", help="Rover callsign in rover_config.json")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="SMS message content")
    parser.add_argument("--live", action="store_true", help="Send a real, billable SMS")
    args = parser.parse_args()
    try:
        result = send_sms(args.callsign, args.message, live=args.live)
    except SMSConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1
    print(f"Mode: {result['mode']}; success: {result['success']}")
    if result.get("quotaRemaining") is not None:
        print(f"Textbelt credits remaining: {result['quotaRemaining']}")
    if result.get("textId") is not None:
        print(f"Textbelt message ID: {result['textId']}")
    if result.get("error"):
        print(f"Error: {result['error']}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
