#!/usr/bin/env python3
"""Publish a deterministic paged L0 snapshot from build_universe outputs."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import io
import datetime as dt
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain an object")
    return payload


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def source_creation_time(raw_path: Path) -> str | None:
    lines = raw_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    for line in reversed(lines[-10:]):
        if line.lower().startswith("file creation time"):
            parts = line.split(":", 1)
            return parts[1].strip().strip("|") if len(parts) == 2 else line.strip().strip("|")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted", type=Path, required=True)
    parser.add_argument("--rejected", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--minimum-raw-rows", type=int, default=5000)
    parser.add_argument("--minimum-accepted-rows", type=int, default=3000)
    parser.add_argument("--producer-file", type=Path, action="append", default=[])
    args = parser.parse_args()

    accepted = read_rows(args.accepted)
    rejected = read_rows(args.rejected)
    summary = load_json(args.summary)
    raw_rows = int(summary.get("raw_rows") or 0)
    if raw_rows < args.minimum_raw_rows:
        raise ValueError(f"raw universe unexpectedly small: {raw_rows}")
    if len(accepted) < args.minimum_accepted_rows:
        raise ValueError(f"accepted universe unexpectedly small: {len(accepted)}")
    if int(summary.get("accepted_unique") or 0) != len(accepted):
        raise ValueError("accepted_unique does not match accepted CSV")
    if not accepted:
        raise ValueError("accepted universe is empty")

    fields = list(accepted[0].keys())
    output = args.output_dir
    pages_dir = output / "pages"
    if output.exists():
        shutil.rmtree(output)
    pages_dir.mkdir(parents=True, exist_ok=True)

    pages: list[dict[str, Any]] = []
    for start in range(0, len(accepted), args.page_size):
        chunk = accepted[start : start + args.page_size]
        name = f"page-{start // args.page_size + 1:04d}.csv"
        page_path = pages_dir / name
        write_rows(page_path, chunk, fields)
        pages.append({"name": name, "rows": len(chunk), "sha256": sha256_file(page_path)})

    csv_buffer = io.StringIO(newline="")
    csv_writer = csv.DictWriter(csv_buffer, fieldnames=fields, extrasaction="ignore")
    csv_writer.writeheader()
    csv_writer.writerows(accepted)
    csv_bytes = csv_buffer.getvalue().encode("utf-8")
    gzip_bytes = gzip.compress(csv_bytes, compresslevel=9, mtime=0)
    bundle_bytes = base64.encodebytes(gzip_bytes)
    bundle_path = output / "l0-universe.csv.gz.b64"
    bundle_path.write_bytes(bundle_bytes)
    bundle = {
        "name": bundle_path.name,
        "encoding": "base64+gzip",
        "rows": len(accepted),
        "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "gzip_sha256": hashlib.sha256(gzip_bytes).hexdigest(),
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "bytes": len(bundle_bytes),
        "line_count": bundle_bytes.count(b"\n"),
    }

    # Publish the exact per-row exclusion ledger. Aggregate reason counts alone
    # are insufficient to explain why a specific security disappeared from L0.
    rejection_path = output / "l0-rejections.csv"
    rejection_fields: list[str] = []
    for row in rejected:
        for key in row:
            if key not in rejection_fields:
                rejection_fields.append(key)
    if not rejection_fields:
        rejection_fields = ["ticker", "rejection_reason"]
    write_rows(rejection_path, rejected, rejection_fields)
    rejection_audit = {
        "name": rejection_path.name,
        "rows": len(rejected),
        "sha256": sha256_file(rejection_path),
    }

    producer_hashes = {path.name: sha256_file(path) for path in args.producer_file}
    rejection_reasons = Counter(str(row.get("rejection_reason") or "unknown") for row in rejected)
    manifest = {
        "schema_version": "1.0.0",
        "complete": True,
        "source_id": args.source_id,
        "source_url": args.source_url,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_file_creation_time": source_creation_time(args.raw),
        "raw_sha256": sha256_file(args.raw),
        "raw_bytes": args.raw.stat().st_size,
        "raw_rows": raw_rows,
        "accepted_unique": len(accepted),
        "page_size": args.page_size,
        "page_count": len(pages),
        "pages": pages,
        "bundle": bundle,
        "rejection_audit": rejection_audit,
        "producer_hashes": producer_hashes,
        "summary": summary,
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
