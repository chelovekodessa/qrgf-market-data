#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_universe_v350


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        listing = temp / "nasdaqtraded.txt"
        fields = [
            "Symbol", "Security Name", "Nasdaq Traded", "Listing Exchange",
            "Market Category", "ETF", "Test Issue", "Financial Status",
        ]
        rows = [
            {
                "Symbol": "AMB",
                "Security Name": "Acme Holdings Limited",
                "Nasdaq Traded": "Y",
                "Listing Exchange": "N",
                "Market Category": "",
                "ETF": "N",
                "Test Issue": "N",
                "Financial Status": "N",
            },
            {
                "Symbol": "GOOD",
                "Security Name": "Good Business Inc. - Common Stock",
                "Nasdaq Traded": "Y",
                "Listing Exchange": "Q",
                "Market Category": "Q",
                "ETF": "N",
                "Test Issue": "N",
                "Financial Status": "N",
            },
            {
                "Symbol": "BADW",
                "Security Name": "Bad Vehicle Inc. Warrants",
                "Nasdaq Traded": "Y",
                "Listing Exchange": "Q",
                "Market Category": "Q",
                "ETF": "N",
                "Test Issue": "N",
                "Financial Status": "N",
            },
            {
                "Symbol": "BADN",
                "Security Name": "Example Capital Inc. 7.875% Notes Due 2029",
                "Nasdaq Traded": "Y",
                "Listing Exchange": "N",
                "Market Category": "",
                "ETF": "N",
                "Test Issue": "N",
                "Financial Status": "N",
            },
        ]
        with listing.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, delimiter="|")
            writer.writeheader()
            writer.writerows(rows)
            handle.write("File Creation Time: 0816202601:00|\n")

        accepted, rejected, summary = build_universe_v350.build_universe(
            listing, {}, False, None
        )
        by_ticker = {str(row.get("ticker")): row for row in accepted}
        check("AMB" in by_ticker, "ambiguous operating equity disappeared from L0")
        check(
            str(by_ticker["AMB"].get("contract_id") or "").startswith("qrgf-resolution-required:"),
            "ambiguous equity lacks provisional resolution identity",
        )
        check(
            by_ticker["AMB"].get("contract_id_status") == "requires_authoritative_resolution_before_L3",
            "ambiguous equity lacks L3 resolution gate",
        )
        check("GOOD" in by_ticker, "explicit common equity was lost")
        check("BADW" not in by_ticker, "explicit warrant leaked into rankable L0")
        check(
            any(row.get("ticker") == "BADW" and row.get("rejection_reason") == "warrant" for row in rejected),
            "explicit warrant rejection reason was lost",
        )
        check("BADN" not in by_ticker, "explicit debt note leaked into provisional L0")
        check(
            any(row.get("ticker") == "BADN" and row.get("rejection_reason") == "debt_note_or_bond" for row in rejected),
            "explicit debt note rejection reason was lost",
        )
        check(summary.get("identity_resolution_required_count") == 1, "resolution-required count is wrong")

        accepted_csv = temp / "accepted.csv"
        rejected_csv = temp / "rejected.csv"
        summary_json = temp / "summary.json"
        build_universe_v350.core.write_csv(accepted_csv, accepted)
        build_universe_v350.core.write_csv(rejected_csv, rejected)
        summary_json.write_text(json.dumps(summary), encoding="utf-8")
        output = temp / "published"
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "publish_l0_snapshot.py"),
                "--accepted", str(accepted_csv),
                "--rejected", str(rejected_csv),
                "--summary", str(summary_json),
                "--raw", str(listing),
                "--output-dir", str(output),
                "--source-id", "test",
                "--source-url", "https://example.invalid/test",
                "--page-size", "10",
                "--minimum-raw-rows", "1",
                "--minimum-accepted-rows", "1",
            ],
            check=True,
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        check(manifest.get("schema_version") == "1.1.0", "L0 manifest schema was not bumped")
        audit = manifest.get("rejection_audit") or {}
        check(audit.get("name") == "l0-rejections.csv", "exact L0 rejection audit is missing")
        check((output / "l0-rejections.csv").is_file(), "L0 rejection audit file was not published")

    print("L0 resolution contract regression tests: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
