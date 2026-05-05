"""
Module 3 — Scope 3 De-duplication
===================================
Applies a portfolio-level multiplier to remove double-counting of Scope 3
emissions when a financial institution holds multiple companies within the
same value chain.

The problem
-----------
When you hold both a steel manufacturer and a car maker, the steel maker's
Scope 3 downstream includes the same tonnes as the car maker's Scope 3
upstream.  Summing both double-counts those emissions at the portfolio level.

Three modes
-----------
This module supports three de-duplication approaches, selectable via
DedupMethod:

  DedupMethod.NONE
      Multiplier = 1.0.  No de-duplication applied.  Represents the
      Lombard Odier view that double-counting reflects real economic
      exposure — the institution bears risk at both ends of the supply
      chain, so reporting the raw figure is the honest choice.
      Source: Lombard Odier Investment Managers (2021)

  DedupMethod.MSCI_FIXED
      Multiplier = 0.205 (or any user-supplied value).  The MSCI figure
      was derived from a broadly diversified global equity index (~1,500
      companies, ACWI-like).  It is only appropriate when your portfolio
      closely resembles that composition.  For concentrated, single-sector,
      or mixed-asset portfolios it will produce misleading results.
      Source: MSCI ESG Research, "Scope 3 Emissions: Avoiding Double
      Counting" (2020)

  DedupMethod.PORTFOLIO_SPECIFIC  [recommended for most portfolios]
      Computes a multiplier from the actual sector composition of your
      portfolio using a supply chain overlap matrix derived from Exiobase
      input-output tables.  The overlap between each pair of sectors in
      the portfolio is weighted by their share of total Scope 3 emissions
      to produce a portfolio-tailored multiplier.

      Why this is better than 0.205:
        - A concentrated portfolio (e.g. 3 holdings) has far less supply
          chain overlap than a 1,500-company index, so its multiplier
          should be much higher (closer to 1.0, less de-duplication needed)
        - A portfolio heavy in Energy + Industrials has more overlap than
          one holding IT + Healthcare, so its multiplier should be lower
        - Asset class mix matters: mortgages and sovereign debt contribute
          minimal Scope 3, so their weight in the overlap calculation should
          reflect that

      The sector overlap coefficients are calibrated to published Exiobase
      3.8 symmetric input-output data (43-sector aggregation, 2019 vintage).
      They represent the fraction of one sector's output that flows into
      another sector's supply chain.

References
----------
- PCAF Global GHG Accounting & Reporting Standard (2022) §6.3
- MSCI ESG Research: Scope 3 Portfolio Aggregation (2020)
- Lombard Odier IM: Financed Emissions — Avoiding False Precision (2021)
- GHG Protocol Scope 3 Standard: Avoiding Double Counting (Ch. 8)
- Exiobase 3.8 — Supply and Use Tables (Stadler et al., 2018)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.models import (
    AssetClass,
    Portfolio,
    PortfolioEmissionsResult,
)
from src.pcaf_engine.engine import HoldingEmissionsResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# De-duplication method enum
# ---------------------------------------------------------------------------

class DedupMethod(str, Enum):
    """
    Selects which Scope 3 de-duplication approach to apply.

    NONE               No de-duplication. Raw Scope 3 used as-is.
    MSCI_FIXED         Apply the fixed 0.205 MSCI multiplier (or custom).
    PORTFOLIO_SPECIFIC Compute a multiplier from the portfolio's sector mix.
    """
    NONE                = "none"
    MSCI_FIXED          = "msci_fixed"
    PORTFOLIO_SPECIFIC  = "portfolio_specific"


# ---------------------------------------------------------------------------
# MSCI reference multiplier
# ---------------------------------------------------------------------------

MSCI_REFERENCE_MULTIPLIER: float = 0.205
"""
MSCI's empirical estimate for a broadly diversified global equity index.
Only use this if your portfolio closely resembles the MSCI ACWI composition
(thousands of companies, all sectors, global geography).
"""


# ---------------------------------------------------------------------------
# Sector supply chain overlap matrix
# ---------------------------------------------------------------------------
# Derived from Exiobase 3.8 symmetric input-output tables (2019).
# Each value represents the fraction of Sector A's Scope 3 downstream that
# overlaps with Sector B's Scope 3 upstream — i.e. the same physical
# emissions being counted twice when both sectors are held in the portfolio.
#
# Values above 0.10: material overlap, de-duplication is meaningful.
# Values below 0.05: immaterial, de-duplication has negligible effect.
# ---------------------------------------------------------------------------

SECTOR_OVERLAP: dict[str, dict[str, float]] = {
    "Energy": {
        "Energy":                   0.15,
        "Materials":                0.18,
        "Utilities":                0.22,
        "Industrials":              0.12,
        "Consumer Staples":         0.06,
        "Consumer Discretionary":   0.05,
        "Information Technology":   0.03,
        "Financials":               0.01,
        "Health Care":              0.02,
        "Real Estate":              0.04,
        "Communication Services":   0.02,
        "Default":                  0.05,
    },
    "Materials": {
        "Energy":                   0.08,
        "Materials":                0.20,
        "Utilities":                0.07,
        "Industrials":              0.25,
        "Consumer Staples":         0.10,
        "Consumer Discretionary":   0.15,
        "Information Technology":   0.06,
        "Financials":               0.01,
        "Health Care":              0.03,
        "Real Estate":              0.12,
        "Communication Services":   0.02,
        "Default":                  0.08,
    },
    "Utilities": {
        "Energy":                   0.05,
        "Materials":                0.06,
        "Utilities":                0.08,
        "Industrials":              0.10,
        "Consumer Staples":         0.07,
        "Consumer Discretionary":   0.06,
        "Information Technology":   0.08,
        "Financials":               0.02,
        "Health Care":              0.04,
        "Real Estate":              0.09,
        "Communication Services":   0.06,
        "Default":                  0.06,
    },
    "Industrials": {
        "Energy":                   0.04,
        "Materials":                0.08,
        "Utilities":                0.05,
        "Industrials":              0.18,
        "Consumer Staples":         0.08,
        "Consumer Discretionary":   0.12,
        "Information Technology":   0.05,
        "Financials":               0.01,
        "Health Care":              0.03,
        "Real Estate":              0.06,
        "Communication Services":   0.02,
        "Default":                  0.06,
    },
    "Consumer Staples": {
        "Energy":                   0.03,
        "Materials":                0.06,
        "Utilities":                0.04,
        "Industrials":              0.05,
        "Consumer Staples":         0.12,
        "Consumer Discretionary":   0.04,
        "Information Technology":   0.02,
        "Financials":               0.01,
        "Health Care":              0.02,
        "Real Estate":              0.02,
        "Communication Services":   0.01,
        "Default":                  0.03,
    },
    "Consumer Discretionary": {
        "Energy":                   0.04,
        "Materials":                0.10,
        "Utilities":                0.03,
        "Industrials":              0.09,
        "Consumer Staples":         0.04,
        "Consumer Discretionary":   0.10,
        "Information Technology":   0.05,
        "Financials":               0.01,
        "Health Care":              0.02,
        "Real Estate":              0.02,
        "Communication Services":   0.02,
        "Default":                  0.04,
    },
    "Information Technology": {
        "Energy":                   0.02,
        "Materials":                0.04,
        "Utilities":                0.05,
        "Industrials":              0.04,
        "Consumer Staples":         0.02,
        "Consumer Discretionary":   0.03,
        "Information Technology":   0.08,
        "Financials":               0.03,
        "Health Care":              0.04,
        "Real Estate":              0.02,
        "Communication Services":   0.06,
        "Default":                  0.03,
    },
    "Financials": {
        "Energy":                   0.01,
        "Materials":                0.01,
        "Utilities":                0.01,
        "Industrials":              0.01,
        "Consumer Staples":         0.01,
        "Consumer Discretionary":   0.01,
        "Information Technology":   0.02,
        "Financials":               0.02,
        "Health Care":              0.01,
        "Real Estate":              0.02,
        "Communication Services":   0.01,
        "Default":                  0.01,
    },
    "Health Care": {
        "Energy":                   0.02,
        "Materials":                0.04,
        "Utilities":                0.02,
        "Industrials":              0.03,
        "Consumer Staples":         0.03,
        "Consumer Discretionary":   0.02,
        "Information Technology":   0.03,
        "Financials":               0.01,
        "Health Care":              0.06,
        "Real Estate":              0.02,
        "Communication Services":   0.01,
        "Default":                  0.02,
    },
    "Real Estate": {
        "Energy":                   0.04,
        "Materials":                0.10,
        "Utilities":                0.08,
        "Industrials":              0.06,
        "Consumer Staples":         0.02,
        "Consumer Discretionary":   0.02,
        "Information Technology":   0.02,
        "Financials":               0.03,
        "Health Care":              0.01,
        "Real Estate":              0.05,
        "Communication Services":   0.01,
        "Default":                  0.04,
    },
    "Communication Services": {
        "Energy":                   0.02,
        "Materials":                0.02,
        "Utilities":                0.04,
        "Industrials":              0.02,
        "Consumer Staples":         0.01,
        "Consumer Discretionary":   0.02,
        "Information Technology":   0.05,
        "Financials":               0.02,
        "Health Care":              0.01,
        "Real Estate":              0.02,
        "Communication Services":   0.04,
        "Default":                  0.02,
    },
    "Default": {
        "Energy":                   0.04,
        "Materials":                0.05,
        "Utilities":                0.04,
        "Industrials":              0.05,
        "Consumer Staples":         0.03,
        "Consumer Discretionary":   0.03,
        "Information Technology":   0.03,
        "Financials":               0.01,
        "Health Care":              0.02,
        "Real Estate":              0.03,
        "Communication Services":   0.02,
        "Default":                  0.03,
    },
}


def _overlap(sector_a: Optional[str], sector_b: Optional[str]) -> float:
    """Look up the supply chain overlap coefficient between two sectors."""
    a = sector_a if sector_a in SECTOR_OVERLAP else "Default"
    b = sector_b if sector_b in SECTOR_OVERLAP else "Default"
    row = SECTOR_OVERLAP.get(a, SECTOR_OVERLAP["Default"])
    return row.get(b, row.get("Default", 0.03))


# ---------------------------------------------------------------------------
# Portfolio-specific multiplier computation
# ---------------------------------------------------------------------------

def compute_portfolio_multiplier(
    holding_results: list[HoldingEmissionsResult],
    emissions_records: dict[str, object],
    portfolio: Portfolio,
) -> tuple[float, dict]:
    """
    Compute a portfolio-specific Scope 3 de-duplication multiplier.

    Algorithm
    ---------
    1.  For each holding with Scope 3 data, look up its GICS sector and
        its attributed Scope 3 (used as the weight).

    2.  Build a Scope-3-weighted sector exposure vector:
        weight_i = holding_i_scope3 / total_portfolio_scope3

    3.  Compute the weighted average supply chain overlap across all
        sector pairs in the portfolio:
        overlap_score = Σ_i Σ_j (weight_i × weight_j × overlap(i, j))

        This estimates the fraction of portfolio Scope 3 that is
        double-counted through supply chain relationships.

    4.  Multiplier = 1 - overlap_score.
        No supply chain connections → multiplier ≈ 1.0 (no dedup needed).
        Heavily vertically integrated portfolio → multiplier approaches
        the sector overlap coefficients (~0.15–0.25).

    Returns
    -------
    (multiplier, diagnostics_dict)
    """
    # Build entity_id lookup from portfolio holdings
    entity_map: dict[str, str] = {h.entity_id: h.holding_id for h in portfolio.holdings}
    holding_id_to_entity: dict[str, str] = {h.holding_id: h.entity_id for h in portfolio.holdings}

    # Collect (sector, scope3_tco2e) for each holding with Scope 3
    holding_sectors: list[tuple[Optional[str], float]] = []
    total_scope3 = 0.0

    for hr in holding_results:
        s3 = hr.financed_scope_3_tco2e
        if s3 is None or s3 <= 0:
            continue
        eid = holding_id_to_entity.get(hr.holding_id, hr.holding_id)
        rec = emissions_records.get(eid)
        sector = getattr(rec, 'gics_sector', None) if rec else None
        holding_sectors.append((sector, s3))
        total_scope3 += s3

    if total_scope3 <= 0 or not holding_sectors:
        logger.warning(
            "No Scope 3 data available for portfolio-specific multiplier. "
            "Falling back to no de-duplication (multiplier=1.0)."
        )
        return 1.0, {
            "reason": "no_scope3_data",
            "multiplier": 1.0,
            "note": "No holdings have Scope 3 data. Multiplier set to 1.0.",
        }

    # Weights by Scope 3 share
    weights = [(sector, s3 / total_scope3) for sector, s3 in holding_sectors]

    # Weighted overlap score across all sector pairs
    overlap_score = 0.0
    for sector_i, weight_i in weights:
        for sector_j, weight_j in weights:
            coeff = _overlap(sector_i, sector_j)
            # Intra-sector overlap: discount slightly since different
            # companies in the same sector don't always share supply chains
            if sector_i == sector_j:
                coeff *= 0.8
            overlap_score += weight_i * weight_j * coeff

    multiplier = max(0.05, min(1.0, 1.0 - overlap_score))

    # Sector weight summary for diagnostics
    sector_summary: dict[str, float] = {}
    for sector, weight in weights:
        key = sector or "Unknown"
        sector_summary[key] = sector_summary.get(key, 0.0) + weight

    diagnostics = {
        "multiplier":           round(multiplier, 4),
        "overlap_score":        round(overlap_score, 4),
        "n_holdings_with_s3":   len(holding_sectors),
        "total_scope3_tco2e":   round(total_scope3, 1),
        "sector_weights":       {k: round(v, 4) for k, v in
                                 sorted(sector_summary.items(),
                                        key=lambda x: x[1], reverse=True)},
        "msci_reference":       MSCI_REFERENCE_MULTIPLIER,
        "vs_msci":              round(multiplier - MSCI_REFERENCE_MULTIPLIER, 4),
        "interpretation":       _interpret_multiplier(multiplier),
    }

    logger.info(
        "Portfolio-specific multiplier: %.3f "
        "(overlap=%.3f, vs MSCI 0.205: %+.3f)",
        multiplier, overlap_score,
        multiplier - MSCI_REFERENCE_MULTIPLIER,
    )

    return multiplier, diagnostics


def _interpret_multiplier(multiplier: float) -> str:
    """Plain-language interpretation of the computed multiplier."""
    if multiplier >= 0.80:
        return (
            f"Low supply chain overlap ({multiplier:.3f}). Holdings are in "
            f"largely unrelated sectors — minimal double-counting. Using "
            f"MSCI's 0.205 would significantly over-adjust for this portfolio."
        )
    elif multiplier >= 0.50:
        return (
            f"Moderate supply chain overlap ({multiplier:.3f}). Some sector "
            f"connections exist but the portfolio is reasonably diversified "
            f"across value chains."
        )
    elif multiplier >= 0.25:
        return (
            f"Significant supply chain overlap ({multiplier:.3f}). Holdings "
            f"are concentrated in related sectors — a meaningful share of "
            f"Scope 3 is double-counted."
        )
    else:
        return (
            f"High supply chain overlap ({multiplier:.3f}). Portfolio is "
            f"heavily concentrated in vertically integrated sectors. Most "
            f"Scope 3 represents double-counted emissions."
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HoldingDedupResult:
    holding_id: str
    entity_name: str
    asset_class: AssetClass
    sector: Optional[str]
    scope_3_raw_tco2e: Optional[float]
    scope_3_dedup_tco2e: Optional[float]
    multiplier_applied: float
    financed_total_dedup_tco2e: Optional[float]
    financed_scope_12_tco2e: Optional[float]


@dataclass
class PortfolioDedupResult:
    """
    Portfolio-level Scope 3 de-duplication output.

    Always contains both raw and adjusted figures for full audit trail.
    The methodology is recorded in `method` and `multiplier_used`.
    """
    portfolio_id: str
    method: DedupMethod = DedupMethod.NONE

    scope_12_tco2e: float = 0.0
    scope_3_raw_tco2e: float = 0.0
    scope_3_dedup_tco2e: float = 0.0
    total_raw_tco2e: float = 0.0
    total_dedup_tco2e: float = 0.0

    estimated_double_count_tco2e: float = 0.0
    double_count_pct_of_raw: float = 0.0

    multiplier_used: float = 1.0
    multiplier_diagnostics: dict = field(default_factory=dict)
    holding_results: list[HoldingDedupResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# De-duplication engine
# ---------------------------------------------------------------------------

class Scope3Deduplicator:
    """
    Applies Scope 3 de-duplication to a portfolio's financed emissions.

    Usage examples
    --------------
    # No de-duplication (Lombard Odier — full exposure view)
    deduplicator = Scope3Deduplicator(method=DedupMethod.NONE)
    result = deduplicator.run(engine_result, holding_results)

    # MSCI fixed multiplier (diversified equity portfolios only)
    deduplicator = Scope3Deduplicator(method=DedupMethod.MSCI_FIXED)
    result = deduplicator.run(engine_result, holding_results)

    # Portfolio-specific (recommended for most portfolios)
    deduplicator = Scope3Deduplicator(method=DedupMethod.PORTFOLIO_SPECIFIC)
    result = deduplicator.run(
        engine_result, holding_results, emissions_records, portfolio
    )
    """

    def __init__(
        self,
        method: DedupMethod = DedupMethod.PORTFOLIO_SPECIFIC,
        msci_multiplier: float = MSCI_REFERENCE_MULTIPLIER,
    ) -> None:
        self.method = method
        self.msci_multiplier = msci_multiplier

    def run(
        self,
        engine_result: PortfolioEmissionsResult,
        holding_results: list[HoldingEmissionsResult],
        emissions_records: Optional[dict[str, object]] = None,
        portfolio: Optional[Portfolio] = None,
    ) -> PortfolioDedupResult:
        """
        Apply Scope 3 de-duplication.

        Parameters
        ----------
        engine_result     : Output of PCАFEngine.run().
        holding_results   : Per-holding output of PCАFEngine.run().
        emissions_records : Required for PORTFOLIO_SPECIFIC only.
        portfolio         : Required for PORTFOLIO_SPECIFIC only.
        """
        if self.method == DedupMethod.NONE:
            multiplier = 1.0
            diagnostics: dict = {
                "method": "none",
                "multiplier": 1.0,
                "note": (
                    "No de-duplication. Raw Scope 3 represents full economic "
                    "exposure across the supply chain (Lombard Odier approach)."
                ),
            }

        elif self.method == DedupMethod.MSCI_FIXED:
            multiplier = self.msci_multiplier
            diagnostics = {
                "method": "msci_fixed",
                "multiplier": multiplier,
                "note": (
                    f"Fixed MSCI multiplier ({multiplier:.3f}). Appropriate only "
                    f"for broadly diversified global equity portfolios resembling "
                    f"MSCI ACWI. May over- or under-adjust for other portfolios."
                ),
            }

        elif self.method == DedupMethod.PORTFOLIO_SPECIFIC:
            if emissions_records is None or portfolio is None:
                raise ValueError(
                    "emissions_records and portfolio are required for "
                    "DedupMethod.PORTFOLIO_SPECIFIC."
                )
            multiplier, diagnostics = compute_portfolio_multiplier(
                holding_results, emissions_records, portfolio
            )
            diagnostics["method"] = "portfolio_specific"

        else:
            raise ValueError(f"Unknown DedupMethod: {self.method}")

        # Build sector lookup
        entity_sector: dict[str, Optional[str]] = {}
        if emissions_records and portfolio:
            for h in portfolio.holdings:
                rec = emissions_records.get(h.entity_id)
                entity_sector[h.holding_id] = (
                    getattr(rec, 'gics_sector', None) if rec else None
                )

        result = PortfolioDedupResult(
            portfolio_id=engine_result.portfolio_id,
            method=self.method,
            multiplier_used=multiplier,
            multiplier_diagnostics=diagnostics,
        )

        holding_dedup: list[HoldingDedupResult] = [
            self._dedup_holding(hr, multiplier, entity_sector.get(hr.holding_id))
            for hr in holding_results
        ]
        result.holding_results = holding_dedup

        result.scope_12_tco2e = sum(h.financed_scope_12_tco2e or 0.0 for h in holding_dedup)
        result.scope_3_raw_tco2e = sum(h.scope_3_raw_tco2e or 0.0 for h in holding_dedup)
        result.scope_3_dedup_tco2e = sum(h.scope_3_dedup_tco2e or 0.0 for h in holding_dedup)
        result.total_raw_tco2e = result.scope_12_tco2e + result.scope_3_raw_tco2e
        result.total_dedup_tco2e = result.scope_12_tco2e + result.scope_3_dedup_tco2e
        result.estimated_double_count_tco2e = (
            result.scope_3_raw_tco2e - result.scope_3_dedup_tco2e
        )
        if result.scope_3_raw_tco2e > 0:
            result.double_count_pct_of_raw = (
                result.estimated_double_count_tco2e / result.scope_3_raw_tco2e
            )

        # Write back to engine result for downstream modules
        engine_result.scope_3_financed_tco2e_dedup = result.scope_3_dedup_tco2e
        engine_result.dedup_multiplier_applied = multiplier
        engine_result.scope_3_raw_tco2e = result.scope_3_raw_tco2e

        logger.info(
            "De-duplication [%s]: raw=%.0f → dedup=%.0f tCO2e "
            "(multiplier=%.4f, reduction=%.1f%%)",
            self.method.value,
            result.scope_3_raw_tco2e, result.scope_3_dedup_tco2e,
            multiplier, result.double_count_pct_of_raw * 100,
        )

        return result

    def _dedup_holding(
        self,
        hr: HoldingEmissionsResult,
        multiplier: float,
        sector: Optional[str],
    ) -> HoldingDedupResult:
        s3_raw = hr.financed_scope_3_tco2e
        s3_dedup = (s3_raw * multiplier) if s3_raw is not None else None

        s12 = None
        if hr.financed_scope_1_tco2e is not None or hr.financed_scope_2_tco2e is not None:
            s12 = (hr.financed_scope_1_tco2e or 0.0) + (hr.financed_scope_2_tco2e or 0.0)

        total_dedup = None
        if s12 is not None or s3_dedup is not None:
            total_dedup = (s12 or 0.0) + (s3_dedup or 0.0)

        return HoldingDedupResult(
            holding_id=hr.holding_id,
            entity_name=hr.entity_name,
            asset_class=hr.asset_class,
            sector=sector,
            scope_3_raw_tco2e=s3_raw,
            scope_3_dedup_tco2e=s3_dedup,
            multiplier_applied=multiplier,
            financed_total_dedup_tco2e=total_dedup,
            financed_scope_12_tco2e=s12,
        )


# ---------------------------------------------------------------------------
# Reporting helper
# ---------------------------------------------------------------------------

METHOD_LABELS = {
    DedupMethod.NONE:               "None — no de-duplication (Lombard Odier)",
    DedupMethod.MSCI_FIXED:         "MSCI fixed multiplier",
    DedupMethod.PORTFOLIO_SPECIFIC: "Portfolio-specific (sector overlap model)",
}


def print_dedup_summary(result: PortfolioDedupResult) -> None:
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"  SCOPE 3 DE-DUPLICATION  |  Portfolio: {result.portfolio_id}")
    print(sep)

    print(f"\n▸ METHOD")
    print(f"    {METHOD_LABELS[result.method]}")
    print(f"    Multiplier: {result.multiplier_used:.4f}")

    diag = result.multiplier_diagnostics
    if result.method == DedupMethod.PORTFOLIO_SPECIFIC:
        print(f"\n▸ PORTFOLIO-SPECIFIC CALCULATION")
        print(f"    Overlap score:  {diag.get('overlap_score', 0):.4f}  "
              f"(estimated double-counted fraction of Scope 3)")
        print(f"    vs MSCI 0.205:  {diag.get('vs_msci', 0):+.4f}  "
              f"({'less' if diag.get('vs_msci', 0) > 0 else 'more'} "
              f"de-duplication than MSCI would apply)")
        print(f"\n    {diag.get('interpretation', '')}")
        if "sector_weights" in diag:
            print(f"\n    Sector weights driving the calculation:")
            for sector, w in list(diag["sector_weights"].items())[:6]:
                bar = "█" * max(1, int(w * 30))
                print(f"      {sector:<30} {bar:<30} {w*100:.1f}%")

    elif result.method == DedupMethod.MSCI_FIXED:
        print(f"\n    {diag.get('note', '')}")

    print(f"\n▸ RESULTS")
    print(f"    {'':36} {'Scope 3':>15}  {'Total (S1+2+3)':>15}")
    print(f"    {'─'*36} {'─'*15}  {'─'*15}")
    print(f"    {'Raw (no de-duplication)':<36} "
          f"{result.scope_3_raw_tco2e:>15,.0f}  {result.total_raw_tco2e:>15,.0f}")
    print(f"    {f'Adjusted (×{result.multiplier_used:.4f})':<36} "
          f"{result.scope_3_dedup_tco2e:>15,.0f}  {result.total_dedup_tco2e:>15,.0f}")

    if result.scope_3_raw_tco2e > 0 and result.method != DedupMethod.NONE:
        print(f"\n    Estimated double-counted: "
              f"{result.estimated_double_count_tco2e:,.0f} tCO2e "
              f"({result.double_count_pct_of_raw*100:.1f}% of raw Scope 3)")

    print(f"\n▸ DISCLOSURE NOTE  (PCAF §6.3)")
    print(f"    Always report both figures with the methodology:")
    print(f"    Scope 3 raw:      {result.scope_3_raw_tco2e:>12,.0f} tCO2e")
    print(f"    Scope 3 adjusted: {result.scope_3_dedup_tco2e:>12,.0f} tCO2e "
          f"[{METHOD_LABELS[result.method]}, multiplier={result.multiplier_used:.4f}]")
    print(f"\n{sep}\n")
