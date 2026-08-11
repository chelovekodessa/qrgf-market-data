#!/usr/bin/env python3
"""Record one non-sensitive probe of delayed Alpaca SIP daily data."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

URL = "https://data.alpaca.markets/v2/stocks/bars"
START_DATE = "2025-07-06"
END_DATE = "2026-08-10"


def safe_message(value: object, key_id: str, secret: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    for token in (key_id, secret):
        if token:
            text = text.replace(token, "[redacted]")
    text = re.sub(r"(?i)(api[_ -]?key|secret|authorization)\s*[:=]\s*[^ ,;]+", r"\1=[redacted]", text)
    return text[:500] or "provider_error"


def query(label: str, start: str, end: str, key_id: str, secret: str) -> dict:
    params = {"symbols": "AAPL", "timeframe": "1Day", "start": start, "end": end, "limit": "10000", "adjustment": "split", "feed": "sip", "sort": "asc"}
    request = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}", headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret, "Accept": "application/json", "User-Agent": "qrgf-delayed-sip-probe/1.0"}, method="GET")
    result = {"label": label, "start": start, "end": end}
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            bars = payload.get("bars") if isinstance(payload, dict) else {}
            rows = bars.get("AAPL") if isinstance(bars, dict) else []
            result.update({"http_status": response.status, "message": None, "bars_returned": len(rows) if isinstance(rows, list) else 0, "latest_bar_timestamp": (rows[-1].get("t") if isinstance(rows, list) and rows else None)})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try: payload = json.loads(body)
        except json.JSONDecodeError: payload = {"message": body}
        result.update({"http_status": exc.code, "message": safe_message(payload.get("message") if isinstance(payload, dict) else payload, key_id, secret)})
    except Exception as exc:
        result.update({"http_status": None, "message": safe_message(type(exc).__name__, key_id, secret)})
    return result


def main() -> int:
    key_id = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    result = {"schema_version": "1.1.0", "probed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "provider": "alpaca", "feed": "sip", "timeframe": "1Day", "symbols": ["AAPL"], "credentials_present": bool(key_id and secret), "probes": []}
    if not key_id or not secret:
        result["probes"] = [{"label": "credentials", "http_status": None, "message": "credentials_missing"}]
    else:
        result["probes"] = [
            query("production_date_only", START_DATE, END_DATE, key_id, secret),
            query("explicit_utc_bounds", f"{START_DATE}T00:00:00Z", f"{END_DATE}T23:59:59Z", key_id, secret),
        ]
    path = Path("data/l1-diagnostics/alpaca-sip-probe.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["probes"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
