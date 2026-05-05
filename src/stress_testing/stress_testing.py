"""
Module 4a — Transition Risk: Carbon Price Stress Test
======================================================
Models the financial impact of carbon pricing on each portfolio holding
under the three NGFS Phase 4 climate scenarios.

What this module does
---------------------
For each holding in the portfolio:

  1.  Look up the entity's Scope 1+2 emissions (direct carbon exposure).
  2.  Apply the NGFS carbon price for the chosen scenario and horizon year.
  3.  Compute the implied annual carbon cost = emissions × carbon price.
  4.  Express that cost as a % of the entity's revenue (revenue proxy for
      EBITDA — a standard approximation when EBITDA is not disclosed).
  5.  Flag holdings where carbon cost exceeds a configurable risk threshold.

The three NGFS scenarios
------------------------
Orderly (Net Zero 2050)
    Early, predictable policy action starting now.  Carbon prices rise
    steadily.  High near-term cost but transition risk is manageable.
    Typical 2030 price: ~$130/tCO2e.  2050: ~$250/tCO2e.

Disorderly (Delayed Transition)
    Policy delayed until 2030, then abrupt action.  Carbon prices spike
    sharply after 2030.  Highest transition risk for carbon-intensive assets.
    Typical 2030 price: ~$60/tCO2e (low pre-action), then ~$600/tCO2e by 2050.

Hot House World (Current Policies)
    No new climate policies.  Low carbon prices, high physical risk.
    Transition risk is low but physical damages (floods, heat, drought)
    accelerate after 2040.  Typical 2030 price: ~$15/tCO2e.

Price paths are calibrated to NGFS Phase 4 (2023) medians, converted to
2022 USD using a 2% annual deflator.

References
----------
- NGFS Phase 4 Climate Scenarios (2023): https://www.ngfs.net/ngfs-scenarios-portal
- GFANZ Transition Finance Metrics Framework (2023) §4
- ECB Economy-Wide Climate Stress Test (2021)
- TCFD Guidance on Scenario Analysis (2020)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from src.models import (
    AssetClass,
    NGFSScenario,
    CarbonPriceScenario,
    StressTestResult,
    Portfolio,
    PortfolioEmissionsResult,
)
from src.pcaf_engine.engine import HoldingEmissionsResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NGFS Phase 4 carbon price paths (USD/tCO2e, 2022 real terms)
# Sourced from NGFS Scenario Explorer, IEA NZE pathway, median estimates
# ---------------------------------------------------------------------------

NGFS_PRICE_PATHS: dict[NGFSScenario, dict[int, float]] = {

    # Orderly: Net Zero 2050 — steady price escalation
    # Reflects carbon markets pricing in early, credible policy commitments
    NGFSScenario.ORDERLY: {
        2023: 30,   2024: 38,   2025: 47,
        2026: 57,   2027: 68,   2028: 80,
        2029: 93,   2030: 108,  2031: 118,
        2032: 128,  2033: 139,  2034: 151,
        2035: 164,  2040: 200,  2045: 230,
        2050: 250,
    },

    # Disorderly: Delayed Transition — low early, then sharp spike post-2030
    # Policy inaction followed by emergency measures creates price volatility
    NGFSScenario.DISORDERLY: {
        2023: 10,   2024: 12,   2025: 14,
        2026: 16,   2027: 19,   2028: 22,
        2029: 25,   2030: 60,   2031: 110,
        2032: 175,  2033: 250,  2034: 320,
        2035: 400,  2040: 500,  2045: 560,
        2050: 600,
    },

    # Hot House World: Current Policies — minimal price escalation
    # Carbon pricing exists but is too low to drive meaningful transition
    NGFSScenario.HOT_HOUSE: {
        2023: 5,    2024: 6,    2025: 7,
        2026: 8,    2027: 9,    2028: 10,
        2029: 11,   2030: 13,   2031: 14,
        2032: 15,   2033: 16,   2034: 17,
        2035: 18,   2040: 22,   2045: 26,
        2050: 30,
    },
}

NGFS_SCENARIO_LABELS: dict[NGFSScenario, str] = {
    NGFSScenario.ORDERLY:     "Orderly (Net Zero 2050)",
    NGFSScenario.DISORDERLY:  "Disorderly (Delayed Transition)",
    NGFSScenario.HOT_HOUSE:   "Hot House World (Current Policies)",
}

# Default risk threshold: carbon cost > 5% of revenue = high risk
DEFAULT_RISK_THRESHOLD_PCT: float = 0.05


# ---------------------------------------------------------------------------
# Per-holding stress test result
# ---------------------------------------------------------------------------

@dataclass
class HoldingStressResult:
    """
    Carbon price stress test output for a single holding.

    Computes carbon cost exposure in USD and as a % of revenue,
    then classifies the holding as high / medium / low risk.
    """
    holding_id: str
    entity_name: str
    asset_class: AssetClass
    outstanding_amount_usd: float

    scenario: NGFSScenario
    horizon_year: int
    carbon_price_usd_per_tco2e: float

    # Entity-level figures (gross, before attribution)
    entity_scope_12_tco2e: Optional[float]       # Gross Scope 1+2
    entity_revenue_usd: Optional[float]

    # Our share (post-attribution)
    financed_scope_12_tco2e: Optional[float]     # Our attributed Scope 1+2
    implied_carbon_cost_usd: Optional[float]     # financed_S12 × carbon_price
    carbon_cost_pct_revenue: Optional[float]     # implied_cost / entity_revenue

    # Risk classification
    risk_flag: str = "unknown"   # "high" | "medium" | "low" | "no_data"

    # Whether this holding qualifies as high-risk
    @property
    def is_high_risk(self) -> bool:
        return self.risk_flag == "high"


# ---------------------------------------------------------------------------
# Portfolio stress test result
# ---------------------------------------------------------------------------

@dataclass
class PortfolioStressResult:
    """
    Portfolio-level transition risk output for one scenario and horizon year.

    Aggregates holding-level carbon cost exposures and provides
    portfolio-wide risk metrics suitable for TCFD scenario analysis disclosure.
    """
    portfolio_id: str
    scenario: NGFSScenario
    scenario_label: str
    horizon_year: int
    carbon_price_usd_per_tco2e: float

    # Portfolio aggregate carbon cost (our attributed share × carbon price)
    total_carbon_cost_usd: float = 0.0

    # Carbon cost as % of total portfolio revenue (where available)
    # Approximates EBITDA at risk — a key TCFD transition risk metric
    # NOTE: This blended figure is unreliable when sovereign debt is present
    # because government revenue dwarfs corporate revenue.  Use the
    # per-asset-class breakdown below for a meaningful picture.
    portfolio_carbon_cost_pct_revenue: Optional[float] = None
    has_sovereign_debt: bool = False   # Warning flag for blended % figure

    # AUM-weighted average carbon cost % across holdings with revenue data
    wacr: Optional[float] = None  # Weighted Average Carbon Risk %

    # Per-asset-class breakdown
    # dict[asset_class_value → {carbon_cost_usd, n_holdings, n_high_risk,
    #                            pct_revenue, aum_usd}]
    by_asset_class: dict[str, dict] = field(default_factory=dict)

    # Risk breakdown
    n_high_risk: int = 0
    n_medium_risk: int = 0
    n_low_risk: int = 0
    n_no_data: int = 0
    high_risk_aum_pct: float = 0.0   # % of total AUM in high-risk holdings

    # Per-holding results
    holding_results: list[HoldingStressResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Carbon price interpolation
# ---------------------------------------------------------------------------

def _interpolate_price(price_path: dict[int, float], year: int) -> float:
    """
    Linear interpolation between known price path anchor points.

    NGFS paths are published at irregular intervals (annual near-term,
    5-year intervals in the mid-term).  This fills in intermediate years.
    """
    years = sorted(price_path.keys())

    if year <= years[0]:
        return price_path[years[0]]
    if year >= years[-1]:
        return price_path[years[-1]]

    # Find surrounding anchor points
    lower = max(y for y in years if y <= year)
    upper = min(y for y in years if y >= year)

    if lower == upper:
        return price_path[lower]

    # Linear interpolation
    frac = (year - lower) / (upper - lower)
    return price_path[lower] + frac * (price_path[upper] - price_path[lower])


def get_carbon_price(scenario: NGFSScenario, year: int) -> float:
    """Return the NGFS Phase 4 carbon price (USD/tCO2e) for a given scenario and year."""
    path = NGFS_PRICE_PATHS.get(scenario)
    if path is None:
        raise ValueError(f"Unknown scenario: {scenario}")
    return _interpolate_price(path, year)


# ---------------------------------------------------------------------------
# Stress test engine
# ---------------------------------------------------------------------------

class CarbonPriceStressTester:
    """
    Runs NGFS carbon price stress tests against a portfolio.

    Usage
    -----
    >>> tester = CarbonPriceStressTester()
    >>> results = tester.run_all_scenarios(
    ...     portfolio, holding_results, emissions_records, horizon_year=2030
    ... )

    Or run a single scenario:
    >>> result = tester.run(
    ...     portfolio, holding_results, emissions_records,
    ...     scenario=NGFSScenario.DISORDERLY, horizon_year=2030
    ... )
    """

    def __init__(self, risk_threshold_pct: float = DEFAULT_RISK_THRESHOLD_PCT) -> None:
        """
        Parameters
        ----------
        risk_threshold_pct : Carbon cost / revenue above this → "high risk".
            Default 5% (i.e. carbon costs exceed 5% of annual revenue).
        """
        self.risk_threshold_pct = risk_threshold_pct
        self.medium_threshold_pct = risk_threshold_pct / 2  # 2.5% = medium

    def run_all_scenarios(
        self,
        portfolio: Portfolio,
        holding_results: list[HoldingEmissionsResult],
        emissions_records: dict[str, object],   # dict[entity_id → EmissionsRecord]
        horizon_year: int = 2030,
    ) -> dict[NGFSScenario, PortfolioStressResult]:
        """Run all three NGFS scenarios and return results keyed by scenario."""
        return {
            scenario: self.run(
                portfolio, holding_results, emissions_records,
                scenario=scenario, horizon_year=horizon_year,
            )
            for scenario in NGFSScenario
        }

    def run(
        self,
        portfolio: Portfolio,
        holding_results: list[HoldingEmissionsResult],
        emissions_records: dict[str, object],
        scenario: NGFSScenario,
        horizon_year: int = 2030,
    ) -> PortfolioStressResult:
        """
        Run a single NGFS scenario stress test.

        Parameters
        ----------
        portfolio : The portfolio being stress-tested.
        holding_results : Per-holding engine output (from PCАFEngine.run()).
        emissions_records : Raw emissions records keyed by entity_id.
        scenario : Which NGFS scenario to apply.
        horizon_year : The year to read the carbon price for (e.g. 2030).

        Returns
        -------
        PortfolioStressResult with per-holding and aggregate metrics.
        """
        carbon_price = get_carbon_price(scenario, horizon_year)

        port_result = PortfolioStressResult(
            portfolio_id=portfolio.portfolio_id,
            scenario=scenario,
            scenario_label=NGFS_SCENARIO_LABELS[scenario],
            horizon_year=horizon_year,
            carbon_price_usd_per_tco2e=carbon_price,
        )

        # Build a lookup from holding_id → PortfolioHolding for revenue access
        holding_map = {h.holding_id: h for h in portfolio.holdings}

        # Build entity_id lookup from portfolio holdings
        entity_to_holding = {}
        for h in portfolio.holdings:
            entity_to_holding[h.entity_id] = h.holding_id

        total_aum = portfolio.total_aum_usd
        total_carbon_cost = 0.0
        total_revenue_with_data = 0.0
        total_cost_with_revenue = 0.0
        wacr_sum = 0.0
        high_risk_aum = 0.0
        has_sovereign = False

        # Per-asset-class accumulators
        # ac_buckets[ac_value] = {cost, revenue, n, n_high, aum}
        ac_buckets: dict[str, dict] = {}

        holding_stress_results: list[HoldingStressResult] = []

        for hr in holding_results:
            rec = emissions_records.get(
                holding_map[hr.holding_id].entity_id
                if hr.holding_id in holding_map else hr.holding_id
            )
            hsr = self._stress_holding(hr, rec, carbon_price, scenario, horizon_year)
            holding_stress_results.append(hsr)

            ac = hr.asset_class.value
            if ac not in ac_buckets:
                ac_buckets[ac] = {
                    "carbon_cost_usd": 0.0,
                    "revenue_usd": 0.0,
                    "n_holdings": 0,
                    "n_high_risk": 0,
                    "aum_usd": 0.0,
                }
            ac_buckets[ac]["n_holdings"] += 1
            ac_buckets[ac]["aum_usd"] += hr.outstanding_amount_usd

            if hsr.implied_carbon_cost_usd is not None:
                total_carbon_cost += hsr.implied_carbon_cost_usd
                ac_buckets[ac]["carbon_cost_usd"] += hsr.implied_carbon_cost_usd

            if hsr.entity_revenue_usd and hsr.implied_carbon_cost_usd is not None:
                total_revenue_with_data += hsr.entity_revenue_usd
                total_cost_with_revenue += hsr.implied_carbon_cost_usd
                ac_buckets[ac]["revenue_usd"] += hsr.entity_revenue_usd

            if ac == AssetClass.SOVEREIGN_DEBT.value:
                has_sovereign = True

            # Weighted average carbon risk (weight by AUM)
            if hsr.carbon_cost_pct_revenue is not None and total_aum > 0:
                weight = hr.outstanding_amount_usd / total_aum
                wacr_sum += weight * hsr.carbon_cost_pct_revenue

            # Risk counters
            if hsr.risk_flag == "high":
                port_result.n_high_risk += 1
                high_risk_aum += hr.outstanding_amount_usd
                ac_buckets[ac]["n_high_risk"] += 1
            elif hsr.risk_flag == "medium":
                port_result.n_medium_risk += 1
            elif hsr.risk_flag == "low":
                port_result.n_low_risk += 1
            else:
                port_result.n_no_data += 1

        # Compute per-asset-class % revenue
        for ac, bucket in ac_buckets.items():
            bucket["pct_revenue"] = (
                bucket["carbon_cost_usd"] / bucket["revenue_usd"]
                if bucket["revenue_usd"] > 0 else None
            )

        port_result.holding_results = holding_stress_results
        port_result.total_carbon_cost_usd = total_carbon_cost
        port_result.high_risk_aum_pct = high_risk_aum / total_aum if total_aum > 0 else 0.0
        port_result.wacr = wacr_sum if wacr_sum > 0 else None
        port_result.by_asset_class = ac_buckets
        port_result.has_sovereign_debt = has_sovereign

        if total_revenue_with_data > 0:
            port_result.portfolio_carbon_cost_pct_revenue = (
                total_cost_with_revenue / total_revenue_with_data
            )

        logger.info(
            "Stress test [%s, %d]: carbon price=$%.0f/tCO2e, "
            "total cost=$%.1fM, high-risk holdings=%d",
            scenario.value, horizon_year, carbon_price,
            total_carbon_cost / 1e6, port_result.n_high_risk,
        )

        return port_result

    def _stress_holding(
        self,
        hr: HoldingEmissionsResult,
        rec: Optional[object],
        carbon_price: float,
        scenario: NGFSScenario,
        horizon_year: int,
    ) -> HoldingStressResult:
        """Compute carbon cost exposure for one holding."""

        entity_s12 = None
        revenue = None

        if rec is not None:
            s1 = getattr(rec, 'scope_1_emissions', None)
            s2 = getattr(rec, 'scope_2_emissions', None)
            if s1 is not None or s2 is not None:
                entity_s12 = (s1 or 0.0) + (s2 or 0.0)
            revenue = getattr(rec, 'revenue_usd', None)

        financed_s12 = hr.financed_scope_1_tco2e
        if financed_s12 is not None and hr.financed_scope_2_tco2e is not None:
            financed_s12 = financed_s12 + hr.financed_scope_2_tco2e
        elif hr.financed_scope_2_tco2e is not None:
            financed_s12 = hr.financed_scope_2_tco2e

        implied_cost = (financed_s12 * carbon_price) if financed_s12 is not None else None
        cost_pct_rev = None
        if implied_cost is not None and revenue and revenue > 0:
            cost_pct_rev = implied_cost / revenue

        # Risk classification
        if cost_pct_rev is None:
            risk = "no_data"
        elif cost_pct_rev >= self.risk_threshold_pct:
            risk = "high"
        elif cost_pct_rev >= self.medium_threshold_pct:
            risk = "medium"
        else:
            risk = "low"

        return HoldingStressResult(
            holding_id=hr.holding_id,
            entity_name=hr.entity_name,
            asset_class=hr.asset_class,
            outstanding_amount_usd=hr.outstanding_amount_usd,
            scenario=scenario,
            horizon_year=horizon_year,
            carbon_price_usd_per_tco2e=carbon_price,
            entity_scope_12_tco2e=entity_s12,
            entity_revenue_usd=revenue,
            financed_scope_12_tco2e=financed_s12,
            implied_carbon_cost_usd=implied_cost,
            carbon_cost_pct_revenue=cost_pct_rev,
            risk_flag=risk,
        )


# ---------------------------------------------------------------------------
# Reporting helper
# ---------------------------------------------------------------------------

def print_stress_summary(results: dict[NGFSScenario, PortfolioStressResult]) -> None:
    """Print a TCFD-style transition risk summary across all three scenarios."""
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"  TRANSITION RISK — CARBON PRICE STRESS TEST")
    first = next(iter(results.values()))
    print(f"  Portfolio: {first.portfolio_id}  |  Horizon: {first.horizon_year}")
    print(sep)

    print(f"\n▸ SCENARIO COMPARISON")
    print(f"    {'Scenario':<38} {'Price':>7}  {'Carbon Cost':>12}  {'High Risk':>9}")
    print(f"    {'─'*38} {'─'*7}  {'─'*12}  {'─'*9}")
    for scenario, r in results.items():
        price = f"${r.carbon_price_usd_per_tco2e:.0f}"
        cost = f"${r.total_carbon_cost_usd/1e6:.1f}M"
        high = f"{r.n_high_risk} holdings"
        print(f"    {r.scenario_label:<38} {price:>7}  {cost:>12}  {high:>9}")

    # Per-asset-class breakdown — show for the orderly scenario (middle ground)
    orderly_result = results.get(NGFSScenario.ORDERLY, first)
    print(f"\n▸ CARBON COST BY ASSET CLASS  "
          f"[{orderly_result.scenario_label}, ${orderly_result.carbon_price_usd_per_tco2e:.0f}/tCO2e]")

    has_sovereign = orderly_result.has_sovereign_debt
    AC_LABELS = {
        "listed_equity_and_corporate_bonds":    "Listed equity / corp bonds",
        "business_loans_and_unlisted_equity":   "Business loans / unlisted eq.",
        "project_finance":                      "Project finance",
        "commercial_real_estate":               "Commercial real estate",
        "mortgages":                            "Mortgages",
        "motor_vehicle_loans":                  "Motor vehicle loans",
        "sovereign_debt":                       "Sovereign debt *" if has_sovereign else "Sovereign debt",
    }

    print(f"    {'Asset class':<34} {'AUM ($M)':>8}  {'Carbon cost':>12}  {'% Revenue':>10}  {'High risk':>9}")
    print(f"    {'─'*34} {'─'*8}  {'─'*12}  {'─'*10}  {'─'*9}")

    for ac, bucket in sorted(orderly_result.by_asset_class.items(),
                              key=lambda x: x[1]["carbon_cost_usd"], reverse=True):
        label = AC_LABELS.get(ac, ac)
        aum = f"{bucket['aum_usd']/1e6:.1f}"
        cost = f"${bucket['carbon_cost_usd']/1e6:.2f}M"
        pct = (f"{bucket['pct_revenue']*100:.1f}%"
               if bucket.get('pct_revenue') is not None else "N/A")
        hi = f"{bucket['n_high_risk']}" if bucket['n_high_risk'] > 0 else "—"
        print(f"    {label:<34} {aum:>8}  {cost:>12}  {pct:>10}  {hi:>9}")

    if has_sovereign:
        print(f"\n    * Sovereign debt: % Revenue uses government tax receipts as denominator.")
        print(f"      This produces near-zero % figures and is not comparable to corporate")
        print(f"      holdings. Sovereign transition risk is better assessed via carbon")
        print(f"      intensity of GDP (tCO2e / $M GDP) rather than revenue.")

    print(f"\n▸ HIGH-RISK HOLDINGS (carbon cost > 5% of own revenue, any scenario)")
    all_high: set[str] = set()
    for r in results.values():
        for hr in r.holding_results:
            if hr.is_high_risk:
                all_high.add(hr.holding_id)

    if not all_high:
        print("    No high-risk holdings identified under any scenario.")
    else:
        print(f"    {'Company':<35} {'Orderly':>10}  {'Disorderly':>10}  {'Hot House':>10}")
        print(f"    {'─'*35} {'─'*10}  {'─'*10}  {'─'*10}")
        for holding_id in sorted(all_high):
            row_parts = []
            name = ""
            for scenario in NGFSScenario:
                r = results.get(scenario)
                if r:
                    hr = next((h for h in r.holding_results
                               if h.holding_id == holding_id), None)
                    if hr:
                        name = hr.entity_name
                        pct = (f"{hr.carbon_cost_pct_revenue*100:.1f}%"
                               if hr.carbon_cost_pct_revenue else "N/A")
                        flag = "▲ " if hr.is_high_risk else "  "
                        row_parts.append(f"{flag}{pct}")
                    else:
                        row_parts.append("—")
            print(f"    {name:<35} {row_parts[0]:>10}  {row_parts[1]:>10}  {row_parts[2]:>10}")

    print(f"\n{sep}\n")
