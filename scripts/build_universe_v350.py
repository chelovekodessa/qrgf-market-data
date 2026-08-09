#!/usr/bin/env python3
"""QRGF 3.5 hardening wrapper for the L0 Nasdaq Trader classifier.

The legacy classifier correctly rejects explicit closed-end/term/income funds,
but some investment companies publish names ending in "Common Stock" or
"Common Shares of Beneficial Interest" and can therefore look like operating
common equity. This wrapper rejects generic non-ETF funds, known CEF-sponsor
trust/beneficial-interest products, commodity/crypto trusts and explicit
business-development companies before delegating every other rule to the
pinned core builder.
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


core.classify_row = classify_row


if __name__ == "__main__":
    raise SystemExit(core.main())
