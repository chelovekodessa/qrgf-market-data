#!/usr/bin/env python3
"""Fail-closed V4.2 policy invariants."""
from __future__ import annotations

from typing import Any
from common import ensure, load_connectors, load_policy, semantic_hash


def _hash_shape(value: Any, label: str) -> None:
    text = str(value or "")
    ensure(len(text) == 64 and all(ch in "0123456789abcdef" for ch in text) and text != "0" * 64, f"{label} must be a nonzero lowercase SHA-256")


def validate() -> dict[str, Any]:
    p = load_policy()
    c = load_connectors()
    ensure(p.get("schema_version") == "2.0.0", "V4.2 policy schema mismatch")
    arch = p.get("architecture") or {}
    ensure(arch.get("version") == "4.2.0", "V4.2 architecture version mismatch")
    ensure(arch.get("release_version") == "4.2.3", "V4.2.3 release version mismatch")
    ensure(arch.get("mode") == "core500_quality_registry_plus_full_market_challengers", "V4.2 architecture mode mismatch")
    for key in (
        "legacy_production_paths_forbidden",
        "core500_is_research_bootstrap_not_whitelist",
        "registry_is_cache_not_universe_gate",
        "unknown_never_becomes_fail_by_missingness",
        "durable_registry_readback_required_for_bootstrap_completion",
        "full_market_challenger_scan_required",
        "publisher_recomputes_master_from_evidence",
        "market_membership_index_required",
        "official_issuer_identity_required_for_master",
        "connector_attestation_is_not_external_signature",
        "v41_is_historical_read_only",
        "deployment_release_manifest_required",
        "remote_release_must_match_pinned_hash",
    ):
        ensure(arch.get(key) is True, f"V4.2 architecture invariant changed: {key}")

    ensure(p["strategy"]["target_recovery_pct"] == [5.0, 7.0] and p["strategy"]["target_horizon_trading_days"] == [10, 50], "strategy target changed")
    ensure((p["universe"]["minimum_price_usd"], p["universe"]["minimum_average_dollar_volume_usd"], p["universe"]["minimum_known_market_cap_usd"]) == (3.0, 2_000_000, 250_000_000), "universe floor changed")
    ensure((p["l3"]["minimum_quality_score"], p["l3"]["minimum_coverage_pct"], p["l3"]["pass_coverage_pct"]) == (65, 70, 85), "L3 thresholds changed")
    ensure((p["l4"]["minimum_score"], p["l4"]["minimum_coverage_pct"], p["l4"]["pass_coverage_pct"]) == (60, 60, 80), "L4 thresholds changed")
    ensure(p["waves"]["L3"] == 4 and p["bootstrap"]["wave_size"] == 4, "deep Structural Quality wave must remain four")

    bootstrap = p["bootstrap"]
    ensure(bootstrap["cohort_size"] == 500 and bootstrap["master_core500_exact_size"] == 500, "Core500 size changed")
    for key in (
        "master_core500_immutable",
        "selector_certificate_required",
        "selector_certificate_must_bind_cohort",
        "quality_candidate_source_required",
        "current_recovery_field_recursive_rejection",
        "current_price_or_recovery_fields_forbidden",
        "quality_score_not_final_until_deep_structural_research",
        "publisher_recompute_required",
        "arbitrary_candidate_list_forbidden",
        "official_issuer_identity_required",
        "query_lane_membership_must_be_computed_from_receipts",
        "insufficient_data_is_quality_unknown",
        "insufficient_data_is_competitive_unresolved",
    ):
        ensure(bootstrap.get(key) is True, f"V4.2 bootstrap invariant changed: {key}")
    ensure(bootstrap["candidate_source"] == "verified_radar_membership_plus_official_identity_plus_metricduck_classification_and_quality_plan_plus_approved_etf_catalog", "V4.2 candidate source changed")
    ensure(bootstrap["selection_model_version"] == "4.2.3-bootstrap-provenance-v4", "V4.2 bootstrap model changed")
    ensure(set(bootstrap["lane_score_weights"]) == {"established_quality", "recognized_growth", "cyclical", "bank", "etf"}, "V4.2 bootstrap lane set changed")
    plan = bootstrap["metricduck_query_plan"]
    ensure(plan["connector_tool"] == "screen_companies" and plan["connector_contract_version"] == "2026-08-22", "MetricDuck connector contract version changed")
    ensure(plan["connector_max_rows_per_query"] == 50 and plan["partition_dimension"] == "market_cap" and plan["connector_sort_by"] == "market_cap", "MetricDuck query-plan transport contract changed")
    ensure(plan["saturated_leaf_forbidden"] is True and plan["connector_trust_class"] == "connector_attested" and plan["external_cryptographic_signature_available"] is False, "MetricDuck query-plan trust contract changed")
    ensure(plan["legacy_internal_field_filters_forbidden"] is True and plan["unsupported_metrics_must_remain_unknown"] is True, "MetricDuck unsupported-field boundary weakened")
    ensure(plan["classification_catalog_required"] is True and plan["classification_catalog_scope"] == "global_sectorless_market_cap_partitioned", "MetricDuck classification catalog contract changed")
    ensure(plan["radar_sector_routing_forbidden"] is True and plan["global_sectorless_screen_supported"] is True, "Radar classification dependency reintroduced")
    ensure(plan["transport_limit_is_not_universe_cutoff"] is True and bootstrap["market_transport_limit_is_not_universe_cutoff"] is True, "MetricDuck transport limit became a universe cutoff")
    ensure(set(plan["query_sector_codes"]) == {"TECH","FIN","HEALTH","CONS_STAPLES","CONS_DISC","IND","ENERGY","UTIL","RE","MAT","COMM"}, "MetricDuck query sector-code set changed")
    ensure(plan.get("returned_sector_code_semantics") == "connector_attested_open_enum", "MetricDuck returned sector taxonomy must remain provider-attested and open")
    ensure(plan.get("returned_sector_code_pattern") == "^[A-Z][A-Z0-9_]*$", "MetricDuck returned sector-code syntax contract changed")
    ensure(plan.get("sector_filtered_query_requires_exact_returned_code") is True, "MetricDuck filtered sector/result equality contract weakened")
    ensure(bootstrap["radar_sector_industry_market_cap_optional"] is True and bootstrap["radar_cik_optional"] is True, "Radar optional-field contract changed")
    ensure(bootstrap["connector_classification_binding_required_for_operating_candidates"] is True and bootstrap["classification_must_not_be_model_guessed"] is True, "classification binding invariant weakened")
    ensure(set(plan["screen_lanes"]) == {"established_quality", "recognized_growth", "cyclical", "bank"}, "MetricDuck screen lane set changed")
    ensure(plan["etf_candidate_source"] == "approved_etf_catalog_plus_pinned_market_membership", "ETF discovery source changed")
    profiles = plan["lane_discovery_profiles"]
    ensure(profiles["established_quality"]["filters"] == [{"metric_id":"roic","operator":"gte","value":0.12,"period_type":"ttm"}], "established-quality MetricDuck profile changed")
    ensure(profiles["recognized_growth"]["filters"] == [{"metric_id":"revenues","operator":"gte","value":0.12,"period_type":"ttm.cagr3"}], "growth MetricDuck profile changed")
    ensure(profiles["bank"]["filters"] == [{"metric_id":"roa","operator":"gte","value":0.005,"period_type":"ttm"}] and profiles["bank"]["sector_codes"] == ["FIN"] and profiles["bank"].get("required_tags") == ["financial_services_traditional"], "bank MetricDuck profile changed")
    ensure(profiles["cyclical"]["filters"] == [] and set(profiles["cyclical"]["sector_codes"]) == {"ENERGY","MAT"}, "cyclical MetricDuck profile changed")
    unsupported = set(plan["not_screenable_as_exact_lane_filters"])
    ensure({"fcf_margin_pct","net_debt_to_ebitda","cet1_ratio_pct","fund_aum","fund_structure_quality_score"}.issubset(unsupported), "unsupported MetricDuck metric registry incomplete")
    ensure(set(bootstrap["quality_resolved_statuses"]) == {"pass", "conditional", "rejected"}, "quality resolution statuses changed")
    ensure(set(bootstrap["competitive_resolved_statuses"]) == {"pass", "conditional", "rejected"}, "competitive resolution statuses changed")

    campaign = p["campaign"]
    ensure(campaign["state_machine_version"] == "4.2.0-master500-phases-v2", "campaign state model changed")
    ensure(campaign["phases"] == ["CANARY", "PILOT", "CORE500", "COMPLETE"], "campaign phase order changed")
    ensure((campaign["canary_scope_count"], campaign["canary_minimum_scope_count"], campaign["canary_maximum_scope_count"], campaign["pilot_scope_count"]) == (15, 12, 20, 50), "campaign cohort gates changed")
    for key in (
        "runtime_reconstruction_gate_required",
        "pilot_registry_loss_gate_required",
        "pilot_reuse_gate_required",
        "daily_broad_requires_complete",
        "state_must_be_reconstructible_from_github",
        "gates_must_be_computed_by_clean_github_run",
        "manual_gate_boolean_input_forbidden",
        "clean_checkout_required",
        "local_state_use_forbidden",
        "gate_input_commit_required",
        "pilot_blocked_state_reconstructed_not_quality_reused",
    ):
        ensure(campaign.get(key) is True, f"V4.2 campaign invariant changed: {key}")

    challenger = p["challenger_lane"]
    ensure(challenger["daily_market_challenger_transport_ceiling"] == 250 and challenger["transport_page_size"] == 250, "challenger page size changed")
    for key in (
        "pinned_session_manifest_required",
        "cursor_continuation_required",
        "page_transport_is_not_universe_cutoff",
        "transport_progress_independent_of_deep_resolution",
        "competitive_frontier_required",
        "insufficient_data_remains_competitive_unknown",
        "durable_triage_required_before_cursor_advance",
        "safe_exclusion_requires_proven_reason",
    ):
        ensure(challenger.get(key) is True, f"V4.2 challenger invariant changed: {key}")
    ensure(challenger["deep_research_wave_size"] == 4, "challenger deep-research wave changed")

    ensure(p["selection"]["l3_to_l4"]["quality_tier_is_primary"] is True and p["selection"]["l3_to_l4"]["weighted_cross_tier_compensation_forbidden"] is True, "L3 selection must be quality-tier first")
    ensure(p["selection"]["l4_to_final"]["quality_tier_is_primary"] is True and p["selection"]["l4_to_final"]["weighted_cross_tier_compensation_forbidden"] is True, "L4 selection must be quality-tier first")
    ensure(p["selection"]["quality_tiers"] == {"A": [85, 100], "B": [78, 84.9999], "C": [70, 77.9999], "D": [65, 69.9999]}, "quality tiers changed")

    registry = p["quality_registry"]
    ensure(registry["quality_policy_version"] == "4.0.0-structural-v1", "Quality policy mismatch")
    for key in (
        "read_after_write_required_for_bootstrap",
        "immutable_passport",
        "mutable_freshness_pointer",
        "registry_is_cache_not_correctness_dependency",
        "proposal_journal_append_only",
        "receipt_is_idempotency_key",
        "replay_must_preserve_original_receipt",
        "progress_requires_current_receipt",
        "proposal_hash_must_not_define_recency",
        "same_logical_version_conflict_fails_closed",
        "partial_state_receipt_recovery_requires_exact_pointer_match",
        "insufficient_data_requires_next_review_date",
    ):
        ensure(registry.get(key) is True, f"Registry invariant changed: {key}")

    evidence = p["evidence"]
    for key in ("model_judgment_must_be_explicit", "subjective_score_rationale_required", "contrary_evidence_field_required", "assessment_uncertainty_required"):
        ensure(evidence.get(key) is True, f"assessment evidence invariant changed: {key}")
    validation = p["validation_framework"]
    ensure(validation["minimum_regression_expectations"] >= 5, "minimum regression expectations weakened")
    ensure(validation["pilot_registry_loss_tolerance"] == 0 and validation["legacy_registry_scope_loss_tolerance"] == 0, "Registry loss tolerance changed")
    for key in ("negative_provenance_tests_required", "remote_recompute_equality_required", "clean_checkout_reconstruction_required", "deployment_overlay_hash_check_required"):
        ensure(validation.get(key) is True, f"V4.2 validation invariant changed: {key}")

    primary = p["primary_evidence"]
    ensure(primary["standard_github_hosted_direct_sec_is_not_required"] is True, "standard GitHub SEC must not be a production dependency")
    ensure(primary["metricduck_is_approved_sec_filing_transport"] is True and primary["metricduck_requires_accession_or_edgar_lineage"] is True, "MetricDuck lineage boundary changed")

    ensure(c.get("schema_version") == "2.0.0", "V4.2 connectors schema mismatch")
    master = c["master_core500_v42"]
    ensure(master["latest_pointer_path"] == "data/v42/master-core500/latest.json" and master["campaign_latest_pointer_path"] == "data/v42/campaign/latest.json", "V4.2 authority pointers changed")
    ensure(master["masters_prefix"] == "data/v42/master-core500/masters" and master["sources_prefix"] == "data/v42/master-core500/sources" and master["certificates_prefix"] == "data/v42/master-core500/certificates" and master["campaign_prefix"] == "data/v42/campaigns", "V4.2 immutable state paths changed")
    ensure(master["bootstrap_checkpoint_prefix"] == "data/v42/master-core500/bootstrap/checkpoints" and master["bootstrap_latest_pointer_path"] == "data/v42/master-core500/bootstrap/latest.json", "V4.2 bootstrap checkpoint authority paths changed")
    ensure(master["publisher_recomputes_master_from_evidence"] is True, "V4.2 publisher recomputation disabled")
    _hash_shape(master["expected_producer_release_sha256"], "V4.2 state producer release hash")

    market = c["market_view_v42"]
    ensure(market["challenger_page_size"] == 250 and market["challenger_transport_is_not_quality_whitelist"] is True, "V4.2 challenger page contract changed")
    ensure(market["latest_pointer_path"] == "data/v42/market/latest.json" and market["session_prefix"] == "data/v42/market/sessions" and market["frontier_prefix"] == "data/v42/market/frontiers", "V4.2 market paths changed")
    _hash_shape(market["expected_producer_release_sha256"], "V4.2 market producer release hash")
    ensure(c["market_radar"]["full_market_materialization_in_chatgpt_forbidden"] is True, "full market must stay producer-side")

    registry_connector = c["quality_registry_v4"]
    for key in ("single_writer_required", "read_after_write_required_for_bootstrap", "proposal_journal_append_only", "receipt_is_idempotency_key", "replay_safe_required", "progress_requires_current_receipt"):
        ensure(registry_connector.get(key) is True, f"Registry connector invariant changed: {key}")
    ensure(registry_connector["producer_release_path"] == master["producer_release_path"] and registry_connector["expected_producer_release_sha256"] == master["expected_producer_release_sha256"], "shared state producer release binding mismatch")
    metricduck = c["primary_evidence"]["metricduck"]
    ensure(metricduck["cross_company_screen_trust_class"] == "connector_attested" and metricduck["external_cryptographic_signature_available"] is False, "MetricDuck trust class changed")
    ensure(metricduck["complete_leaf_requires_matched_equals_returned"] is True and metricduck["connector_max_rows_per_query"] == 50, "MetricDuck complete-leaf contract changed")
    ensure(c["primary_evidence"]["github_direct_sec"]["production_required"] is False, "GitHub direct SEC unexpectedly required")
    ensure(set(p["final_decision"]["allowed_statuses"]) == {"open_now", "prepare_limit_order", "wait", "do_not_enter", "do_not_consider"}, "final decision statuses changed")
    return {
        "valid": True,
        "architecture_version": "4.2.0",
        "policy_sha256": semantic_hash(p),
        "connectors_sha256": semantic_hash(c),
        "selection_model_version": p["selection"]["model_version"],
        "bootstrap_model_version": bootstrap["selection_model_version"],
        "campaign_state_machine_version": campaign["state_machine_version"],
    }
