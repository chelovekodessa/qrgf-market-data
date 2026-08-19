#!/usr/bin/env python3
"""V4.1 market producer smoke: authority gate and paginated manifest."""
from __future__ import annotations
import csv,json,sys,tempfile
from pathlib import Path
import build_v4_market_view as m
import promote_v4_state as p
from test_promote_v4_state import bundle,proposal

def check(condition,message):
    if not condition:raise AssertionError(message)
def write(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,sort_keys=True)+"\n")
def complete_state(master):
    body={"schema_version":"2.0.0","kind":"qrgf_v41_campaign_state","state_machine_version":"4.1.0-master500-phases-v1","master_sha256":master["master_sha256"],"master_content_sha256":master["master_content_sha256"],"selector_certificate_sha256":master["selector_certificate_sha256"],"market_session_id":master["market_session_id"],"campaign_input_sha256":"a"*64,"registry_snapshot_sha256":"b"*64,"phase":"COMPLETE","canary_scope_count":15,"canary_durable_count":15,"pilot_scope_count":50,"pilot_durable_count":50,"master_scope_count":500,"master_durable_count":500,"quality_resolved_count":500,"durable_incomplete_count":0,"canary_durable_complete":True,"runtime_reconstruction_gate_passed":True,"pilot_registry_loss_gate_passed":True,"core500_complete":True,"daily_broad_allowed":True,"next_scope_count":0,"next_scopes":[],"generated_at":"2026-08-18T00:00:00Z","producer_release_sha256":"c"*64}
    return {**body,"state_sha256":p.sem(body)}
def main():
    saved=m.verify_release;m.verify_release=lambda *_:"d"*64
    try:
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);sys.argv=["market","--repo-root",str(root),"--release-manifest","ignored"]
            check(m.main()==0,"blocked market run failed");blocked=json.loads((root/"data/v4/market/v41/latest.json").read_text());check(blocked["ordinary_daily_broad_allowed"] is False and blocked["reason"]=="MASTER_CORE500_NOT_INITIALIZED","missing MASTER was not blocked")
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);b=bundle();master=b["master"];p.publish_master(root,proposal(b),"a"*64,"2026-08-18T00:00:00Z")
            state_value=complete_state(master);state_path=root/"data/v4/campaigns"/master["master_sha256"]/"state.json";write(state_path,state_value);pointer_body={"schema_version":"2.0.0","kind":"qrgf_v41_campaign_pointer","state_path":state_path.relative_to(root).as_posix(),"state_sha256":state_value["state_sha256"],"master_sha256":master["master_sha256"],"phase":"COMPLETE","daily_broad_allowed":True,"published_at":"2026-08-18T00:00:00Z","producer_release_sha256":"a"*64};write(root/"data/v4/campaign/latest.json",{**pointer_body,"pointer_sha256":p.sem(pointer_body)})
            page=root/"data/v3/radar/page.csv";page.parent.mkdir(parents=True,exist_ok=True);fields=["ticker","contract_id","instrument_status","l2_status","l2_setup_score","l2_confidence_pct","setup_prior_growth","avg_dollar_volume"]
            with page.open("w",newline="") as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader()
                for i in range(501):writer.writerow({"ticker":f"X{i:04d}","contract_id":f"US:X{i:04d}","instrument_status":"eligible","l2_status":"pass","l2_setup_score":100-i%50,"l2_confidence_pct":80,"setup_prior_growth":20,"avg_dollar_volume":10000000})
            manifest={"complete":True,"rows":501,"pages":[{"name":"page.csv","sha256":m.sha(page)}]};write(root/"data/v3/radar/manifest.json",manifest);write(root/"data/v3/latest.json",{"complete":True,"market_session_id":"2026-08-18","snapshot_id":"fixture","radar_manifest_path":"data/v3/radar/manifest.json"})
            sys.argv=["market","--repo-root",str(root),"--release-manifest","ignored"];check(m.main()==0,"complete market build failed")
            pointer=json.loads((root/"data/v4/market/v41/latest.json").read_text());session=root/pointer["manifest_path"];manifest=json.loads(session.read_text());check(manifest["page_size"]==250 and manifest["page_count"]==3 and manifest["total_eligible_challengers"]==501,"pagination contract failed")
            p0=json.loads((session.parent/"challengers/page-0000.json").read_text());p1=json.loads((session.parent/"challengers/page-0001.json").read_text());check(not {x["research_scope_key"] for x in p0["rows"]}.intersection({x["research_scope_key"] for x in p1["rows"]}),"page overlap")
    finally:m.verify_release=saved
    print("V4.1 MARKET PRODUCER SELFTEST PASS")
    return 0
if __name__=="__main__":raise SystemExit(main())
