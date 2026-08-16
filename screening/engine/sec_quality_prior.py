#!/usr/bin/env python3
"""Point-in-time SEC CompanyFacts quality prior for the QRGF L2 cutoff band.

Enrich only candidates that can mathematically affect the final L2 top-K after
the configured positive-only quality rescue bonus. Missing SEC facts never
become a neutral or zero quality score: unknown quality receives no bonus and
keeps the market-setup score unchanged.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
DEFAULT_USER_AGENT = "qrgf-market-data/3.0 qrgf-market-data-bot@users.noreply.github.com"
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
INSTANT_FORMS = ANNUAL_FORMS | {"10-Q", "10-Q/A", "6-K", "6-K/A"}

CONCEPTS = {
    "revenue": (
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "Revenues"),
        ("us-gaap", "SalesRevenueNet"),
        ("ifrs-full", "Revenue"),
    ),
    "profit": (
        ("us-gaap", "NetIncomeLoss"),
        ("us-gaap", "ProfitLoss"),
        ("ifrs-full", "ProfitLoss"),
    ),
    "operating_cash_flow": (
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ),
    "equity": (
        ("us-gaap", "StockholdersEquity"),
        ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ("ifrs-full", "Equity"),
    ),
}
QUALITY_WEIGHTS = {
    "profitability": 35.0,
    "operating_cash_flow": 30.0,
    "revenue_trend": 25.0,
    "positive_equity": 10.0,
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_date(value: Any) -> dt.date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            serial: dict[str, Any] = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    serial[key] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                elif value is None:
                    serial[key] = ""
                else:
                    serial[key] = value
            writer.writerow(serial)


class SecClient:
    def __init__(self, user_agent: str, min_interval_seconds: float = 0.12) -> None:
        self.user_agent = user_agent
        self.min_interval_seconds = max(0.10, float(min_interval_seconds))
        self._last_request = 0.0
        self.requests = 0
        self.errors: list[dict[str, Any]] = []

    def get_json(self, url: str, attempts: int = 4) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(attempts):
            delay = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                self._last_request = time.monotonic()
                self.requests += 1
                with urllib.request.urlopen(request, timeout=45) as response:  # nosec B310 - fixed SEC endpoints
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("SEC response root is not an object")
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                    time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    continue
                break
        self.errors.append({"url": url, "error": type(last_error).__name__ if last_error else "unknown"})
        raise RuntimeError(f"SEC request failed: {url}: {last_error}")


def ticker_to_cik(payload: dict[str, Any]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    fields = payload.get("fields")
    data = payload.get("data")
    if isinstance(fields, list) and isinstance(data, list):
        normalized = [str(item).strip().lower() for item in fields]
        try:
            cik_i = normalized.index("cik")
            ticker_i = normalized.index("ticker")
        except ValueError as exc:
            raise ValueError("SEC ticker map lacks cik/ticker fields") from exc
        for row in data:
            if not isinstance(row, list) or len(row) <= max(cik_i, ticker_i):
                continue
            ticker = str(row[ticker_i] or "").strip().upper()
            try:
                cik = int(row[cik_i])
            except (TypeError, ValueError):
                continue
            if ticker:
                result.setdefault(ticker, []).append(cik)
        return result
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        ticker = str(value.get("ticker") or "").strip().upper()
        cik_raw = value.get("cik_str", value.get("cik"))
        try:
            cik = int(cik_raw)
        except (TypeError, ValueError):
            continue
        if ticker:
            result.setdefault(ticker, []).append(cik)
    return result


def _eligible_fact(item: Any, as_of: dt.date, forms: set[str], *, annual: bool) -> bool:
    if not isinstance(item, dict):
        return False
    value = number(item.get("val"))
    filed = parse_date(item.get("filed"))
    end = parse_date(item.get("end"))
    form = str(item.get("form") or "").strip().upper()
    if value is None or filed is None or end is None or filed > as_of or end > as_of or form not in forms:
        return False
    if not annual:
        return True
    start = parse_date(item.get("start"))
    if start is None:
        return False
    duration = (end - start).days
    return 300 <= duration <= 430


def _series_for_concept(
    companyfacts: dict[str, Any],
    concepts: Iterable[tuple[str, str]],
    as_of: dt.date,
    *,
    annual: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    facts = companyfacts.get("facts")
    if not isinstance(facts, dict):
        return [], None
    best: tuple[tuple[int, dt.date, int], list[dict[str, Any]], dict[str, Any]] | None = None
    forms = ANNUAL_FORMS if annual else INSTANT_FORMS
    for priority, (taxonomy, concept) in enumerate(concepts):
        tax = facts.get(taxonomy)
        if not isinstance(tax, dict):
            continue
        node = tax.get(concept)
        if not isinstance(node, dict):
            continue
        units = node.get("units")
        if not isinstance(units, dict):
            continue
        for unit, raw_rows in units.items():
            if not isinstance(raw_rows, list):
                continue
            eligible = [dict(item) for item in raw_rows if _eligible_fact(item, as_of, forms, annual=annual)]
            if not eligible:
                continue
            by_end: dict[dt.date, dict[str, Any]] = {}
            for item in eligible:
                end = parse_date(item.get("end"))
                filed = parse_date(item.get("filed"))
                assert end is not None and filed is not None
                old = by_end.get(end)
                if old is None or parse_date(old.get("filed")) <= filed:
                    by_end[end] = item
            series = [by_end[key] for key in sorted(by_end)]
            latest_end = parse_date(series[-1].get("end"))
            assert latest_end is not None
            key = (len(series), latest_end, -priority)
            meta = {"taxonomy": taxonomy, "concept": concept, "unit": unit}
            if best is None or key > best[0]:
                best = (key, series, meta)
    return (best[1], best[2]) if best is not None else ([], None)


def _value(item: dict[str, Any] | None) -> float | None:
    return number((item or {}).get("val"))


def profitability_score(profit: float | None, revenue: float | None) -> float | None:
    if profit is None:
        return None
    if revenue is None or revenue <= 0:
        return 80.0 if profit > 0 else 10.0
    margin = 100.0 * profit / revenue
    if margin >= 15:
        return 100.0
    if margin >= 8:
        return 92.0
    if margin >= 3:
        return 82.0
    if margin > 0:
        return 72.0
    if margin >= -3:
        return 35.0
    if margin >= -10:
        return 15.0
    return 0.0


def operating_cash_flow_score(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 0:
        return 100.0
    if value == 0:
        return 50.0
    return 0.0


def revenue_trend_score(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous is None or previous <= 0:
        return None
    growth = 100.0 * (latest / previous - 1.0)
    if growth >= 20:
        return 100.0
    if growth >= 10:
        return 92.0
    if growth >= 5:
        return 85.0
    if growth >= 0:
        return 75.0
    if growth >= -5:
        return 55.0
    if growth >= -15:
        return 25.0
    return 0.0


def positive_equity_score(value: float | None) -> float | None:
    if value is None:
        return None
    return 100.0 if value > 0 else 0.0


def derive_quality_prior(companyfacts: dict[str, Any], as_of: dt.date) -> dict[str, Any]:
    revenue, revenue_meta = _series_for_concept(companyfacts, CONCEPTS["revenue"], as_of, annual=True)
    profit, profit_meta = _series_for_concept(companyfacts, CONCEPTS["profit"], as_of, annual=True)
    cfo, cfo_meta = _series_for_concept(companyfacts, CONCEPTS["operating_cash_flow"], as_of, annual=True)
    equity, equity_meta = _series_for_concept(companyfacts, CONCEPTS["equity"], as_of, annual=False)

    revenue_latest = _value(revenue[-1]) if revenue else None
    revenue_previous = _value(revenue[-2]) if len(revenue) >= 2 else None
    profit_latest = _value(profit[-1]) if profit else None
    cfo_latest = _value(cfo[-1]) if cfo else None
    equity_latest = _value(equity[-1]) if equity else None

    components = {
        "profitability": profitability_score(profit_latest, revenue_latest),
        "operating_cash_flow": operating_cash_flow_score(cfo_latest),
        "revenue_trend": revenue_trend_score(revenue_latest, revenue_previous),
        "positive_equity": positive_equity_score(equity_latest),
    }
    known_weight = sum(QUALITY_WEIGHTS[name] for name, value in components.items() if value is not None)
    weighted = sum(QUALITY_WEIGHTS[name] * float(value) for name, value in components.items() if value is not None)
    coverage = round(100.0 * known_weight / sum(QUALITY_WEIGHTS.values()), 2)
    score = round(weighted / known_weight, 2) if known_weight else None

    relevant: list[dt.date] = []
    for series in (revenue[-2:], profit[-1:], cfo[-1:], equity[-1:]):
        for item in series:
            filed = parse_date(item.get("filed"))
            if filed is not None:
                relevant.append(filed)
    latest_filing = max(relevant).isoformat() if relevant else None
    required_core = components["profitability"] is not None and components["revenue_trend"] is not None
    active = bool(required_core and coverage >= 60.0 and score is not None)
    return {
        "quality_prior_score": score if active else None,
        "quality_prior_coverage_pct": coverage,
        "quality_prior_status": "usable" if active else "insufficient_coverage",
        "quality_prior_components": components,
        "quality_prior_as_of": as_of.isoformat(),
        "quality_prior_filing_date": latest_filing,
        "quality_prior_source": "sec_companyfacts",
        "quality_prior_model_version": "1.0.0",
        "quality_prior_concepts": {
            "revenue": revenue_meta,
            "profit": profit_meta,
            "operating_cash_flow": cfo_meta,
            "equity": equity_meta,
        },
    }


def quality_rescue_bonus(
    setup_score: float | None,
    quality_score: float | None,
    coverage_pct: float | None,
    config: dict[str, Any],
) -> float:
    if setup_score is None or quality_score is None or coverage_pct is None:
        return 0.0
    min_coverage = float(config.get("minimum_coverage_pct", 60.0))
    floor = float(config.get("minimum_quality_score", 70.0))
    max_bonus = float(config.get("max_bonus_points", 2.0))
    if coverage_pct < min_coverage or quality_score <= floor or max_bonus <= 0:
        return 0.0
    quality_fraction = clamp((quality_score - floor) / max(1e-9, 100.0 - floor), 0.0, 1.0)
    confidence = clamp(coverage_pct / 100.0, 0.0, 1.0)
    return round(max_bonus * quality_fraction * confidence, 4)


def candidate_cohort(base_l2: dict[str, Any], keep: int, max_bonus_points: float) -> tuple[set[tuple[str, str]], float]:
    rows = [dict(row) for row in (base_l2.get("all_results") or []) if isinstance(row, dict)]
    eligible = [
        row for row in rows
        if str(row.get("l2_status") or "") in {"pass", "conditional", "recheck"}
        and number(row.get("l2_setup_score")) is not None
    ]
    if len(eligible) < keep:
        raise ValueError(f"base L2 has only {len(eligible)} rankable candidates for keep={keep}")
    cutoff = float(eligible[keep - 1]["l2_setup_score"])
    floor = cutoff - float(max_bonus_points)
    selected = {
        (str(row.get("ticker") or "").strip().upper(), str(row.get("contract_id") or "").strip())
        for row in eligible
        if float(row.get("l2_setup_score")) >= floor
    }
    return selected, cutoff


def enrich_rows(
    rows: list[dict[str, str]],
    base_l2: dict[str, Any],
    rules: dict[str, Any],
    client: SecClient,
    *,
    keep: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rescue_cfg = dict(rules.get("quality_rescue") or {})
    max_bonus = float(rescue_cfg.get("max_bonus_points", 2.0))
    cohort, cutoff = candidate_cohort(base_l2, keep, max_bonus)
    as_of_values = {parse_date(row.get("as_of")) for row in rows if parse_date(row.get("as_of")) is not None}
    if not as_of_values:
        raise ValueError("candidate CSV has no as_of date")
    as_of = max(as_of_values)

    mapping = ticker_to_cik(client.get_json(SEC_TICKERS_URL))
    by_key = {
        (str(row.get("ticker") or "").strip().upper(), str(row.get("contract_id") or "").strip()): row
        for row in rows
    }
    requested = mapped = fetched = usable = ambiguous_cik = 0
    quality_errors: list[dict[str, Any]] = []
    cache: dict[int, dict[str, Any]] = {}

    for key in sorted(cohort):
        row = by_key.get(key)
        if row is None:
            continue
        requested += 1
        ticker = key[0]
        ciks = sorted(set(mapping.get(ticker) or []))
        if len(ciks) != 1:
            if len(ciks) > 1:
                ambiguous_cik += 1
            row["quality_prior_status"] = "cik_unresolved"
            row["quality_prior_source"] = "sec_companyfacts"
            row["quality_prior_as_of"] = as_of.isoformat()
            continue
        cik = ciks[0]
        mapped += 1
        try:
            facts = cache.get(cik)
            if facts is None:
                facts = client.get_json(SEC_COMPANYFACTS_URL.format(cik=cik))
                cache[cik] = facts
                fetched += 1
            prior = derive_quality_prior(facts, as_of)
            row.update(prior)
            row["quality_prior_cik"] = str(cik).zfill(10)
            if prior.get("quality_prior_score") is not None:
                usable += 1
        except Exception as exc:
            row["quality_prior_status"] = "provider_error"
            row["quality_prior_source"] = "sec_companyfacts"
            row["quality_prior_as_of"] = as_of.isoformat()
            quality_errors.append({"ticker": ticker, "cik": cik, "error": type(exc).__name__})

    provider_success = (fetched / mapped * 100.0) if mapped else 100.0
    minimum_provider_success = float(rescue_cfg.get("minimum_provider_success_pct", 90.0))
    if mapped and provider_success < minimum_provider_success:
        raise RuntimeError(f"SEC quality provider success {provider_success:.2f}% below {minimum_provider_success:.2f}%")

    summary = {
        "schema_version": "1.0.0",
        "model_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "source": "sec_companyfacts",
        "selection_cutoff_setup_score": round(cutoff, 4),
        "max_quality_rescue_bonus_points": max_bonus,
        "enrichment_floor_setup_score": round(cutoff - max_bonus, 4),
        "cohort_size": len(cohort),
        "requested_rows": requested,
        "mapped_unique_cik": mapped,
        "ambiguous_cik": ambiguous_cik,
        "companyfacts_fetched": fetched,
        "usable_quality_prior": usable,
        "provider_success_pct": round(provider_success, 2),
        "sec_request_count": client.requests,
        "provider_errors": quality_errors,
    }
    return [dict(row) for row in rows], summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="L1 finalists CSV")
    parser.add_argument("--base-l2", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--min-interval-seconds", type=float, default=0.12)
    args = parser.parse_args()
    if not 1 <= args.keep <= 120:
        parser.error("keep must be between 1 and 120")
    client = SecClient(args.user_agent, args.min_interval_seconds)
    rows, summary = enrich_rows(
        read_csv(args.input),
        load_json(args.base_l2),
        load_json(args.rules),
        client,
        keep=args.keep,
    )
    write_csv(args.output, rows)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
