#!/usr/bin/env python3
"""Independent verifier for QRGF v3.2 shadow Structural Quality bounds."""
from __future__ import annotations
import argparse, hashlib, json, subprocess
from pathlib import Path
from typing import Any, Mapping
import v32_quality_math as qm


def canonical(v:Any)->bytes:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def sem(v:Any)->str:return hashlib.sha256(canonical(v)).hexdigest()
def sha_file(p:Path)->str:return hashlib.sha256(p.read_bytes()).hexdigest()
def load(p:Path)->dict[str,Any]:
    v=json.loads(p.read_text(encoding="utf-8-sig"));
    if not isinstance(v,dict):raise ValueError(f"{p} must contain an object")
    return v
def git_show(root:Path,commit:str,path:str)->bytes:
    cp=subprocess.run(["git","show",f"{commit}:{path}"],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if cp.returncode!=0:raise ValueError(f"cannot read source {path} at {commit}")
    return cp.stdout

def safe_child(root:Path,path_text:str)->Path:
    p=(root/path_text).resolve(); rr=root.resolve()
    if rr not in p.parents and p!=rr:raise ValueError(f"path escapes repository: {path_text}")
    return p

def verify_release(path:Path,root:Path)->tuple[dict[str,Any],str]:
    rel=load(path); mapping={
        "build_v32_structural_shadow.py":root/"screening/engine/build_v32_structural_shadow.py",
        "verify_v32_structural_shadow.py":root/"screening/engine/verify_v32_structural_shadow.py",
        "v32_quality_math.py":root/"screening/engine/v32_quality_math.py",
        "v32-quality-bound-model.json":root/"screening/config/v32-quality-bound-model.json",
        "update-v32-shadow.yml":root/".github/workflows/update-v32-shadow.yml",
    }
    hashes=rel.get("producer_hashes") or {}
    if rel.get("schema_version")!="1.0.0" or rel.get("release_version")!="1.0.0" or set(hashes)!=set(mapping):raise ValueError("invalid v3.2 shadow producer release")
    for name,p in mapping.items():
        if not p.is_file() or sha_file(p)!=hashes[name]:raise ValueError(f"v3.2 shadow producer hash mismatch: {name}")
    return rel,sha_file(path)

def pct(n:int,d:int)->float:return round(100.0*n/d,2) if d else 0.0

def verify(root:Path,snapshot_dir:Path,model_path:Path,release_path:Path)->dict[str,Any]:
    root=root.resolve(); snap=snapshot_dir.resolve()
    if root not in snap.parents:raise ValueError("snapshot path escapes repository")
    model=load(model_path); model_sha=sem(model); release,release_sha=verify_release(release_path,root)
    if release.get("quality_bound_model_sha256")!=model_sha:raise ValueError("release/model hash mismatch")
    cert_path=snap/"certificate.json"; summary_path=snap/"summary.json"; cert=load(cert_path); summary=load(summary_path)
    cert_body={k:v for k,v in cert.items() if k!="certificate_sha256"}; sum_body={k:v for k,v in summary.items() if k!="summary_sha256"}
    if cert.get("certificate_sha256")!=sem(cert_body):raise ValueError("shadow certificate self hash mismatch")
    if summary.get("summary_sha256")!=sem(sum_body):raise ValueError("shadow summary self hash mismatch")
    if cert.get("shadow_only") is not True or cert.get("production_selection_authorized") is not False:raise ValueError("shadow authorization flags invalid")
    if summary.get("shadow_only") is not True or summary.get("production_selection_authorized") is not False:raise ValueError("shadow summary authorization flags invalid")
    if cert.get("quality_bound_model_sha256")!=model_sha or cert.get("producer_release_sha256")!=release_sha:raise ValueError("shadow certificate producer/model mismatch")
    if summary.get("quality_bound_model_sha256")!=model_sha or summary.get("producer_release_sha256")!=release_sha:raise ValueError("shadow summary producer/model mismatch")
    if cert.get("summary_sha256")!=summary.get("summary_sha256"):raise ValueError("certificate/summary mismatch")
    src=cert.get("source_v31") if isinstance(cert.get("source_v31"),Mapping) else {}; commit=str(src.get("publication_commit_sha") or "")
    v31_cert=json.loads(git_show(root,commit,f"data/v31/snapshots/{src['snapshot_id']}/market-certificate.json")); v31_manifest=json.loads(git_show(root,commit,f"data/v31/snapshots/{src['snapshot_id']}/frontier-manifest.json"))
    v31_cert_body={k:v for k,v in v31_cert.items() if k!="certificate_sha256"}; v31_man_body={k:v for k,v in v31_manifest.items() if k!="manifest_semantic_sha256"}
    if sem(v31_cert_body)!=src.get("certificate_sha256") or sem(v31_man_body)!=src.get("frontier_manifest_sha256"):raise ValueError("source v3.1 lineage mismatch")
    if int(v31_manifest.get("scope_count") or -1)!=int(src.get("scope_count") or -2):raise ValueError("source v3.1 scope count mismatch")
    if str((v31_cert.get("frontier_producer_release") or {}).get("manifest_sha256") or "")!=str(release.get("source_v31_frontier_producer_release_sha256") or ""):raise ValueError("source v3.1 producer release is not approved")
    pages=summary.get("pages") if isinstance(summary.get("pages"),list) else []; all_scopes=[]; expected_rank=1; previous=None
    for index,decl in enumerate(pages,1):
        if int(decl.get("page_index") or -1)!=index:raise ValueError("shadow page index gap")
        path=safe_child(root,str(decl.get("path") or ""))
        if snap not in path.parents:raise ValueError("shadow page escapes snapshot")
        if sha_file(path)!=decl.get("sha256"):raise ValueError("shadow page byte hash mismatch")
        page=load(path); body={k:v for k,v in page.items() if k!="page_semantic_sha256"}
        if page.get("page_semantic_sha256")!=sem(body) or page.get("page_semantic_sha256")!=decl.get("page_semantic_sha256"):raise ValueError("shadow page semantic hash mismatch")
        scopes=page.get("scopes") if isinstance(page.get("scopes"),list) else []
        if len(scopes)!=int(decl.get("scope_count") or -1):raise ValueError("shadow page scope count mismatch")
        for scope in scopes:
            if int(scope.get("shadow_rank") or -1)!=expected_rank:raise ValueError("shadow rank gap")
            facts=scope.get("machine_bound_facts") if isinstance(scope.get("machine_bound_facts"),Mapping) else {}; lanes=[str(x) for x in scope.get("possible_lanes") or []]
            q,binding,bounds=qm.quality_upper_bound(facts,lanes,model)
            if abs(q-float(scope.get("quality_upper_bound")))>1e-6 or binding!=scope.get("binding_lane"):raise ValueError("shadow quality upper bound mismatch")
            if {k:float(v) for k,v in bounds.items()}!={k:float(v) for k,v in (scope.get("lane_quality_upper_bounds") or {}).items()}:raise ValueError("shadow lane bounds mismatch")
            sec_uppers=[]
            for sec in scope.get("securities") or []:
                value=qm.progression_upper_bound(quality_upper=q,setup=sec.get("l2_setup_score"),confidence=sec.get("l2_confidence_pct"),model=model)
                if abs(value-float(sec.get("v32_shadow_upper_bound")))>1e-6:raise ValueError("shadow security upper bound mismatch")
                sec_uppers.append(value)
            scope_upper=max(sec_uppers,default=q)
            if abs(scope_upper-float(scope.get("scope_upper_bound")))>1e-6:raise ValueError("shadow scope upper bound mismatch")
            if previous is not None and scope_upper>previous+1e-9:raise ValueError("shadow scopes not globally sorted")
            previous=scope_upper; all_scopes.append(scope); expected_rank+=1
    if len(all_scopes)!=int(summary.get("scope_count") or -1) or len(all_scopes)!=int(cert.get("scope_count") or -2):raise ValueError("shadow total scope count mismatch")
    if sem(all_scopes)!=summary.get("shadow_semantic_sha256") or sem(all_scopes)!=cert.get("shadow_semantic_sha256"):raise ValueError("shadow semantic hash mismatch")
    resolved=sum(1 for r in all_scopes if r.get("cik_resolution_status")=="resolved"); factful=sum(1 for r in all_scopes if int(r.get("machine_fact_count") or 0)>0); tightened=sum(1 for r in all_scopes if float(r.get("quality_upper_bound") or 100)>=0 and float(r.get("quality_upper_bound") or 100)<99.9999)
    if resolved!=int(summary.get("cik_resolved_scope_count") or -1) or factful!=int(summary.get("machine_fact_scope_count") or -1) or tightened!=int(summary.get("quality_bound_tightened_scope_count") or -1):raise ValueError("shadow summary counts mismatch")
    expected_thresholds={str(t):sum(1 for r in all_scopes if float(r["scope_upper_bound"])>=t) for t in (99,97.5,95,92.5,90,87.5,85,82.5,80,75)}
    if expected_thresholds!={str(k):int(v) for k,v in (summary.get("final_upper_bound_threshold_counts") or {}).items()}:raise ValueError("shadow threshold distribution mismatch")
    return {"verified":True,"snapshot_id":cert["snapshot_id"],"market_session_id":cert["market_session_id"],"scope_count":len(all_scopes),"cik_resolved_scope_count":resolved,"cik_resolution_pct":pct(resolved,len(all_scopes)),"machine_fact_scope_count":factful,"quality_bound_tightened_scope_count":tightened,"final_upper_bound_threshold_counts":expected_thresholds,"final_upper_bound_at_rank":summary.get("final_upper_bound_at_rank"),"sec_request_count":summary.get("sec_request_count"),"sec_errors":summary.get("sec_errors")}

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument("--repo-root",type=Path,default=Path("."));ap.add_argument("--snapshot-dir",type=Path,required=True);ap.add_argument("--model",type=Path,default=Path("screening/config/v32-quality-bound-model.json"));ap.add_argument("--release-manifest",type=Path,default=Path("screening/config/v32-shadow-producer-release.json"));args=ap.parse_args();root=args.repo_root.resolve();snap=(root/args.snapshot_dir).resolve() if not args.snapshot_dir.is_absolute() else args.snapshot_dir.resolve();model=(root/args.model).resolve() if not args.model.is_absolute() else args.model.resolve();rel=(root/args.release_manifest).resolve() if not args.release_manifest.is_absolute() else args.release_manifest.resolve();print(json.dumps(verify(root,snap,model,rel),ensure_ascii=False,indent=2,sort_keys=True));return 0
if __name__=="__main__":raise SystemExit(main())
