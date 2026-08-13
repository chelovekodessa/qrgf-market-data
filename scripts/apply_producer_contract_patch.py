#!/usr/bin/env python3
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_exact(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"{label}: expected baseline not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_classify() -> None:
    path = ROOT / "screening/engine/classify_l2.py"
    replace_exact(
        path,
        "from qrgf_common import clamp, is_missing, strict_bool, strict_float, tolerant_bool, tolerant_float  # noqa: E402",
        "from qrgf_common import clamp, is_missing, parse_datetime, strict_bool, strict_float, tolerant_bool, tolerant_float  # noqa: E402",
        "classify parse_datetime import",
    )
    replace_exact(path, 'if sessions >= 252:\n            status = "full"', 'if sessions >= 253:\n            status = "full"', "classify 253-close boundary")


def patch_publisher() -> None:
    path = ROOT / "screening/engine/publish_funnel_snapshot.py"
    replace_exact(
        path,
        '    parser.add_argument("--producer-file", type=Path, action="append", default=[])\n',
        '    parser.add_argument("--producer-file", type=Path, action="append", default=[])\n    parser.add_argument("--producer-release", type=Path, required=True)\n',
        "publisher release argument",
    )
    replace_exact(
        path,
        '    if set(producer_hashes) != required_producers:\n        raise ValueError("producer-file set is incomplete or unexpected")\n\n    source = {\n',
        '    if set(producer_hashes) != required_producers:\n        raise ValueError("producer-file set is incomplete or unexpected")\n    producer_release = load_json(args.producer_release)\n    if str(producer_release.get("schema_version") or "") != "1.0.0":\n        raise ValueError("unsupported producer release schema")\n    release_version = str(producer_release.get("release_version") or "").strip()\n    if not release_version:\n        raise ValueError("producer release_version is required")\n    expected_hashes = producer_release.get("producer_hashes")\n    if not isinstance(expected_hashes, dict) or expected_hashes != producer_hashes:\n        raise ValueError("actual producer hashes do not match producer release manifest")\n    history_contract = producer_release.get("history_contract") or {}\n    if int(history_contract.get("observed_closes_for_12m_return") or 0) != 253:\n        raise ValueError("producer release must require 253 observed closes for 12m return")\n    producer_release_sha256 = sha256_file(args.producer_release)\n\n    source = {\n',
        "publisher release validation",
    )
    replace_exact(
        path,
        '        "producer_hashes": producer_hashes,\n        "l1_summary_sha256": semantic_sha256(l1_summary),\n',
        '        "producer_hashes": producer_hashes,\n        "producer_release_version": release_version,\n        "producer_release_sha256": producer_release_sha256,\n        "l1_summary_sha256": semantic_sha256(l1_summary),\n',
        "publisher content identity",
    )
    replace_exact(
        path,
        '        "producer_hashes": producer_hashes,\n    }\n    atomic_json(manifest_path, manifest)\n',
        '        "producer_hashes": producer_hashes,\n        "producer_release": {\n            "schema_version": "1.0.0",\n            "release_version": release_version,\n            "manifest_path": "screening/config/producer-release.json",\n            "manifest_sha256": producer_release_sha256,\n            "history_contract": history_contract,\n        },\n    }\n    atomic_json(manifest_path, manifest)\n',
        "publisher manifest provenance",
    )


def write_test() -> None:
    path = ROOT / "screening/tests/test_producer_contract.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('''#!/usr/bin/env python3\nfrom __future__ import annotations\n\nimport sys\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[2]\nENGINE = ROOT / "screening" / "engine"\nif str(ENGINE) not in sys.path:\n    sys.path.insert(0, str(ENGINE))\n\nimport classify_l2\nimport bulk_prefilter\n\n\ndef check(condition: bool, message: str) -> None:\n    if not condition:\n        raise AssertionError(message)\n\n\ndef main() -> int:\n    base = {"ticker": "BOUNDARY", "return_3m_pct": 1.0, "return_6m_pct": 2.0, "return_12m_pct": 3.0}\n    s252, _ = classify_l2.derive_history({**base, "trading_history_days": 252})\n    s253, _ = classify_l2.derive_history({**base, "trading_history_days": 253})\n    check(s252 == "limited_but_usable", f"252 boundary regressed: {s252}")\n    check(s253 == "full", f"253 boundary regressed: {s253}")\n\n    mature, missing = classify_l2.derive_history({\n        "ticker": "MATURE", "return_3m_pct": 1.0, "return_6m_pct": 2.0, "return_12m_pct": None,\n        "listing_date": "2020-01-02", "as_of": "2026-08-12",\n    })\n    check(mature == "full" and "return_12m_pct" in missing, f"listing fallback failed: {mature}, {missing}")\n\n    base_row = {\n        "ticker": "GAP", "sources": ["first"], "source_conflicts": [], "_field_meta": {},\n        "trading_history_days": 180, "return_3m": 1.0, "return_6m": 2.0, "return_12m": None,\n        "momentum_history_status": "limited_but_usable",\n    }\n    gap_item = {"ticker": "GAP", "sources": ["gap-source"], "source_conflicts": [], "_field_meta": {}, "momentum_history_status": "source_gap"}\n    bulk_prefilter._merge_item(base_row, gap_item)\n    check(base_row["momentum_history_status"] == "unknown", f"source_gap lost: {base_row['momentum_history_status']}")\n    check(base_row.get("_explicit_history_source_gap") is True, "source_gap sticky marker missing")\n    followup = {\n        "ticker": "GAP", "sources": ["followup"], "source_conflicts": [], "_field_meta": {},\n        "trading_history_days": 180, "return_3m": 1.0, "return_6m": 2.0, "momentum_history_status": "limited_but_usable",\n    }\n    bulk_prefilter._merge_item(base_row, followup)\n    check(base_row["momentum_history_status"] == "unknown", "later update erased source_gap")\n    print("producer contract regression tests: PASS")\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n''', encoding="utf-8")


def materialize_bulk_for_test() -> Path:
    vendor = ROOT / "screening/vendor/bulk_prefilter.py.gz.b64"
    out = ROOT / "screening/engine/bulk_prefilter.py"
    raw = gzip.decompress(base64.b64decode(b"".join(vendor.read_bytes().split()), validate=True)).decode("utf-8")
    old_history = '        if sessions >= L1_SESSIONS_12M:\n            return "full"'
    new_history = ('        # A 252-session return needs 253 observed closes (current close plus\n'
                   '        # the close 252 sessions earlier). The L1 producer follows the same\n'
                   '        # contract and extends histories shorter than 253 observations.\n'
                   '        if sessions > L1_SESSIONS_12M:\n            return "full"')
    old_gate = ('    for row in eligible:\n'
                '        status = threshold_status(row)\n'
                '        if status in {"below_price_threshold", "below_liquidity_threshold", "below_market_cap_threshold"}:\n'
                '            continue\n'
                '        history = str(row.get("momentum_history_status") or "unknown")\n'
                '        if status == "pass" and history == "insufficient":\n'
                '            insufficient.append(row)\n'
                '            continue')
    new_gate = ('    for row in eligible:\n'
                '        history = str(row.get("momentum_history_status") or "unknown")\n'
                '        # Objective insufficient history is already terminal for L1 research\n'
                '        # eligibility. Later missing price/liquidity fields cannot change that\n'
                '        # outcome and therefore must not deadlock the full-market coverage gate.\n'
                '        if history == "insufficient":\n'
                '            insufficient.append(row)\n'
                '            continue\n'
                '        status = threshold_status(row)\n'
                '        if status in {"below_price_threshold", "below_liquidity_threshold", "below_market_cap_threshold"}:\n'
                '            continue')
    old_merge = ('    base["quality_seed"] = bool(base.get("quality_seed")) or bool(item.get("quality_seed"))\n'
                 '    base["momentum_history_status"] = normalize_momentum_history_status(\n'
                 '        None,\n'
                 '        base.get("trading_history_days"),\n'
                 '        base.get("return_3m"),\n'
                 '        base.get("return_6m"),\n'
                 '        base.get("return_12m"),\n'
                 '        base.get("listing_date"),\n'
                 '        base.get("as_of"),\n'
                 '    )\n')
    new_merge = ('    base["quality_seed"] = bool(base.get("quality_seed")) or bool(item.get("quality_seed"))\n'
                 '    # An explicit provider source gap must survive merge order and stronger-looking\n'
                 '    # partial updates. Keep the marker private so it cannot leak into exported CSV,\n'
                 '    # but use it when recomputing the canonical history status.\n'
                 '    base["_explicit_history_source_gap"] = bool(base.get("_explicit_history_source_gap")) or (\n'
                 '        str(item.get("momentum_history_status") or "").strip().lower() == "source_gap"\n'
                 '    )\n'
                 '    base["momentum_history_status"] = normalize_momentum_history_status(\n'
                 '        "source_gap" if base["_explicit_history_source_gap"] else None,\n'
                 '        base.get("trading_history_days"),\n'
                 '        base.get("return_3m"),\n'
                 '        base.get("return_6m"),\n'
                 '        base.get("return_12m"),\n'
                 '        base.get("listing_date"),\n'
                 '        base.get("as_of"),\n'
                 '    )\n')
    if old_history not in raw or old_gate not in raw or old_merge not in raw:
        raise SystemExit("vendor bulk_prefilter baseline changed unexpectedly")
    raw = raw.replace(old_history, new_history).replace(old_gate, new_gate).replace(old_merge, new_merge)
    out.write_text(raw, encoding="utf-8")
    expected = "0fa5bc831a1745587530ddcc33dbd3eeee3fa1146760ce82392412e5c019abf3"
    if sha256(out) != expected:
        raise SystemExit(f"materialized bulk_prefilter hash mismatch: {sha256(out)}")
    return out


def write_release_manifest() -> None:
    payload = {
        "schema_version": "1.0.0",
        "release_version": "1.0.0",
        "history_contract": {
            "observed_closes_for_12m_return": 253,
            "source_gap_is_sticky": True,
        },
        "producer_hashes": {
            "bulk_prefilter.py": "0fa5bc831a1745587530ddcc33dbd3eeee3fa1146760ce82392412e5c019abf3",
            "batch_l2.py": "4cb69718e6e22d5c199b9ac740e96e697faa4e0f1f214ff9b825af083a482a5b",
            "classify_l2.py": "36d29aec9a66ad9ed4b56bfac74b8026ee2e9ed7dceac6f05a8cf8df1767bb42",
            "qrgf_common.py": "34195413b2623872388ad0c769f68346199e6efe94896647c0ec120ae8233b6a",
            "l1-rules.json": "5287a1713d12f365272c4df799a1b31a8f5c4e26ddef3d9a94019572fb1cb3e3",
            "l2-rules.json": "e1dedc6a3786bd69b3727396adbfbc8bb145f1a7e966e2d06e58495739c51a93",
            "publish_funnel_snapshot.py": "d705fd346ef4957af6d36b1aa1acd63ab651d7fe13157155cbcea53a5f5f3d44",
            "update-funnel.yml": "c9342310b3d0f88727b4625d799aaadf67407c1a7bfd7d2fdb3cf18f3ae10f8d",
        },
    }
    path = ROOT / "screening/config/producer-release.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    patch_classify()
    patch_publisher()
    write_test()
    write_release_manifest()
    bulk = materialize_bulk_for_test()
    try:
        subprocess.run(["python", "-m", "compileall", "-q", "screening/engine/classify_l2.py", "screening/engine/publish_funnel_snapshot.py", "screening/tests/test_producer_contract.py"], cwd=ROOT, check=True)
        subprocess.run(["python", "screening/tests/test_producer_contract.py"], cwd=ROOT, check=True)
    finally:
        bulk.unlink(missing_ok=True)
    expected = {
        ROOT / "screening/engine/classify_l2.py": "36d29aec9a66ad9ed4b56bfac74b8026ee2e9ed7dceac6f05a8cf8df1767bb42",
        ROOT / "screening/engine/publish_funnel_snapshot.py": "d705fd346ef4957af6d36b1aa1acd63ab651d7fe13157155cbcea53a5f5f3d44",
    }
    for path, digest in expected.items():
        if sha256(path) != digest:
            raise SystemExit(f"unexpected final hash for {path}: {sha256(path)}")
    print("one-time producer contract patch: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
