"""
Module 4b — SBTi Pathway Alignment
=====================================
Assesses whether each portfolio holding is aligned with the Science Based
Targets initiative (SBTi) 1.5°C decarbonisation pathway.

What this module does
---------------------
For each holding:
  1.  Compute the entity's current emissions intensity (tCO2e / $M revenue).
  2.  Look up the sector-specific 1.5°C benchmark intensity for the
      reporting year and the target year.
  3.  Compare — if the entity's intensity is above the benchmark, it is
      misaligned.  The gap is the alignment gap (tCO2e / $M revenue).
  4.  Aggregate to a portfolio alignment score (% AUM aligned).

How SBTi 1.5°C pathways work
------------------------------
SBTi provides sector-specific decarbonisation pathways derived from the
IEA Net Zero by 2050 scenario.  Each sector has a benchmark intensity in
a base year (typically 2020) and must reduce by a sector-specific annual
rate to stay on the 1.5°C pathway.

The SBTi Corporate Net-Zero Standard requires:
  - Near-term targets (5–10 years): Scope 1+2 reduction ≥ 4.2%/year
    (absolute) OR emissions intensity reduction ≥ 7%/year (for some sectors)
  - Long-term targets (by 2050): Net-zero across all scopes

Sector benchmarks used here
-----------------------------
Benchmarks are emissions intensities in tCO2e / $M revenue, calibrated
to the SBTi/IEA NZE sector pathways for the base year 2020.
Annual reduction rates are from SBTi Corporate Net-Zero Standard (2021).

    Sector                  Base intensity   Annual reduction
    Energy                  4,500            -7.0%/yr
    Materials               3,200            -5.5%/yr
    Utilities               2,800            -8.0%/yr
    Industrials             1,200            -4.5%/yr
    Consumer Staples        900              -4.2%/yr
    Consumer Discretionary  700              -4.2%/yr
    Information Technology  150              -4.2%/yr
    Financials              80               -4.2%/yr
    Health Care             200              -4.2%/yr
    Real Estate             1,800            -5.0%/yr
    Communication Services  120              -4.2%/yr
    Default (all others)    500              -4.2%/yr

References
----------
- SBTi Corporate Net-Zero Standard v1.1 (2021)
- IEA Net Zero by 2050 — A Roadmap for the Global Energy Sector (2021)
- PCAF / GFANZ: Measuring Portfolio Alignment (2021)
- Transition Pathway Initiative (TPI) Sector Benchmarks
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.models import AssetClass, Portfolio
from src.pcaf_engine.engine import HoldingEmissionsResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Temperature score methodology options
# ---------------------------------------------------------------------------

class TemperatureScoreMethod(str, Enum):
    """
    Controls how extreme outliers are handled when computing the
    portfolio-level implied temperature score.

    NO_CAP
        Raw AUM-weighted average of all holdings' overshoot percentages.
        Academically pure but highly sensitive to outliers — one holding
        with enormous emissions relative to revenue can push the portfolio
        score to implausible levels (e.g. 7°C+).
        Use when: you want a conservative upper-bound view, or are
        comparing against other uncapped methodologies.

    OVERSHOOT_CAP
        Each holding's overshoot is capped at a configurable multiple of
        the sector benchmark before averaging.  Default cap: 10× (i.e. a
        holding can contribute at most 10× the benchmark intensity to the
        weighted average, regardless of its actual overshoot).
        This is the most widely used approach — adopted by MSCI, FTSE
        Russell, and most major asset managers.
        Use when: you want a portfolio score that reflects the central
        tendency rather than the worst outlier.

    WINSORISE
        Cap at the 95th percentile of the portfolio's own overshoot
        distribution before averaging.  More data-driven than a fixed
        multiple — the cap adjusts to the portfolio's own characteristics.
        Use when: you have a large, diversified portfolio and want the
        cap to be defensible without assuming a fixed multiple.
    """
    NO_CAP        = "no_cap"
    OVERSHOOT_CAP = "overshoot_cap"
    WINSORISE     = "winsorise"


# Default overshoot cap multiplier (MSCI / mainstream standard)
DEFAULT_OVERSHOOT_CAP: float = 10.0

# Winsorisation percentile
DEFAULT_WINSORISE_PCT: float = 0.95

# Alignment tolerance — holdings within 20% of benchmark = "aligned"
ALIGNMENT_TOLERANCE_PCT: float = 0.20


# ---------------------------------------------------------------------------
# SBTi sector benchmarks
# Base year: 2020.  Intensity in tCO2e / $M revenue.
# Annual reduction rate to stay on 1.5°C pathway.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectorBenchmark:
    sector: str
    base_intensity_tco2e_per_mrevenue: float  # tCO2e / $M revenue in base year
    annual_reduction_rate: float              # e.g. 0.07 = 7% per year
    base_year: int = 2020
    source: str = "SBTi Corporate Net-Zero Standard (2021) / IEA NZE"

    def intensity_for_year(self, year: int) -> float:
        """Benchmark intensity for a given year, compounded from base year."""
        years_elapsed = year - self.base_year
        return self.base_intensity_tco2e_per_mrevenue * (
            (1 - self.annual_reduction_rate) ** years_elapsed
        )

    def reduction_required_by(self, current_year: int, target_year: int) -> float:
        """Total % reduction required from current_year to target_year."""
        years = target_year - current_year
        return 1 - (1 - self.annual_reduction_rate) ** years


SECTOR_BENCHMARKS: dict[str, SectorBenchmark] = {
    "Energy": SectorBenchmark(
        "Energy", 4500, 0.070,
        source="IEA NZE Oil & Gas sector pathway"
    ),
    "Materials": SectorBenchmark(
        "Materials", 3200, 0.055,
        source="IEA NZE Heavy Industry (steel/cement/chemicals)"
    ),
    "Utilities": SectorBenchmark(
        "Utilities", 2800, 0.080,
        source="IEA NZE Power sector pathway"
    ),
    "Industrials": SectorBenchmark(
        "Industrials", 1200, 0.045,
        source="SBTi Corporate NZ Standard — Industrials"
    ),
    "Consumer Staples": SectorBenchmark(
        "Consumer Staples", 900, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
    "Consumer Discretionary": SectorBenchmark(
        "Consumer Discretionary", 700, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
    "Information Technology": SectorBenchmark(
        "Information Technology", 150, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
    "Financials": SectorBenchmark(
        "Financials", 80, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
    "Health Care": SectorBenchmark(
        "Health Care", 200, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
    "Real Estate": SectorBenchmark(
        "Real Estate", 1800, 0.050,
        source="SBTi Buildings sector pathway"
    ),
    "Communication Services": SectorBenchmark(
        "Communication Services", 120, 0.042,
        source="SBTi Corporate NZ Standard — general cross-sector"
    ),
}

# Default for unknown / unmapped sectors
DEFAULT_BENCHMARK = SectorBenchmark(
    "Default", 500, 0.042,
    source="SBTi cross-sector pathway (4.2%/yr)"
)

# Alignment classification thresholds
# A holding within 20% of the benchmark is considered "aligned"


# ---------------------------------------------------------------------------
# Per-holding alignment result
# ---------------------------------------------------------------------------

@dataclass
class HoldingAlignmentResult:
    """
    SBTi pathway alignment assessment for a single holding.

    Compares the entity's current emissions intensity against the
    sector-specific 1.5°C benchmark for the assessment year.
    """
    holding_id: str
    entity_name: str
    asset_class: AssetClass
    outstanding_amount_usd: float
    sector: Optional[str]

    # Benchmark used
    benchmark: SectorBenchmark
    assessment_year: int

    # Entity's current intensity (tCO2e / $M revenue)
    entity_intensity: Optional[float]

    # 1.5°C benchmark intensity for this year (tCO2e / $M revenue)
    benchmark_intensity: float

    # Gap: positive = above benchmark (misaligned), negative = below (ahead of path)
    intensity_gap: Optional[float]

    # Gap as % of benchmark (how far above/below the 1.5°C line)
    intensity_gap_pct: Optional[float]

    # Implied annual reduction needed to reach benchmark in 5 years
    required_annual_reduction_pct: Optional[float]

    # Alignment status
    alignment_status: str  # "aligned" | "misaligned" | "no_data" | "ahead_of_path"

    # Absolute financed emissions gap (how many tCO2e above benchmark, attributed)
    financed_gap_tco2e: Optional[float]


# ---------------------------------------------------------------------------
# Portfolio alignment result
# ---------------------------------------------------------------------------

@dataclass
class PortfolioAlignmentResult:
    """
    Portfolio-level SBTi alignment summary.

    The headline metric — % of AUM aligned with 1.5°C — is the primary
    output required under GFANZ's Measuring Portfolio Alignment framework
    and SFDR Article 29 (France's energy & climate law).
    """
    portfolio_id: str
    assessment_year: int
    target_year: int   # Year the portfolio aims to be fully aligned (e.g. 2030)

    # Portfolio alignment metrics
    pct_aum_aligned: float = 0.0           # % of AUM in aligned holdings
    pct_aum_misaligned: float = 0.0        # % of AUM in misaligned holdings
    pct_aum_ahead_of_path: float = 0.0     # % of AUM already below benchmark
    pct_aum_no_data: float = 0.0           # % of AUM with no intensity data

    # Aggregate alignment gap
    total_financed_gap_tco2e: float = 0.0  # Total excess tCO2e vs 1.5°C path

    # Portfolio temperature scores — one per method for comparison
    # All three are always computed so users can compare methodologies
    implied_temperature_no_cap: Optional[float] = None       # Raw, no outlier treatment
    implied_temperature_capped: Optional[float] = None       # Overshoot cap (default 10×)
    implied_temperature_winsorised: Optional[float] = None   # 95th percentile cap

    # Which method was chosen as the primary reported score
    temperature_method: str = TemperatureScoreMethod.OVERSHOOT_CAP
    implied_temperature_c: Optional[float] = None            # Primary score (chosen method)
    overshoot_cap_used: float = DEFAULT_OVERSHOOT_CAP
    winsorise_pct_used: float = DEFAULT_WINSORISE_PCT

    # Counts
    n_aligned: int = 0
    n_misaligned: int = 0
    n_ahead_of_path: int = 0
    n_no_data: int = 0

    # Top misaligned holdings (sorted by financed gap)
    top_misaligned: list[str] = field(default_factory=list)  # entity names

    # Per-holding results
    holding_results: list[HoldingAlignmentResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Alignment engine
# ---------------------------------------------------------------------------

class PathwayAlignmentAssessor:
    """
    Assesses portfolio alignment with the SBTi 1.5°C pathway.

    Usage
    -----
    >>> assessor = PathwayAlignmentAssessor()
    >>> result = assessor.run(
    ...     portfolio, holding_results, emissions_records,
    ...     assessment_year=2022, target_year=2030
    ... )
    """

    def __init__(
        self,
        alignment_tolerance_pct: float = ALIGNMENT_TOLERANCE_PCT,
        temperature_method: TemperatureScoreMethod = TemperatureScoreMethod.OVERSHOOT_CAP,
        overshoot_cap: float = DEFAULT_OVERSHOOT_CAP,
        winsorise_pct: float = DEFAULT_WINSORISE_PCT,
    ) -> None:
        """
        Parameters
        ----------
        alignment_tolerance_pct : Holdings within this % of benchmark = "aligned".
            Default 20% (consistent with TPI / MSCI methodology).
        temperature_method : Which outlier treatment to use as the primary
            reported temperature score.  All three are always computed.
            Default: OVERSHOOT_CAP (mainstream / MSCI approach).
        overshoot_cap : Multiple of the benchmark used as the ceiling for
            each holding's overshoot contribution when method=OVERSHOOT_CAP.
            Default 10× (MSCI standard).  Lower values (e.g. 5×) produce
            more conservative scores; higher values (e.g. 20×) allow more
            outlier influence.
        winsorise_pct : Percentile at which to cap overshoot values when
            method=WINSORISE.  Default 95th percentile.
        """
        self.tolerance = alignment_tolerance_pct
        self.temperature_method = temperature_method
        self.overshoot_cap = overshoot_cap
        self.winsorise_pct = winsorise_pct

    def run(
        self,
        portfolio: Portfolio,
        holding_results: list[HoldingEmissionsResult],
        emissions_records: dict[str, object],
        assessment_year: int = 2022,
        target_year: int = 2030,
    ) -> PortfolioAlignmentResult:
        """
        Run the full portfolio alignment assessment.

        Parameters
        ----------
        portfolio : The portfolio to assess.
        holding_results : Per-holding engine output.
        emissions_records : Raw emissions records (entity_id → EmissionsRecord).
        assessment_year : Year of the emissions data being assessed.
        target_year : Near-term SBTi target year (default 2030).

        Returns
        -------
        PortfolioAlignmentResult with per-holding and portfolio-level metrics.
        """
        result = PortfolioAlignmentResult(
            portfolio_id=portfolio.portfolio_id,
            assessment_year=assessment_year,
            target_year=target_year,
        )

        total_aum = portfolio.total_aum_usd
        aum_aligned = aum_misaligned = aum_ahead = aum_no_data = 0.0
        total_gap_tco2e = 0.0

        holding_map = {h.holding_id: h for h in portfolio.holdings}
        holding_alignment: list[HoldingAlignmentResult] = []

        for hr in holding_results:
            ph = holding_map.get(hr.holding_id)
            rec = emissions_records.get(ph.entity_id if ph else hr.holding_id)

            har = self._assess_holding(
                hr, rec, assessment_year, target_year
            )
            holding_alignment.append(har)

            aum = hr.outstanding_amount_usd
            aum_weight = aum / total_aum if total_aum > 0 else 0.0

            if har.alignment_status == "aligned":
                result.n_aligned += 1
                aum_aligned += aum
            elif har.alignment_status == "ahead_of_path":
                result.n_ahead_of_path += 1
                aum_ahead += aum
            elif har.alignment_status == "misaligned":
                result.n_misaligned += 1
                aum_misaligned += aum
                if har.financed_gap_tco2e:
                    total_gap_tco2e += har.financed_gap_tco2e
            else:
                result.n_no_data += 1
                aum_no_data += aum

        result.holding_results = holding_alignment
        result.total_financed_gap_tco2e = total_gap_tco2e

        if total_aum > 0:
            result.pct_aum_aligned = aum_aligned / total_aum
            result.pct_aum_misaligned = aum_misaligned / total_aum
            result.pct_aum_ahead_of_path = aum_ahead / total_aum
            result.pct_aum_no_data = aum_no_data / total_aum

        # ── Temperature score — compute all three methods ──────────────────
        # Collect (aum_weight, intensity_gap_pct) for holdings with data
        temp_inputs: list[tuple[float, float]] = []
        for har in holding_alignment:
            if har.intensity_gap_pct is not None:
                ph = holding_map.get(har.holding_id)
                aum = ph.outstanding_amount_usd if ph else 0.0
                w = aum / total_aum if total_aum > 0 else 0.0
                temp_inputs.append((w, har.intensity_gap_pct))

        def _to_temp(avg_overshoot: float) -> float:
            """Convert weighted-average overshoot to approximate °C."""
            return 1.5 + max(0.0, avg_overshoot)

        if temp_inputs:
            total_w = sum(w for w, _ in temp_inputs)

            # Method 1 — no cap
            raw_avg = sum(w * g for w, g in temp_inputs) / total_w if total_w > 0 else 0.0
            result.implied_temperature_no_cap = _to_temp(raw_avg)

            # Method 2 — overshoot cap at N× benchmark (gap_pct capped at cap value)
            cap = self.overshoot_cap  # e.g. 10× means gap_pct capped at 10.0 (1000%)
            capped_avg = sum(w * min(g, cap) for w, g in temp_inputs) / total_w if total_w > 0 else 0.0
            result.implied_temperature_capped = _to_temp(capped_avg)

            # Method 3 — winsorise at Nth percentile of this portfolio's distribution
            sorted_gaps = sorted(g for _, g in temp_inputs)
            pct_idx = max(0, int(math.ceil(self.winsorise_pct * len(sorted_gaps))) - 1)
            winsorise_ceiling = sorted_gaps[pct_idx]
            wins_avg = sum(w * min(g, winsorise_ceiling) for w, g in temp_inputs) / total_w if total_w > 0 else 0.0
            result.implied_temperature_winsorised = _to_temp(wins_avg)

        # Set primary score based on chosen method
        result.temperature_method = self.temperature_method
        result.overshoot_cap_used = self.overshoot_cap
        result.winsorise_pct_used = self.winsorise_pct

        if self.temperature_method == TemperatureScoreMethod.NO_CAP:
            result.implied_temperature_c = result.implied_temperature_no_cap
        elif self.temperature_method == TemperatureScoreMethod.OVERSHOOT_CAP:
            result.implied_temperature_c = result.implied_temperature_capped
        else:
            result.implied_temperature_c = result.implied_temperature_winsorised

        # Top misaligned (by financed gap)
        misaligned = sorted(
            [h for h in holding_alignment if h.alignment_status == "misaligned"
             and h.financed_gap_tco2e is not None],
            key=lambda h: h.financed_gap_tco2e,
            reverse=True,
        )
        result.top_misaligned = [h.entity_name for h in misaligned[:5]]

        logger.info(
            "Alignment assessment [%d]: %.1f%% AUM aligned, %.1f%% misaligned, "
            "implied temperature ~%.1f°C",
            assessment_year,
            result.pct_aum_aligned * 100,
            result.pct_aum_misaligned * 100,
            result.implied_temperature_c or 0,
        )

        return result

    def _assess_holding(
        self,
        hr: HoldingEmissionsResult,
        rec: Optional[object],
        assessment_year: int,
        target_year: int,
    ) -> HoldingAlignmentResult:
        """Assess a single holding's alignment with the 1.5°C pathway."""

        sector = getattr(rec, 'gics_sector', None) if rec else None
        benchmark = SECTOR_BENCHMARKS.get(sector, DEFAULT_BENCHMARK) if sector else DEFAULT_BENCHMARK
        bench_intensity = benchmark.intensity_for_year(assessment_year)

        # Compute entity intensity
        entity_intensity = None
        revenue = getattr(rec, 'revenue_usd', None) if rec else None
        if revenue and revenue > 0:
            s1 = getattr(rec, 'scope_1_emissions', None)
            s2 = getattr(rec, 'scope_2_emissions', None)
            if s1 is not None or s2 is not None:
                total_s12 = (s1 or 0.0) + (s2 or 0.0)
                entity_intensity = total_s12 / (revenue / 1_000_000)

        # Compute gap
        intensity_gap = None
        intensity_gap_pct = None
        required_reduction = None
        alignment_status = "no_data"
        financed_gap = None

        if entity_intensity is not None:
            intensity_gap = entity_intensity - bench_intensity
            intensity_gap_pct = intensity_gap / bench_intensity if bench_intensity > 0 else None

            # Classify
            if intensity_gap <= 0:
                alignment_status = "ahead_of_path"
            elif intensity_gap <= bench_intensity * self.tolerance:
                alignment_status = "aligned"
            else:
                alignment_status = "misaligned"

            # Annual reduction needed to reach benchmark by target_year
            years_to_target = target_year - assessment_year
            if entity_intensity > 0 and years_to_target > 0:
                required_annual = 1 - (bench_intensity / entity_intensity) ** (1 / years_to_target)
                required_reduction = max(0.0, required_annual)

            # Financed gap in absolute tCO2e
            # = (entity intensity gap) × (attribution factor) × (entity revenue / $M)
            attr = hr.attribution_factor
            if attr is not None and revenue and intensity_gap > 0:
                financed_gap = intensity_gap * attr * (revenue / 1_000_000)

        return HoldingAlignmentResult(
            holding_id=hr.holding_id,
            entity_name=hr.entity_name,
            asset_class=hr.asset_class,
            outstanding_amount_usd=hr.outstanding_amount_usd,
            sector=sector,
            benchmark=benchmark,
            assessment_year=assessment_year,
            entity_intensity=entity_intensity,
            benchmark_intensity=bench_intensity,
            intensity_gap=intensity_gap,
            intensity_gap_pct=intensity_gap_pct,
            required_annual_reduction_pct=required_reduction,
            alignment_status=alignment_status,
            financed_gap_tco2e=financed_gap,
        )


# ---------------------------------------------------------------------------
# Reporting helper
# ---------------------------------------------------------------------------

STATUS_SYMBOLS = {
    "aligned":       "✓ Aligned",
    "ahead_of_path": "★ Ahead",
    "misaligned":    "✗ Misaligned",
    "no_data":       "○ No data",
}

def print_alignment_summary(result: PortfolioAlignmentResult) -> None:
    """Print a plain-English SBTi alignment summary to stdout."""
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"  SBTi 1.5°C PATHWAY ALIGNMENT  |  Portfolio: {result.portfolio_id}")
    print(f"  Assessment year: {result.assessment_year}  |  Target year: {result.target_year}")
    print(sep)

    print(f"\n▸ IMPLIED TEMPERATURE SCORE")
    method_labels = {
        TemperatureScoreMethod.NO_CAP:        "No cap (raw)",
        TemperatureScoreMethod.OVERSHOOT_CAP: f"Overshoot cap ({result.overshoot_cap_used:.0f}× benchmark)  [MSCI standard]",
        TemperatureScoreMethod.WINSORISE:     f"Winsorised ({result.winsorise_pct_used*100:.0f}th percentile)",
    }
    scores = [
        (TemperatureScoreMethod.NO_CAP,        result.implied_temperature_no_cap),
        (TemperatureScoreMethod.OVERSHOOT_CAP, result.implied_temperature_capped),
        (TemperatureScoreMethod.WINSORISE,     result.implied_temperature_winsorised),
    ]
    for method, temp in scores:
        primary = " ◀ primary" if method == result.temperature_method else ""
        temp_str = f"~{temp:.1f}°C" if temp is not None else "N/A"
        print(f"    {method_labels[method]:<48} {temp_str}{primary}")

    print(f"\n    Interpretation:")
    t = result.implied_temperature_c
    if t is not None:
        if t <= 1.6:
            print(f"    Portfolio is aligned with the 1.5°C Paris Agreement target.")
        elif t <= 2.0:
            print(f"    Portfolio is broadly aligned with well-below 2°C but not yet 1.5°C.")
        elif t <= 2.7:
            print(f"    Portfolio is misaligned — consistent with ~{t:.1f}°C of warming.")
        else:
            print(f"    Portfolio is significantly misaligned — high transition risk.")

        # Show outlier note when uncapped score is much higher than capped
        no_cap = result.implied_temperature_no_cap
        if (no_cap is not None and t is not None and
                no_cap - t > 1.0 and result.temperature_method != TemperatureScoreMethod.NO_CAP):
            print(f"\n    Note: The uncapped score ({no_cap:.1f}°C) is materially higher than "
                  f"the primary score ({t:.1f}°C). This gap is driven by one or more holdings "
                  f"with very high emissions relative to revenue (see misaligned holdings "
                  f"above). The capped score better represents the portfolio's central "
                  f"tendency; the uncapped score reflects worst-case outlier exposure.")

    print(f"\n▸ PORTFOLIO ALIGNMENT")
    print(f"    AUM aligned with 1.5°C:      {result.pct_aum_aligned*100:.1f}%")
    print(f"    AUM ahead of pathway:         {result.pct_aum_ahead_of_path*100:.1f}%")
    print(f"    AUM misaligned:               {result.pct_aum_misaligned*100:.1f}%")
    print(f"    AUM no data:                  {result.pct_aum_no_data*100:.1f}%")
    print(f"    Total financed gap:           {result.total_financed_gap_tco2e:,.0f} tCO2e")

    print(f"\n▸ HOLDING DETAIL")
    print(f"    {'Company':<35} {'Sector':<22} {'Status':<14} {'Gap %':>7}  {'Req. reduction':>14}")
    print(f"    {'─'*35} {'─'*22} {'─'*14} {'─'*7}  {'─'*14}")

    for h in sorted(result.holding_results,
                    key=lambda x: (x.alignment_status != 'misaligned',
                                   -(x.intensity_gap or 0))):
        status = STATUS_SYMBOLS.get(h.alignment_status, h.alignment_status)
        gap = f"{h.intensity_gap_pct*100:+.0f}%" if h.intensity_gap_pct is not None else "—"
        req = (f"{h.required_annual_reduction_pct*100:.1f}%/yr"
               if h.required_annual_reduction_pct else "—")
        sector = (h.sector or "—")[:22]
        print(f"    {h.entity_name:<35} {sector:<22} {status:<14} {gap:>7}  {req:>14}")

    if result.top_misaligned:
        print(f"\n▸ PRIORITY ENGAGEMENT")
        print(f"    Most misaligned holdings (by attributed emissions gap):")
        for name in result.top_misaligned:
            print(f"      • {name}")

    print(f"\n{sep}\n")
