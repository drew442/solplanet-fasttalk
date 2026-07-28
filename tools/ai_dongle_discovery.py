#!/usr/bin/env python3
"""Collect a privacy-safe snapshot from the Ai dongle's read-only CGI."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import ssl
import sys
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request


TOOL_VERSION = "0.2"
ENDPOINT_PATH = "/paraget.cgi"
MAX_RESPONSE_BYTES = 1024 * 1024
METER_FIELDS = ("meter_en", "meter_add", "meter_mod")
SAFE_DEVICE_FIELDS = (
    "typ",
    "mod",
    "muf",
    "brd",
    "hw",
    "sw",
    "wsw",
    "status",
    "elink",
)
KNOWN_SENSITIVE_FIELDS = (
    "psn",
    "key",
    "nam",
    "pdk",
    "ser",
    "ali_ip",
    "ali_port",
)


class DongleDiscoveryError(Exception):
    """Expected URL, HTTP, or response-format failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def endpoint_url(base_url: str) -> str:
    candidate = base_url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme != "https":
        raise DongleDiscoveryError("the Ai dongle base URL must use https")
    if not parsed.hostname:
        raise DongleDiscoveryError("the base URL must include a host or IP address")
    if parsed.username is not None or parsed.password is not None:
        raise DongleDiscoveryError("credentials must not be embedded in the URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise DongleDiscoveryError(
            "supply only the dongle base URL, without a path, query, or fragment"
        )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, ENDPOINT_PATH, "", "")
    )


def fetch_parameters(base_url: str, timeout: float) -> dict[str, Any]:
    url = endpoint_url(base_url)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_context.check_hostname = False
    tls_context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": f"solplanet-fasttalk-ai-dongle-discovery/{TOOL_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=tls_context,
        ) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DongleDiscoveryError(f"GET {ENDPOINT_PATH} failed: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise DongleDiscoveryError(
            f"{ENDPOINT_PATH} exceeded the {MAX_RESPONSE_BYTES}-byte limit"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DongleDiscoveryError(
            f"{ENDPOINT_PATH} did not return valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DongleDiscoveryError(
            f"{ENDPOINT_PATH} returned {type(payload).__name__}, expected an object"
        )
    return payload


def make_safe_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    meter = {key: payload[key] for key in METER_FIELDS if key in payload}
    device = {
        key: payload[key]
        for key in SAFE_DEVICE_FIELDS
        if key in payload
    }
    present_sensitive = [
        key for key in KNOWN_SENSITIVE_FIELDS if key in payload
    ]
    return {
        "schema_version": 1,
        "tool": "solplanet-fasttalk-ai-dongle-discovery",
        "tool_version": TOOL_VERSION,
        "retrieved_at_utc": utc_now(),
        "source_endpoint": ENDPOINT_PATH,
        "http_method": "GET",
        "transport": {
            "scheme": "https",
            "certificate_verification": False,
            "scope": "this request only",
        },
        "meter_configuration": meter,
        "device_summary": device,
        "response_field_names": sorted(str(key) for key in payload),
        "privacy": {
            "raw_response_saved": False,
            "omitted_sensitive_fields_present": present_sensitive,
        },
    }


def write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Query the Ai dongle's read-only /paraget.cgi endpoint and save "
            "only a privacy-safe subset of its JSON response."
        )
    )
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    parser.add_argument(
        "--base-url",
        required=True,
        help="Ai dongle HTTPS base URL or IP, for example 192.0.2.10",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP response timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--output",
        help="write the privacy-safe JSON snapshot to this path",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        payload = fetch_parameters(args.base_url, args.timeout)
        result = make_safe_snapshot(payload)
    except DongleDiscoveryError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(result, indent=2, sort_keys=False)
    print(rendered)
    if args.output:
        output = Path(args.output)
        write_json(output, result)
        print(f"Saved privacy-safe discovery output to {output}")
    else:
        print("No --output path supplied; results were not saved.")

    missing = [
        field
        for field in METER_FIELDS
        if field not in result["meter_configuration"]
    ]
    if missing:
        print(
            "The response omitted expected meter fields: " + ", ".join(missing),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
