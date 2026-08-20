#!/usr/bin/env python3
"""Independent Opportunity and intrinsic Risk scoring."""

from __future__ import annotations

from typing import Any, Mapping

from common import clamp, load_policy, number, weighted_score
import selection


def _range_label(score: float, ranges: Mapping[str, list[float]]) -> str:
    for label, bounds in ranges.items():
        if float(bounds[0]) <= score <= float(bounds[1]):
            return label
    raise ValueError(f"score {score} is outside configured ranges")


def score_dimensions(
    opportunity_components: Mapping[str, Any],
    risk_components: Mapping[str, Any],
) -> dict[str, Any]:
    policy = load_policy()["dimensions"]
    opportunity, opportunity_coverage = weighted_score(opportunity_components, policy["opportunity"])
    risk, risk_coverage = weighted_score(risk_components, policy["intrinsic_risk"])
    minimum = float(policy["minimum_classification_coverage_pct"])
    opportunity_class = policy["unknown_class"]
    if opportunity is not None and opportunity_coverage >= minimum:
        opportunity_class = _range_label(opportunity, policy["opportunity_classes"])

    # Unknown risk is never treated as zero. The upper bound assumes every
    # missing component is maximally risky and is the ranking-safe value.
    risk_weights = policy["intrinsic_risk"]
    known_numerator = 0.0
    missing_weight = 0.0
    for name, raw_weight in risk_weights.items():
        weight = float(raw_weight)
        value = number(risk_components.get(name))
        if value is None:
            missing_weight += weight
        else:
            known_numerator += clamp(value) * weight
    total_weight = sum(float(value) for value in risk_weights.values())
    conservative_risk = round((known_numerator + 100.0 * missing_weight) / total_weight, 2)
    risk_band = _range_label(conservative_risk, policy["risk_bands"])

    return {
        "opportunity_score": opportunity,
        "opportunity_coverage_pct": opportunity_coverage,
        "opportunity_class": opportunity_class,
        "intrinsic_risk_score": risk,
        "intrinsic_risk_coverage_pct": risk_coverage,
        "intrinsic_risk_conservative_upper_bound": conservative_risk,
        "intrinsic_risk_band": risk_band,
        "opportunity_components": dict(opportunity_components),
        "intrinsic_risk_components": dict(risk_components),
        "account_context_used": False,
    }


def research_rank_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Compatibility facade over the one canonical progression ranking module."""
    return selection.research_rank_key(row)
