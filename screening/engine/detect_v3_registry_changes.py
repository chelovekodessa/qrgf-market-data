#!/usr/bin/env python3
"""Cheap SEC filing delta detector for the QRGF v3 Quality Registry.

It never refreshes a Passport. It only downgrades affected/uncertain issuer
entries from fresh to needs_refresh so expensive research can be targeted later.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "8-K", "8-K/A", "6-K", "6-K/A"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def fetch_json(url: str, user_agent: str, attempts: int = 4) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                value = json.loads(response.read().decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("SEC response is not an object")
                return value
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 401, 403, 404}:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"SEC submissions request failed: {last}")


def recent_events(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    filings = payload.get("filings") if isinstance(payload.get("filings"), Mapping) else {}
    recent = filings.get("recent") if isinstance(filings.get("recent"), Mapping) else {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary = recent.get("primaryDocument") or []
    result = []
    for index, form in enumerate(forms):
        text = str(form or "")
        if text not in FORMS:
            continue
        result.append({
            "form": text,
            "accession": str(accessions[index] if index < len(accessions) else ""),
            "filing_date": str(dates[index] if index < len(dates) else ""),
            "primary_document": str(primary[index] if index < len(primary) else ""),
        })
    return result


def publish(root: Path, registry: dict[str, Any], entries: list[dict[str, Any]], marker: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    body = {
        "schema_version": "1.0.0", "kind": "qrgf_quality_registry",
        "registry_id": f"registry-delta-{marker[:16]}", "created_at": now,
        "quality_policy_version": registry.get("quality_policy_version"),
        "entries": sorted(entries, key=lambda row: str(row.get("issuer_id") or "")),
    }
    value = {**body, "registry_sha256": semantic_hash(body)}
    snapshots = root / "registry" / "snapshots"; snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / f"{value['registry_sha256']}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "registry" / "latest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for path in (root / "latest.json", root / "latest" / "system-snapshot.json"):
        if not path.is_file():
            continue
        system = load(path)
        system["registry_sha256"] = value["registry_sha256"]
        coverage = system.get("coverage") if isinstance(system.get("coverage"), dict) else {}
        coverage["registry_entry_count"] = len(entries); system["coverage"] = coverage
        system.pop("system_snapshot_sha256", None); system["system_snapshot_sha256"] = semantic_hash(system)
        path.write_text(json.dumps(system, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v3-root", type=Path, required=True)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--min-interval-seconds", type=float, default=0.12)
    args = parser.parse_args()
    latest = args.v3_root / "registry" / "latest.json"
    if not latest.is_file():
        print(json.dumps({"checked": 0, "changed": 0, "reason": "registry_not_published"}, sort_keys=True)); return 0
    registry = load(latest)
    body = {k: v for k, v in registry.items() if k != "registry_sha256"}
    if registry.get("registry_sha256") != semantic_hash(body):
        raise ValueError("Registry self-hash mismatch")
    entries = [dict(row) for row in registry.get("entries") or []]
    changed = 0; checked = 0; failures = 0; marker_parts: list[str] = []
    for row in entries:
        issuer = str(row.get("issuer_id") or "")
        if not issuer.startswith("CIK:"):
            continue
        cik = issuer.split(":", 1)[1]
        if not (len(cik) == 10 and cik.isdigit()):
            row["freshness_status"] = "needs_refresh"; row["stale_reason"] = "invalid_cik_identity"; changed += 1; continue
        checked += 1
        try:
            payload = fetch_json(f"https://data.sec.gov/submissions/CIK{cik}.json", args.user_agent)
            events = recent_events(payload)
            scan_through = str(row.get("event_scan_through") or "")[:10]
            newer = [event for event in events if event["filing_date"] and (not scan_through or event["filing_date"] > scan_through)]
            if newer:
                newest = max(newer, key=lambda item: (item["filing_date"], item["accession"]))
                row["freshness_status"] = "needs_refresh"
                row["stale_reason"] = "new_sec_filing"
                row["detected_event"] = newest
                changed += 1
                marker_parts.append(f"{issuer}:{newest['accession']}")
            row["sec_delta_checked_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except Exception as exc:
            row["freshness_status"] = "needs_refresh"
            row["stale_reason"] = "sec_delta_check_failed"
            row["delta_check_error"] = str(exc)[:300]
            changed += 1; failures += 1; marker_parts.append(f"{issuer}:failed")
        time.sleep(max(0.0, args.min_interval_seconds))
    if changed:
        marker = semantic_hash(marker_parts or ["state_change"])
        registry = publish(args.v3_root, registry, entries, marker)
    print(json.dumps({"checked": checked, "changed": changed, "failures": failures, "registry_sha256": registry["registry_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
