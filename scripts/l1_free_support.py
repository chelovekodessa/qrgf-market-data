from __future__ import annotations

import base64
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from l1_market_session import is_market_session, latest_completed_session


def observed_history_end(output_dir: Path, manifest: dict[str, Any]) -> str:
    name = str((manifest.get("bundle") or {}).get("name") or "l1-snapshot.csv.gz.b64")
    compressed = base64.b64decode(b"".join((output_dir / name).read_bytes().split()), validate=True)
    csv_bytes = gzip.decompress(compressed)
    observed: list[dt.date] = []
    with io.StringIO(csv_bytes.decode("utf-8-sig"), newline="") as handle:
        for row in csv.DictReader(handle):
            text = str(row.get("as_of") or "").strip()[:10]
            if not text:
                continue
            day = dt.date.fromisoformat(text)
            if not is_market_session(day):
                raise ValueError(f"L1 observed as_of is not a U.S. equity session: {day}")
            observed.append(day)
    if not observed:
        raise ValueError("L1 snapshot contains no observed market sessions")
    return max(observed).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def postprocess_manifest(output_dir: Path) -> None:
    path = output_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    alpaca = manifest.get("alpaca") if isinstance(manifest.get("alpaca"), dict) else {}
    requested_start = str(alpaca.get("effective_history_start") or "")
    requested_end = str(alpaca.get("effective_history_end") or "")
    actual_end = observed_history_end(output_dir, manifest)
    expected_end = latest_completed_session().isoformat()
    if not requested_start or requested_end != expected_end:
        raise ValueError(f"L1 effective requested_history_end is stale: {requested_end}; expected {expected_end}")
    if actual_end != expected_end:
        raise ValueError(f"L1 observed history_end is stale: {actual_end}; expected {expected_end}")
    manifest.update({
        "requested_history_start": requested_start,
        "requested_history_end": requested_end,
        "history_start": requested_start,
        "history_end": actual_end,
        "history_start_semantics": "requested_calendar_boundary",
        "history_end_semantics": "max_observed_market_session",
        "observed_latest_market_session": actual_end,
        "source_id": "alpaca_sip_daily_free_l1",
        "reference_provider": {"provider": "deferred", "mode": "free_bulk_reference_not_required_for_l1", "market_cap_status": "deferred_to_narrowed_research"},
        "market_cap_policy": {"l1_required": False, "known_direct_value_may_reject_below_usd": 250000000, "missing_value": "deferred_not_zero"},
    })
    here = Path(__file__).resolve().parent
    hashes = manifest.setdefault("producer_hashes", {})
    for name in ("l1_market_session.py", "l1_free_support.py"):
        hashes[name] = _sha(here / name)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
