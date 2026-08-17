#!/usr/bin/env python3
"""Build the QRGF v3 full-market Radar from the existing complete L1 snapshot.

This producer never clips the market to a fixed candidate count. It reuses the
pinned legacy L2 setup model only as cheap research-priority features. Structural
quality is intentionally absent from this stage and is supplied later by the
Quality Registry / targeted research.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from batch_l2 import adapt_l1_candidate, apply_selection_contract  # noqa: E402
from classify_l2 import classify, growth_score, liquidity_score, pullback_score  # noqa: E402

_CLASS_SUFFIXES = (
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$", re.I),
    re.compile(r"\s*[-–—]\s*class\s+[a-z0-9-]+\s+ordinary\s+shares?\s*$", re.I),
    re.compile(r"\s+class\s+[a-z0-9-]+\s+(?:common\s+|capital\s+)?(?:stock|shares?)\s*$", re.I),
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def load_l1_bundle(path: Path) -> list[dict[str, Any]]:
    compressed = base64.b64decode(b"".join(path.read_bytes().split()), validate=True)
    text = gzip.decompress(compressed).decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def load_sec_tickers(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    raw = load_json(path)
    result: dict[str, str] = {}
    values = raw.values() if all(isinstance(v, dict) for v in raw.values()) else []
    for item in values:
        ticker = str(item.get("ticker") or "").strip().upper()
        cik = str(item.get("cik_str") or "").strip().lstrip("0")
        if ticker and cik.isdigit():
            padded = f"{int(cik):010d}"
            for key in {ticker, ticker.replace("-", "."), ticker.replace(".", "-")}:
                result.setdefault(key, padded)
    return result


def _clean_company(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern in _CLASS_SUFFIXES:
        stripped = pattern.sub("", text).strip(" -–—")
        if stripped != text:
            return re.sub(r"[^A-Z0-9]+", " ", stripped.upper()).strip()
    return ""


def issuer_identity(row: dict[str, Any], sec_map: dict[str, str]) -> tuple[str, str | None]:
    ticker = str(row.get("ticker") or "").strip().upper()
    contract = str(row.get("contract_id") or "").strip()
    security_type = str(row.get("security_type") or "").strip().lower()
    if security_type == "etf":
        return f"SECURITY:{ticker}:{contract}", None
    cik = sec_map.get(ticker) or sec_map.get(ticker.replace(".", "-")) or sec_map.get(ticker.replace("-", "."))
    if cik:
        return f"CIK:{cik}", cik
    company = _clean_company(row.get("company") or row.get("company_name"))
    if company:
        return f"NAMECLASS:{company}", None
    return f"SECURITY:{ticker}:{contract}", None


def compact_row(raw: dict[str, Any], classified: dict[str, Any], rules: dict[str, Any], sec_map: dict[str, str]) -> dict[str, Any]:
    row = {**raw, **classified}
    issuer_id, cik = issuer_identity(row, sec_map)
    r3 = _num(row.get("return_3m_pct", row.get("return_3m")))
    r6 = _num(row.get("return_6m_pct", row.get("return_6m")))
    r12 = _num(row.get("return_12m_pct", row.get("return_12m")))
    dd = _num(row.get("drawdown_pct", row.get("drawdown_52w")))
    adv = _num(row.get("avg_dollar_volume"))
    history = str(row.get("momentum_history_status") or "unknown")
    prior_growth = growth_score(r3, r6, r12, history) if r3 is not None and r6 is not None else None
    pullback = pullback_score(dd) if dd is not None else None
    liquidity = liquidity_score(adv) if adv is not None else None
    data_completeness = None
    research_components = row.get("research_components") if isinstance(row.get("research_components"), dict) else {}
    if research_components.get("data_completeness") is not None:
        data_completeness = _num(research_components.get("data_completeness"))
    current = _num(row.get("current_price", row.get("price")))
    reference_high = None
    if current is not None and dd is not None and dd < 100:
        denominator = 1.0 - dd / 100.0
        if denominator > 0:
            reference_high = current / denominator
    return {
        "ticker": str(row.get("ticker") or "").upper(),
        "company": row.get("company") or row.get("company_name"),
        "contract_id": row.get("contract_id"),
        "issuer_id": issuer_id,
        "issuer_cik": cik,
        "security_type": row.get("security_type"),
        "instrument_status": row.get("instrument_status"),
        "exchange": row.get("exchange"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "current_price": current,
        "reference_52w_high": round(reference_high, 8) if reference_high is not None else None,
        "market_cap": _num(row.get("market_cap")),
        "avg_dollar_volume": adv,
        "return_1m_pct": _num(row.get("return_1m_pct", row.get("return_1m"))),
        "return_3m_pct": r3,
        "return_6m_pct": r6,
        "return_12m_pct": r12,
        "drawdown_pct": dd,
        "historical_volatility_pct": _num(row.get("historical_volatility_pct", row.get("historical_volatility"))),
        "trading_history_days": _num(row.get("trading_history_days")),
        "momentum_history_status": history,
        "data_integrity_status": row.get("data_integrity_status") or "usable",
        "as_of": row.get("as_of") or row.get("data_as_of"),
        "l2_status": row.get("l2_status"),
        "l2_setup_score": _num(row.get("l2_setup_score")),
        "l2_confidence_pct": _num(row.get("l2_confidence_pct")),
        "l2_quality_prior_score": _num(row.get("l2_quality_prior_score")),
        "l2_room_to_target_score": _num(row.get("l2_room_to_target_score")),
        "setup_prior_growth": prior_growth,
        "setup_pullback_geometry": pullback,
        "setup_liquidity": liquidity,
        "setup_data_completeness": data_completeness,
        "setup_model_version": str((rules.get("selection_setup") or {}).get("model_version") or ""),
        "l2_rules_hash": row.get("l2_rules_hash"),
    }


def _num(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_registry(output_root: Path, release: dict[str, Any]) -> tuple[dict[str, Any], str]:
    latest = output_root / "registry" / "latest.json"
    if latest.is_file():
        registry = load_json(latest)
        expected = registry.get("registry_sha256")
        body = {k: v for k, v in registry.items() if k != "registry_sha256"}
        if expected != semantic_hash(body):
            raise ValueError("existing v3 registry self-hash mismatch")
        return registry, str(latest.as_posix())
    body = {
        "schema_version": "1.0.0",
        "kind": "qrgf_quality_registry",
        "registry_id": "bootstrap-empty-v1",
        "created_at": "1970-01-01T00:00:00Z",
        "quality_policy_version": str(release.get("quality_policy_version") or "3.0.0-structural-v1"),
        "entries": [],
    }
    registry = {**body, "registry_sha256": semantic_hash(body)}
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    immutable = output_root / "registry" / "snapshots" / f"{registry['registry_sha256']}.json"
    immutable.parent.mkdir(parents=True, exist_ok=True)
    immutable.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return registry, immutable.as_posix()


def verify_release(release_path: Path, repo_root: Path) -> tuple[dict[str, Any], str]:
    release = load_json(release_path)
    if release.get("schema_version") != "1.0.0" or release.get("release_version") != "4.0.0":
        raise ValueError("unexpected v3 producer release")
    hashes = release.get("producer_hashes") or {}
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("v3 producer release has no producer hashes")
    mapping = {
        "build_v3_radar.py": repo_root / "screening/engine/build_v3_radar.py",
        "batch_l2.py": repo_root / "screening/engine/batch_l2.py",
        "classify_l2.py": repo_root / "screening/engine/classify_l2.py",
        "detect-v3-registry-changes.yml": repo_root / ".github/workflows/detect-v3-registry-changes.yml",
        "detect_v3_registry_changes.py": repo_root / "screening/engine/detect_v3_registry_changes.py",
        "l2-rules.json": repo_root / "screening/assets/l2-rules.json",
        "promote-v3-registry.yml": repo_root / ".github/workflows/promote-v3-registry.yml",
        "promote_v3_registry.py": repo_root / "screening/engine/promote_v3_registry.py",
        "qrgf_common.py": repo_root / "screening/engine/qrgf_common.py",
        "update-v3.yml": repo_root / ".github/workflows/update-v3.yml",
    }
    if set(hashes) != set(mapping):
        raise ValueError("v3 producer release file set mismatch")
    for name, path in mapping.items():
        if not path.is_file() or hashes.get(name) != sha256_file(path):
            raise ValueError(f"v3 producer hash mismatch: {name}")
    return release, sha256_file(release_path)


def build(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = args.repo_root.resolve()
    l1_manifest = load_json(args.l1_manifest)
    if l1_manifest.get("complete") is not True:
        raise ValueError("L1 manifest is incomplete")
    l1_rows = load_l1_bundle(args.l1_bundle)
    if len(l1_rows) != int(l1_manifest.get("rows") or -1):
        raise ValueError("L1 bundle/manifest row count mismatch")
    market_session = str(l1_manifest.get("history_end") or "")
    if len(market_session) != 10:
        raise ValueError("L1 history_end is not a market-session date")
    rules_bytes = args.rules.read_bytes()
    rules_hash = hashlib.sha256(rules_bytes).hexdigest()
    rules = json.loads(rules_bytes)
    release, release_hash = verify_release(args.release_manifest, repo_root)
    sec_map = load_sec_tickers(args.sec_tickers)

    radar: list[dict[str, Any]] = []
    for raw in l1_rows:
        adapted = adapt_l1_candidate(raw)
        classified = apply_selection_contract(classify(adapted, rules, rules_hash), rules)
        classified["l2_rules_hash"] = rules_hash
        radar.append(compact_row(adapted, classified, rules, sec_map))
    radar.sort(key=lambda row: (str(row.get("ticker") or ""), str(row.get("contract_id") or "")))
    if len(radar) < int(args.minimum_rows):
        raise ValueError(f"v3 Radar coverage too small: {len(radar)}")

    output_root = args.output_root
    work = output_root.parent / ".v3-next"
    if work.exists():
        shutil.rmtree(work)
    radar_dir = work / "latest" / "radar"
    radar_dir.mkdir(parents=True, exist_ok=True)
    fields = list(radar[0].keys())
    pages = []
    for index, start in enumerate(range(0, len(radar), args.page_size), 1):
        chunk = radar[start:start + args.page_size]
        name = f"page-{index:04d}.csv"
        path = radar_dir / name
        write_csv(path, chunk, fields)
        pages.append({"name": name, "rows": len(chunk), "sha256": sha256_file(path)})

    rankable = sum(row.get("l2_setup_score") is not None and row.get("l2_status") != "rejected" for row in radar)
    manifest_body = {
        "schema_version": "1.0.0",
        "kind": "qrgf_market_radar",
        "complete": True,
        "snapshot_id": "pending",
        "market_session_id": market_session,
        "created_at": args.created_at,
        "rows": len(radar),
        "rankable_rows": rankable,
        "page_size": args.page_size,
        "pages": pages,
        "rows_semantic_sha256": semantic_hash(radar),
        "l1_lineage": {
            "manifest_sha256": sha256_file(args.l1_manifest),
            "source_id": l1_manifest.get("source_id"),
            "history_end": l1_manifest.get("history_end"),
            "rows": l1_manifest.get("rows"),
        },
        "setup_model": {
            "ruleset_version": rules.get("ruleset_version"),
            "l2_rules_sha256": rules_hash,
            "selection_model_version": (rules.get("selection_setup") or {}).get("model_version"),
            "weights": (rules.get("selection_setup") or {}).get("weights"),
            "fixed_candidate_count_cutoff": False,
        },
        "issuer_identity": {
            "sec_company_tickers_used": bool(sec_map),
            "cik_mapped_rows": sum(bool(row.get("issuer_cik")) for row in radar),
            "fallback_is_conservative_share_class_normalized": True,
            "etf_identity_is_security_specific": True,
        },
        "producer_release": {
            "release_version": release.get("release_version"),
            "manifest_path": str(args.release_manifest.relative_to(repo_root).as_posix()),
            "manifest_sha256": release_hash,
            "producer_hashes": release.get("producer_hashes"),
        },
    }
    seed = {
        "market_session_id": market_session,
        "rows_semantic_sha256": manifest_body["rows_semantic_sha256"],
        "l1_manifest_sha256": manifest_body["l1_lineage"]["manifest_sha256"],
        "producer_release_sha256": release_hash,
        "source_commit_sha": args.source_commit_sha,
    }
    snapshot_content_sha = semantic_hash(seed)
    snapshot_id = snapshot_content_sha[:24]
    manifest_body["snapshot_id"] = snapshot_id
    manifest_body["snapshot_content_sha256"] = snapshot_content_sha
    manifest_body["manifest_semantic_sha256"] = semantic_hash(manifest_body)
    manifest_path = radar_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Carry the last validated registry forward. The Registry is a cache only;
    # market correctness never depends on a successful registry write.
    real_output_root = output_root
    existing_registry = real_output_root / "registry" / "latest.json"
    if existing_registry.is_file():
        (work / "registry").mkdir(parents=True, exist_ok=True)
        shutil.copytree(real_output_root / "registry", work / "registry", dirs_exist_ok=True)
    registry, _registry_path = ensure_registry(work, release)

    system_body = {
        "schema_version": "1.0.0",
        "kind": "qrgf_v3_system_snapshot",
        "complete": True,
        "snapshot_id": snapshot_id,
        "snapshot_content_sha256": snapshot_content_sha,
        "market_session_id": market_session,
        "created_at": args.created_at,
        "radar_manifest_path": "data/v3/latest/radar/manifest.json",
        "radar_manifest_sha256": manifest_body["manifest_semantic_sha256"],
        "registry_manifest_path": "data/v3/registry/latest.json",
        "registry_sha256": registry["registry_sha256"],
        "producer_release_sha256": release_hash,
        "source_commit_sha": args.source_commit_sha,
        "coverage": {
            "universe_rows": len(radar),
            "market_scanned_rows": len(radar),
            "rankable_market_rows": rankable,
            "registry_entry_count": len(registry.get("entries") or []),
            "frontier_complete": False,
        },
    }
    system = {**system_body, "system_snapshot_sha256": semantic_hash(system_body)}
    (work / "latest.json").write_text(json.dumps(system, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (work / "latest" / "system-snapshot.json").write_text(json.dumps(system, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if output_root.exists():
        backup = output_root.parent / ".v3-old"
        if backup.exists():
            shutil.rmtree(backup)
        output_root.rename(backup)
        work.rename(output_root)
        shutil.rmtree(backup)
    else:
        work.rename(output_root)
    return {
        "complete": True,
        "snapshot_id": snapshot_id,
        "market_session_id": market_session,
        "rows": len(radar),
        "rankable_rows": rankable,
        "registry_sha256": registry["registry_sha256"],
        "producer_release_sha256": release_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--l1-manifest", type=Path, required=True)
    parser.add_argument("--l1-bundle", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--sec-tickers", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-commit-sha", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--minimum-rows", type=int, default=3000)
    args = parser.parse_args()
    if len(args.source_commit_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.source_commit_sha):
        parser.error("source commit SHA must be full lowercase git SHA")
    result = build(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
