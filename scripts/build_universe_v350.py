#!/usr/bin/env python3
"""QRGF L0 hardening and high-recall identity wrapper.

The pinned core classifier remains conservative about explicit non-common
instruments. Rows that are neither accepted nor explicitly rejected, and would
otherwise disappear as ``instrument_resolution_required``, stay rankable under
a provisional internal identity. They MUST be resolved from an authoritative
broker/exchange identity before L3 research. This prevents silent false-negative
losses while preserving the fail-closed treatment of known warrants, preferreds,
funds, debt, SPACs and other prohibited structures.
"""

from __future__ import annotations

import re

import build_universe as core

_GENERIC_NON_ETF_FUND = re.compile(
    r"\b(?:fund|term\s+trust|target\s+term\s+trust|management\s+investment\s+company|CEF)\b",
    re.I,
)
_CEF_SPONSOR_TRUST = re.compile(
    r"\b(?:BlackRock|Nuveen|PIMCO|Eaton\s+Vance|John\s+Hancock|Cohen\s*&\s*Steers|"
    r"Gabelli|Calamos|MFS|abrdn|Western\s+Asset|Virtus|ClearBridge|Allspring|First\s+Trust|"
    r"Neuberger\s+Berman|DoubleLine|Tortoise|Kayne\s+Anderson|Tekla)\b.*"
    r"(?:\btrust\b|\bcommon\s+shares?\s+of\s+beneficial\s+interest\b)",
    re.I,
)
_NON_OPERATING_INVESTMENT = re.compile(
    r"(?:\b(?:gold|silver|platinum|palladium|bitcoin|ether|ethereum|crypto|commodity)\b.{0,50}\btrust\b|"
    r"\btrust\b.{0,50}\b(?:gold|silver|platinum|palladium|bitcoin|ether|ethereum|crypto|commodity)\b|"
    r"\b(?:BDC|business\s+development\s+(?:company|corporation))\b)",
    re.I,
)

_ORIGINAL_CLASSIFY = core.classify_row
_ORIGINAL_BUILD = core.build_universe
_PROVISIONAL_PREFIX = "qrgf-resolution-required"


def classify_row(row, include_etfs, approved_etfs=None):
    is_etf = str(row.get("ETF") or "").strip().upper() == "Y"
    name = str(row.get("Security Name") or "").strip()
    if not is_etf and (
        _GENERIC_NON_ETF_FUND.search(name)
        or _CEF_SPONSOR_TRUST.search(name)
        or _NON_OPERATING_INVESTMENT.search(name)
    ):
        return False, "unsuitable_non_etf_fund", "fund", False
    return _ORIGINAL_CLASSIFY(row, include_etfs, approved_etfs)


def build_universe(listing_path, seed_membership, include_etfs, approved_etfs=None):
    accepted, rejected, summary = _ORIGINAL_BUILD(
        listing_path, seed_membership, include_etfs, approved_etfs
    )
    rescued = []
    remaining_rejected = []
    for row in rejected:
        if str(row.get("rejection_reason") or "") != "instrument_resolution_required":
            remaining_rejected.append(row)
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        exchange = str(row.get("listing_exchange") or "US").strip() or "US"
        if not ticker:
            remaining_rejected.append(row)
            continue
        provisional = dict(row)
        provisional.pop("rejection_reason", None)
        provisional.update({
            # Common-equity is a transport placeholder only. The exact L3
            # security_type is resolved before research and may become ADR.
            "security_type": "common_equity",
            "instrument_status": "eligible",
            "rankable": True,
            "adr_flag": "N",
            "contract_id": f"{_PROVISIONAL_PREFIX}:{exchange}:{ticker}",
            "contract_id_status": "requires_authoritative_resolution_before_L3",
            "identity_resolution_required": True,
            "identity_resolution_reason": "instrument_resolution_required",
        })
        rescued.append(provisional)

    combined = accepted + rescued
    identities = [(str(row.get("ticker") or ""), str(row.get("contract_id") or "")) for row in combined]
    if len(set(identities)) != len(identities):
        raise ValueError("provisional identity rescue created a duplicate L0 identity")
    combined.sort(key=lambda row: (str(row.get("security_type") or ""), str(row.get("ticker") or "")))

    remaining_quarantine = [
        row for row in remaining_rejected
        if str(row.get("instrument_status") or "") == "resolution_required"
    ]
    summary = dict(summary)
    summary.update({
        "accepted_unique": len(combined),
        "structurally_eligible": len(combined),
        "rankable_l0": len(combined),
        "rejected_rows": len(remaining_rejected) - len(remaining_quarantine),
        "quarantined_rows": len(remaining_quarantine),
        "ambiguous_review_count": len(rescued),
        "identity_resolution_required_count": len(rescued),
        "rankable_resolution_required_count": len(rescued),
        "eligibility_resolution_complete": len(rescued) == 0 and len(remaining_quarantine) == 0,
        "common_equity_count": sum(row.get("security_type") == "common_equity" for row in combined),
        "adr_count": sum(row.get("adr_flag") == "Y" for row in combined),
        "approved_etfs_accepted": sum(row.get("security_type") == "etf" for row in combined),
        "complete": True,
    })
    return combined, remaining_rejected, summary


core.classify_row = classify_row
core.build_universe = build_universe


if __name__ == "__main__":
    raise SystemExit(core.main())
