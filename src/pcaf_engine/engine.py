"""
Module 2 — PCAF Emissions Engine
=================================
Computes attribution factors, financed emissions, and portfolio-level
metrics (absolute, WACI, economic intensity, DQ summary) for all 7
PCAF asset classes.

References
----------
- PCAF Global GHG Accounting & Reporting Standard (2022) §5, §10
- GFANZ Transition Finance Metrics Framework (2023) Table 10.5
- GHG Protocol Corporate Value Chain (Scope 3) Standard

Key formulas
------------
Attribution factor  = outstanding_amount / PCAF_denominator          [PCAF §5.1]
Financed emissions  = attribution_factor × entity_total_emissions     [PCAF §5.2]
WACI                = Σ (weight_i × emission_intensity_i)             [PCAF §10.3]
                    where weight_i   = outstanding_i / total_AUM
                    and   intensity_i = emissions_i / revenue_i  (tCO2e / $M revenue)
Economic intensity  = total_financed_emissions / total_AUM            [PCAF §10.3]
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import Optional

from src.models import (
    AssetClass,
    DataQualityScore,
    EmissionsRecord,
    Portfolio,
    PortfolioHolding,
    PortfolioEmissionsResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine configuration
# ---------------------------------------------------------------------------

@dataclass
class EngineConfig:
    """
    Controls scope inclusion and edge-case handling for the engine run.

    Parameters
    ----------
    include_scope_1 : Include Scope 1 in financed emissions totals.
    include_scope_2 : Include Scope 2 in financed emissions totals.
    include_scope_3 : Include Scope 3 in financed emissions totals.
    scope_3_upstream_only : When True, only upstream Scope 3 is included
        (useful when downstream is unavailable or excluded by policy).
    warn_missing_emissions : Emit a UserWarning when a holding has no
        matching emissions record.
    warn_missing_denominator : Emit a UserWarning when the PCAF
        attribution denominator cannot be resolved for a holding.
    min_dq_score_for_waci : Exclude holdings with a weighted DQ score
        above this threshold from WACI calculation.  Set to 5 to include
        all.  Useful to avoid WACI being distorted by heavily estimated
        data.  Default: 5 (include all).
    """
    include_scope_1: bool = True
    include_scope_2: bool = True
    include_scope_3: bool = True
    scope_3_upstream_only: bool = False
    warn_missing_emissions: bool = True
    warn_missing_denominator: bool = True
    min_dq_score_for_waci: int = 5          # 1–5; exclude if weighted DQ > this


# ---------------------------------------------------------------------------
# Per-holding result (internal, enriched representation)
# ---------------------------------------------------------------------------

@dataclass
class HoldingEmissionsResult:
    """
    Intermediate per-holding output produced by the engine before
    aggregation into PortfolioEmissionsResult.

    Retained so callers can inspect holding-level attribution factors,
    DQ scores, and error margins without re-running the engine.
    """
    holding_id: str
    entity_name: str
    asset_class: AssetClass
    outstanding_amount_usd: float

    # Attribution
    attribution_factor: Optional[float]     # outstanding / denominator
    attribution_denominator: Optional[float]

    # Financed emissions by scope (tCO2e), post-attribution
    financed_scope_1_tco2e: Optional[float]
    financed_scope_2_tco2e: Optional[float]
    financed_scope_3_tco2e: Optional[float]  # upstream+downstream (per config)
    financed_total_tco2e: Optional[float]

    # Data quality
    weighted_dq_score: Optional[float]

    # WACI components (set only when revenue is available)
    emission_intensity_tco2e_per_mrevenue: Optional[float]  # entity-level
    waci_contribution: Optional[float]                      # weight × intensity

    # Flags
    emissions_record_found: bool = True
    denominator_found: bool = True


# ---------------------------------------------------------------------------
# Attribution factor helpers
# ---------------------------------------------------------------------------

def _compute_attribution_factor(
    holding: PortfolioHolding,
    config: EngineConfig,
) -> tuple[Optional[float], Optional[float]]:
    """
    Return (attribution_factor, denominator_value) for the holding.

    Attribution factor = outstanding_amount / PCAF_denominator.

    Clamped to [0, 1] as per PCAF guidance — a holding can never be
    attributed more than 100% of an entity's emissions.

    Returns (None, None) if the denominator cannot be resolved.
    """
    denominator = holding.attribution_denominator

    if denominator is None:
        # PCAF (2025) §5.6: motor vehicle loans with unknown origination value
        # must apply 100% attribution as a conservative fallback.
        if holding.asset_class.value == "motor_vehicle_loans":
            warnings.warn(
                f"Holding {holding.holding_id} (motor_vehicle_loans): "
                "vehicle_value_at_origination_usd is unknown. "
                "Applying 100% attribution per PCAF (2025) §5.6 conservative fallback.",
                UserWarning,
                stacklevel=3,
            )
            return 1.0, holding.outstanding_amount_usd
        if config.warn_missing_denominator:
            warnings.warn(
                f"Holding {holding.holding_id} ({holding.asset_class.value}): "
                f"attribution denominator is missing. "
                f"Financed emissions will be None for this holding.",
                UserWarning,
                stacklevel=3,
            )
        return None, None

    if denominator <= 0:
        if config.warn_missing_denominator:
            warnings.warn(
                f"Holding {holding.holding_id} ({holding.asset_class.value}): "
                f"attribution denominator is zero or negative. "
                f"Financed emissions will be None for this holding.",
                UserWarning,
                stacklevel=3,
            )
        return None, None

    factor = holding.outstanding_amount_usd / denominator

    # PCAF §5.1 note: factor > 1 can arise from stale EVIC data; clamp and warn.
    if factor > 1.0:
        warnings.warn(
            f"Holding {holding.holding_id}: attribution factor {factor:.4f} > 1.0 "
            f"(outstanding {holding.outstanding_amount_usd:,.0f} / "
            f"denominator {denominator:,.0f}). "
            f"Clamping to 1.0 — consider refreshing the denominator.",
            UserWarning,
            stacklevel=3,
        )
        factor = 1.0

    return factor, denominator


# ---------------------------------------------------------------------------
# Financed emissions for a single holding
# ---------------------------------------------------------------------------

def _compute_holding_emissions(
    holding: PortfolioHolding,
    record: EmissionsRecord,
    attribution_factor: float,
    config: EngineConfig,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Return (financed_s1, financed_s2, financed_s3) in tCO2e.

    Each scope is independently gated by the config flags so that
    callers can produce Scope-1+2-only portfolios when Scope 3 data
    quality is insufficient.

    Scope 3 obeys scope_3_upstream_only: when True only upstream is
    included, to avoid double-counting with downstream use-phase
    emissions in certain asset classes (e.g. motor vehicles where
    downstream is already captured via the fuel consumption proxy).
    """
    s1 = (attribution_factor * record.scope_1_emissions
          if config.include_scope_1 and record.scope_1_emissions is not None
          else None)

    s2 = (attribution_factor * record.scope_2_emissions
          if config.include_scope_2 and record.scope_2_emissions is not None
          else None)

    if config.include_scope_3:
        if config.scope_3_upstream_only:
            s3_raw = record.scope_3_upstream_emissions
        else:
            s3_raw = record.total_scope_3
        s3 = attribution_factor * s3_raw if s3_raw is not None else None
    else:
        s3 = None

    return s1, s2, s3


# ---------------------------------------------------------------------------
# WACI intensity for a single entity
# ---------------------------------------------------------------------------

def _compute_entity_intensity(
    record: EmissionsRecord,
    config: EngineConfig,
) -> Optional[float]:
    """
    Compute entity-level emissions intensity in tCO2e / $M revenue.

    Used as the per-entity input to the portfolio WACI calculation.
    Returns None when revenue is absent or zero (intensity undefined).

    The numerator respects the same scope flags as financed emissions
    so that WACI and absolute figures are always scope-consistent.
    """
    if record.revenue_usd is None or record.revenue_usd <= 0:
        return None

    scopes: list[float] = []
    if config.include_scope_1 and record.scope_1_emissions is not None:
        scopes.append(record.scope_1_emissions)
    if config.include_scope_2 and record.scope_2_emissions is not None:
        scopes.append(record.scope_2_emissions)
    if config.include_scope_3:
        s3 = (record.scope_3_upstream_emissions
              if config.scope_3_upstream_only
              else record.total_scope_3)
        if s3 is not None:
            scopes.append(s3)

    if not scopes:
        return None

    # Revenue in $M to produce tCO2e / $M revenue
    return sum(scopes) / (record.revenue_usd / 1_000_000)


# ---------------------------------------------------------------------------
# Main engine class
# ---------------------------------------------------------------------------

class PCАFEngine:
    """
    Computes PCAF-aligned financed emissions metrics for a portfolio.

    Usage
    -----
    >>> engine = PCАFEngine(config=EngineConfig())
    >>> result, holding_results = engine.run(portfolio, emissions_records)

    The engine is stateless between runs; the same instance can be
    reused across portfolios.
    """

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        portfolio: Portfolio,
        emissions_records: dict[str, EmissionsRecord],
        reporting_year: Optional[int] = None,
    ) -> tuple[PortfolioEmissionsResult, list[HoldingEmissionsResult]]:
        """
        Execute the full PCAF attribution computation.

        Parameters
        ----------
        portfolio : Typed Portfolio object from loader.py.
        emissions_records : dict[entity_id → EmissionsRecord] from loader.py.
        reporting_year : Overrides the year stamped on the result.  Defaults
            to the most common reporting_year in emissions_records.

        Returns
        -------
        result : Aggregated PortfolioEmissionsResult.
        holding_results : Per-holding intermediate results for inspection.
        """
        cfg = self.config
        total_aum = portfolio.total_aum_usd

        if total_aum <= 0:
            raise ValueError("Portfolio has zero or negative AUM — cannot compute metrics.")

        year = reporting_year or self._infer_reporting_year(emissions_records)

        holding_results: list[HoldingEmissionsResult] = []

        for holding in portfolio.holdings:
            hr = self._process_holding(holding, emissions_records, total_aum, cfg)
            holding_results.append(hr)

            # Write back to the PortfolioHolding so downstream modules
            # (deduplication, stress testing) see pre-computed values.
            holding.attribution_factor = hr.attribution_factor
            holding.financed_emissions_tco2e = hr.financed_total_tco2e
            holding.financed_emissions_dq_score = hr.weighted_dq_score

        result = self._aggregate(portfolio, holding_results, year, total_aum)
        return result, holding_results

    # ------------------------------------------------------------------
    # Per-holding processing
    # ------------------------------------------------------------------

    def _process_holding(
        self,
        holding: PortfolioHolding,
        emissions_records: dict[str, EmissionsRecord],
        total_aum: float,
        cfg: EngineConfig,
    ) -> HoldingEmissionsResult:

        record = emissions_records.get(holding.entity_id)
        emissions_found = record is not None

        if record is None:
            if cfg.warn_missing_emissions:
                warnings.warn(
                    f"Holding {holding.holding_id} ({holding.entity_name}): "
                    f"no emissions record found for entity_id='{holding.entity_id}'. "
                    f"Financed emissions will be None.",
                    UserWarning,
                    stacklevel=3,
                )
            return HoldingEmissionsResult(
                holding_id=holding.holding_id,
                entity_name=holding.entity_name,
                asset_class=holding.asset_class,
                outstanding_amount_usd=holding.outstanding_amount_usd,
                attribution_factor=None,
                attribution_denominator=None,
                financed_scope_1_tco2e=None,
                financed_scope_2_tco2e=None,
                financed_scope_3_tco2e=None,
                financed_total_tco2e=None,
                weighted_dq_score=None,
                emission_intensity_tco2e_per_mrevenue=None,
                waci_contribution=None,
                emissions_record_found=False,
                denominator_found=False,
            )

        attr_factor, denom_val = _compute_attribution_factor(holding, cfg)
        denom_found = attr_factor is not None

        if not denom_found:
            return HoldingEmissionsResult(
                holding_id=holding.holding_id,
                entity_name=holding.entity_name,
                asset_class=holding.asset_class,
                outstanding_amount_usd=holding.outstanding_amount_usd,
                attribution_factor=None,
                attribution_denominator=None,
                financed_scope_1_tco2e=None,
                financed_scope_2_tco2e=None,
                financed_scope_3_tco2e=None,
                financed_total_tco2e=None,
                weighted_dq_score=record.weighted_dq_score,
                emission_intensity_tco2e_per_mrevenue=_compute_entity_intensity(record, cfg),
                waci_contribution=None,
                emissions_record_found=True,
                denominator_found=False,
            )

        s1, s2, s3 = _compute_holding_emissions(holding, record, attr_factor, cfg)

        non_null = [v for v in (s1, s2, s3) if v is not None]
        total = sum(non_null) if non_null else None

        # WACI contribution: portfolio_weight × entity_intensity
        intensity = _compute_entity_intensity(record, cfg)
        weight = holding.outstanding_amount_usd / total_aum
        waci_contrib = weight * intensity if intensity is not None else None

        # Gate WACI by DQ score threshold
        dq = record.weighted_dq_score
        if dq is not None and dq > cfg.min_dq_score_for_waci:
            waci_contrib = None

        return HoldingEmissionsResult(
            holding_id=holding.holding_id,
            entity_name=holding.entity_name,
            asset_class=holding.asset_class,
            outstanding_amount_usd=holding.outstanding_amount_usd,
            attribution_factor=attr_factor,
            attribution_denominator=denom_val,
            financed_scope_1_tco2e=s1,
            financed_scope_2_tco2e=s2,
            financed_scope_3_tco2e=s3,
            financed_total_tco2e=total,
            weighted_dq_score=dq,
            emission_intensity_tco2e_per_mrevenue=intensity,
            waci_contribution=waci_contrib,
            emissions_record_found=emissions_found,
            denominator_found=True,
        )

    # ------------------------------------------------------------------
    # Portfolio-level aggregation
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        portfolio: Portfolio,
        holding_results: list[HoldingEmissionsResult],
        year: int,
        total_aum: float,
    ) -> PortfolioEmissionsResult:

        result = PortfolioEmissionsResult(
            portfolio_id=portfolio.portfolio_id,
            reporting_year=year,
        )
        result.n_holdings_total = len(holding_results)

        total_s1 = total_s2 = total_s3 = total_fe = 0.0
        waci_sum = 0.0
        dq_weight_sum = dq_score_sum = 0.0
        aum_with_emissions = 0.0
        n_with_emissions = 0
        n_reported = 0   # DQ 1 or 2
        n_estimated = 0  # DQ 4 or 5

        by_ac: dict[str, dict] = {}

        for hr in holding_results:
            if hr.financed_total_tco2e is not None:
                n_with_emissions += 1
                aum_with_emissions += hr.outstanding_amount_usd

                total_fe += hr.financed_total_tco2e
                total_s1 += hr.financed_scope_1_tco2e or 0.0
                total_s2 += hr.financed_scope_2_tco2e or 0.0
                total_s3 += hr.financed_scope_3_tco2e or 0.0

                # Portfolio-weighted DQ score
                # PCAF (2025) Box 6.1-6: weight by outstanding amount, not financed emissions.
                if hr.weighted_dq_score is not None:
                    dq_weight_sum += hr.outstanding_amount_usd
                    dq_score_sum += hr.outstanding_amount_usd * hr.weighted_dq_score

            # DQ coverage counters (based on emissions record, not attribution)
            if hr.weighted_dq_score is not None:
                if hr.weighted_dq_score <= 2.0:
                    n_reported += 1
                elif hr.weighted_dq_score >= 4.0:
                    n_estimated += 1

            # WACI
            if hr.waci_contribution is not None:
                waci_sum += hr.waci_contribution

            # Per asset class aggregation
            ac_key = hr.asset_class.value
            if ac_key not in by_ac:
                by_ac[ac_key] = {
                    "n_holdings": 0,
                    "aum_usd": 0.0,
                    "financed_emissions_tco2e": 0.0,
                }
            by_ac[ac_key]["n_holdings"] += 1
            by_ac[ac_key]["aum_usd"] += hr.outstanding_amount_usd
            by_ac[ac_key]["financed_emissions_tco2e"] += hr.financed_total_tco2e or 0.0

        result.n_holdings_with_emissions = n_with_emissions
        result.total_financed_emissions_tco2e = total_fe
        result.scope_1_financed_tco2e = total_s1
        result.scope_2_financed_tco2e = total_s2
        result.scope_3_financed_tco2e = total_s3

        # Intensity metrics
        result.waci_tco2e_per_mrevenue = waci_sum if waci_sum > 0 else None
        if total_aum > 0 and total_fe > 0:
            result.economic_intensity_tco2e_per_musd = total_fe / (total_aum / 1_000_000)

        # Data quality
        result.portfolio_weighted_dq_score = (
            dq_score_sum / dq_weight_sum if dq_weight_sum > 0 else None
        )
        n_total = result.n_holdings_total
        result.pct_holdings_with_reported_data = n_reported / n_total if n_total else 0.0
        result.pct_holdings_estimated = n_estimated / n_total if n_total else 0.0

        # Coverage
        result.aum_coverage_pct = aum_with_emissions / total_aum if total_aum else 0.0

        result.by_asset_class = by_ac
        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_reporting_year(records: dict[str, EmissionsRecord]) -> int:
        if not records:
            return 0
        from collections import Counter
        years = Counter(r.reporting_year for r in records.values())
        return years.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def build_default_engine() -> PCАFEngine:
    """Return an engine with PCAF-recommended defaults (all scopes, no DQ gate)."""
    return PCАFEngine(config=EngineConfig())


def build_scope12_engine() -> PCАFEngine:
    """
    Return an engine that excludes Scope 3.

    Useful when Scope 3 data quality across the portfolio is too poor to
    be meaningful, or when comparing against benchmarks that report
    Scope 1+2 only.
    """
    return PCАFEngine(config=EngineConfig(include_scope_3=False))


# ---------------------------------------------------------------------------
# Reporting helper — human-readable summary
# ---------------------------------------------------------------------------

def print_portfolio_summary(
    result: PortfolioEmissionsResult,
    holding_results: list[HoldingEmissionsResult],
) -> None:
    """Print a concise TCFD-style summary to stdout."""
    sep = "─" * 70

    print(f"\n{sep}")
    print(f"  GHG FINANCED EMISSIONS REPORT  |  Portfolio: {result.portfolio_id}")
    print(f"  Reporting year: {result.reporting_year}   |  "
          f"Computed: {result.computed_at.strftime('%Y-%m-%d %H:%M UTC')}")
    print(sep)

    print("\n▸ ABSOLUTE FINANCED EMISSIONS")
    print(f"    Scope 1          {result.scope_1_financed_tco2e:>15,.0f} tCO2e")
    print(f"    Scope 2          {result.scope_2_financed_tco2e:>15,.0f} tCO2e")
    print(f"    Scope 3          {result.scope_3_financed_tco2e:>15,.0f} tCO2e")
    print(f"    ─────────────────────────────────────")
    print(f"    TOTAL            {result.total_financed_emissions_tco2e:>15,.0f} tCO2e")

    print("\n▸ INTENSITY METRICS")
    waci = result.waci_tco2e_per_mrevenue
    ei   = result.economic_intensity_tco2e_per_musd
    print(f"    WACI             "
          f"{f'{waci:>12,.2f} tCO2e / $M revenue' if waci else '           N/A (no revenue data)'}")
    print(f"    Economic intens. "
          f"{f'{ei:>12,.2f} tCO2e / $M AUM' if ei else '           N/A'}")

    print("\n▸ DATA QUALITY")
    dq = result.portfolio_weighted_dq_score
    print(f"    Weighted DQ score       {f'{dq:.2f} / 5' if dq else 'N/A':>12}")
    print(f"    Holdings w/ reported data  {result.pct_holdings_with_reported_data * 100:>6.1f}%")
    print(f"    Holdings estimated         {result.pct_holdings_estimated * 100:>6.1f}%")
    print(f"    AUM coverage               {result.aum_coverage_pct * 100:>6.1f}%")

    print(f"\n▸ COVERAGE")
    print(f"    {result.n_holdings_with_emissions} / {result.n_holdings_total} "
          f"holdings have financed emissions data")

    print("\n▸ BY ASSET CLASS")
    print(f"    {'Asset Class':<40} {'AUM ($M)':>10}  {'Financed Emis. (tCO2e)':>22}")
    print(f"    {'─'*40} {'─'*10}  {'─'*22}")
    for ac, data in sorted(result.by_asset_class.items(),
                           key=lambda x: x[1]["financed_emissions_tco2e"], reverse=True):
        print(f"    {ac:<40} {data['aum_usd']/1e6:>10.1f}  {data['financed_emissions_tco2e']:>22,.0f}")

    print("\n▸ TOP HOLDINGS BY FINANCED EMISSIONS")
    print(f"    {'Holding':<35} {'Attribution':>12}  {'Financed (tCO2e)':>18}  {'DQ Score':>8}")
    print(f"    {'─'*35} {'─'*12}  {'─'*18}  {'─'*8}")
    sorted_hr = sorted(
        [h for h in holding_results if h.financed_total_tco2e is not None],
        key=lambda h: h.financed_total_tco2e,
        reverse=True,
    )
    for hr in sorted_hr[:10]:
        af = f"{hr.attribution_factor:.4f}" if hr.attribution_factor is not None else "  N/A"
        dq = f"{hr.weighted_dq_score:.1f}" if hr.weighted_dq_score is not None else "N/A"
        print(f"    {hr.entity_name:<35} {af:>12}  {hr.financed_total_tco2e:>18,.0f}  {dq:>8}")

    print(f"\n{sep}\n")
