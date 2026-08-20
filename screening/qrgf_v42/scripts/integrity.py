#!/usr/bin/env python3
"""QRGF V4 runtime integrity manifest."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from common import ROOT, file_hash, semantic_hash, write_json

MANIFEST=ROOT/"config"/"integrity-manifest.json"
PACKAGE_VERSION="4.2.1"
MANIFEST_SCHEMA_VERSION="2.0.0"
RUNTIME_ROOT_FILES={"SKILL.md"}
RUNTIME_PREFIXES=("config/","references/","schemas/","scripts/")
EXCLUDED={"config/integrity-manifest.json"}
EXCLUDED_PREFIXES=("agents/","assets/","tests/")


def runtime_protected(relative: str) -> bool:
    if relative in EXCLUDED or any(relative.startswith(p) for p in EXCLUDED_PREFIXES): return False
    return relative in RUNTIME_ROOT_FILES or any(relative.startswith(p) for p in RUNTIME_PREFIXES)


def build() -> dict[str, Any]:
    files=[]
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix==".pyc": continue
        rel=path.relative_to(ROOT).as_posix()
        if runtime_protected(rel): files.append({"path":rel,"sha256":file_hash(path),"bytes":path.stat().st_size})
    analytical=[x for x in files if x["path"] in {
        "config/policy.json","config/connectors.json","config/approved-etfs.csv","scripts/provenance.py","scripts/bootstrap.py","scripts/deployment.py","scripts/factpack.py","scripts/passport.py","scripts/research.py","scripts/selection.py","scripts/evidence.py","scripts/eligibility.py","scripts/registry.py","scripts/registry_store.py","scripts/campaign.py","scripts/migration.py","scripts/market_view.py","scripts/cli.py","scripts/decision.py","scripts/technical.py","scripts/recovery.py"
    } or x["path"].startswith("schemas/")]
    body={
        "schema_version":MANIFEST_SCHEMA_VERSION,"skill_name":"find-quality-recovery-stocks","package_version":PACKAGE_VERSION,
        "integrity_scope":{"mode":"runtime_allowlist","root_files":sorted(RUNTIME_ROOT_FILES),"prefixes":list(RUNTIME_PREFIXES),"excluded_files":sorted(EXCLUDED),"installer_managed_prefixes":list(EXCLUDED_PREFIXES)},
        "analytical_policy_sha256":semantic_hash(analytical),"files":files,"package_content_sha256":semantic_hash(files),
    }
    return body


def write_manifest() -> dict[str, Any]:
    v=build(); write_json(MANIFEST,v); return v


def verify() -> dict[str, Any]:
    stored=json.loads(MANIFEST.read_text(encoding="utf-8")); actual=build(); errors=[]
    for field in ("schema_version","skill_name","package_version","integrity_scope","analytical_policy_sha256","files","package_content_sha256"):
        if stored.get(field)!=actual.get(field): errors.append(f"integrity mismatch: {field}")
    return {"valid":not errors,"errors":errors,"package_version":PACKAGE_VERSION,"analytical_policy_sha256":actual["analytical_policy_sha256"],"package_content_sha256":actual["package_content_sha256"]}

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("build","verify")); a=p.parse_args(); r=write_manifest() if a.command=="build" else verify(); print(json.dumps(r,ensure_ascii=False,indent=2,sort_keys=True)); raise SystemExit(0 if r.get("valid",True) else 2)
