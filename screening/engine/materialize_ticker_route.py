#!/usr/bin/env python3
"""Materialize a broad ticker->CIK routing hint from edgartools' SEC mirror.

This artifact is never authoritative. The consumer must verify every routed CIK
against the official SEC submissions endpoint before using CompanyFacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

REPO = "dgunning/edgartools"
SOURCE_PATH = "edgar/reference/data/company_tickers.parquet"
API_COMMIT = f"https://api.github.com/repos/{REPO}/commits/{{ref}}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{{commit}}/{SOURCE_PATH}"
USER_AGENT = "qrgf-market-data/3.0"


def request_bytes(
    url: str,
    *,
    github_token: str | None = None,
    accept: str = "application/octet-stream",
) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=90) as response:  # nosec B310 - fixed GitHub endpoints
        return response.read()


def resolve_commit(ref: str, github_token: str | None) -> str:
    payload = json.loads(
        request_bytes(
            API_COMMIT.format(ref=ref),
            github_token=github_token,
            accept="application/vnd.github+json",
        ).decode("utf-8")
    )
    commit = str(payload.get("sha") or "").strip()
    if len(commit) != 40:
        raise ValueError("could not resolve edgartools routing commit")
    return commit


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--meta-output", type=Path, required=True)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--minimum-rows", type=int, default=9000)
    args = parser.parse_args()

    try:
        import pyarrow.parquet as pq
        import pyarrow as pa
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to materialize ticker routing") from exc

    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    commit = resolve_commit(args.ref, token)
    raw = request_bytes(RAW_URL.format(commit=commit))
    raw_sha = hashlib.sha256(raw).hexdigest()
    table = pq.read_table(pa.BufferReader(raw), columns=["cik", "ticker"])
    data: list[list[Any]] = []
    seen: set[tuple[int, str]] = set()
    for cik_raw, ticker_raw in zip(table.column("cik").to_pylist(), table.column("ticker").to_pylist()):
        ticker = str(ticker_raw or "").strip().upper()
        try:
            cik = int(str(cik_raw).strip())
        except (TypeError, ValueError):
            continue
        if not ticker or cik <= 0:
            continue
        key = (cik, ticker)
        if key in seen:
            continue
        seen.add(key)
        data.append([cik, ticker])
    data.sort(key=lambda row: (row[1], row[0]))
    if len(data) < args.minimum_rows:
        raise ValueError(f"ticker routing mirror unexpectedly small: {len(data)}")

    payload = {"fields": ["cik", "ticker"], "data": data}
    meta = {
        "kind": "ticker_cik_routing_hint",
        "authoritative": False,
        "verification_required": "sec_submissions",
        "materializer_sha256": sha256_file(Path(__file__).resolve()),
        "source_repository": REPO,
        "source_ref": args.ref,
        "source_commit": commit,
        "source_path": SOURCE_PATH,
        "source_file_sha256": raw_sha,
        "rows": len(data),
        "unique_tickers": len({row[1] for row in data}),
        "unique_ciks": len({row[0] for row in data}),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    args.meta_output.parent.mkdir(parents=True, exist_ok=True)
    args.meta_output.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
