"""
GHG Portfolio Analyzer — Core Data Models
==========================================
Implements PCAF-aligned schemas for all 7 asset classes.

References:
  - PCAF Global GHG Accounting and Reporting Standard for the Financial Industry (2022)
  - GHG Protocol Corporate Value Chain (Scope 3) Standard
  - GFANZ Transition Finance Metrics Framework (2023)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import datetime


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AssetClass(str, Enum):
    """PCAF's 7 asset classes for financed emissions reporting."""
    LISTED_EQUITY_CORP_BONDS = "listed_equity_and_corporate_bonds"
    BUSINESS_LOANS_UNLISTED_EQUITY = "business_loans_and_unlisted_equity"
    PROJECT_FINANCE = "project_finance"
    COMMERCIAL_REAL_ESTATE = "commercial_real_estate"
    MORTGAGES = "mortgages"
    MOTOR_VEHICLE_LOANS = "motor_vehicle_loans"
    SOVEREIGN_DEBT = "sovereign_debt"


class EmissionsScope(str, Enum):
    SCOPE_1 = "scope_1"
    SCOPE_2 = "scope_2"
    SCOPE_3 = "scope_3"


class DataQualityScore(int, Enum):
    """
    PCAF Data Quality Scoring System (1 = best, 5 = worst).

    Score 1: Audited / third-party verified reported data.   Error margin: ~5-10%
    Score 2: Reported but unaudited primary data.            Error margin: ~10-20%
    Score 3: Sector-specific average / proxy data.           Error margin: ~20-30%
    Score 4: Regional or country-average proxy data.         Error margin: ~30-40%
    Score 5: Estimated data with limited support.            Error margin: ~40-50%
    """
    VERIFIED_REPORTED = 1
    REPORTED_UNAUDITED = 2
    SECTOR_AVERAGE = 3
    REGIONAL_PROXY = 4
    ESTIMATED = 5


class EmissionsEstimationMethod(str, Enum):
    """Fallback estimation hierarchy when reported data is unavailable."""
    REPORTED_VERIFIED = "reported_verified"          # DQ Score 1
    REPORTED_UNAUDITED = "reported_unaudited"        # DQ Score 2
    EEIO_SPEND_BASED = "eeio_spend_based"            # DQ Score 3-4
    SECTOR_INTENSITY = "sector_intensity_revenue"    # DQ Score 4
    REGIONAL_PROXY = "regional_proxy"               # DQ Score 5


class NGFSScenario(str, Enum):
    """NGFS climate scenarios for stress testing."""
    ORDERLY = "net_zero_2050_orderly"           # Early, predictable policy action
    DISORDERLY = "delayed_transition_disorderly" # Late, abrupt policy action
    HOT_HOUSE = "current_policies_hot_house"    # No new climate policies


class EmissionsUnit(str, Enum):
    TCO2E = "tCO2e"
    KTCO2E = "ktCO2e"
    MTCO2E = "MtCO2e"


# ---------------------------------------------------------------------------
# Emissions data at the company / asset level
# ---------------------------------------------------------------------------

@dataclass
class EmissionsRecord:
    """
    Reported or estimated GHG emissions for a single company or asset.
    Covers all three scopes with PCAF data quality scoring per scope.

    The data quality score drives uncertainty bounds on financed emissions,
    consistent with PCAF Figure 10.7.
    """
    entity_id: str                           # Internal unique ID (ISIN, loan ID, etc.)
    reporting_year: int

    # --- Absolute emissions (tCO2e) ---
    scope_1_emissions: Optional[float] = None
    scope_2_emissions: Optional[float] = None
    scope_3_upstream_emissions: Optional[float] = None
    scope_3_downstream_emissions: Optional[float] = None

    # --- PCAF Data Quality Scores (1–5) per scope ---
    scope_1_dq_score: DataQualityScore = DataQualityScore.ESTIMATED
    scope_2_dq_score: DataQualityScore = DataQualityScore.ESTIMATED
    scope_3_dq_score: DataQualityScore = DataQualityScore.ESTIMATED

    # --- Estimation methods used (for audit trail) ---
    scope_1_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY
    scope_2_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY
    scope_3_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY

    # --- Financial context (needed for intensity metrics) ---
    revenue_usd: Optional[float] = None              # For WACI calculation
    enterprise_value_incl_cash: Optional[float] = None  # EVIC — PCAF denominator
    total_equity_and_debt: Optional[float] = None    # Alternative denominator

    # --- Sector classification (for proxy fallback) ---
    gics_sector: Optional[str] = None
    gics_industry_group: Optional[str] = None
    nace_code: Optional[str] = None
    country_iso3: Optional[str] = None

    # --- Scope 3 de-duplication flag ---
    scope_3_dedup_applied: bool = False
    scope_3_dedup_multiplier: Optional[float] = None  # e.g. 0.205 (MSCI methodology)

    @property
    def total_scope_3(self) -> Optional[float]:
        upstream = self.scope_3_upstream_emissions or 0.0
        downstream = self.scope_3_downstream_emissions or 0.0
        if self.scope_3_upstream_emissions is None and self.scope_3_downstream_emissions is None:
            return None
        return upstream + downstream

    @property
    def total_emissions(self) -> Optional[float]:
        """Scope 1 + 2 + 3 total. Returns None if all scopes are missing."""
        parts = [self.scope_1_emissions, self.scope_2_emissions, self.total_scope_3]
        non_null = [p for p in parts if p is not None]
        return sum(non_null) if non_null else None

    @property
    def weighted_dq_score(self) -> Optional[float]:
        """
        Emissions-weighted average DQ score across available scopes.
        Lower is better. Used for portfolio-level data quality reporting.
        """
        weights, scores = [], []
        pairs = [
            (self.scope_1_emissions, self.scope_1_dq_score),
            (self.scope_2_emissions, self.scope_2_dq_score),
            (self.total_scope_3, self.scope_3_dq_score),
        ]
        for emissions, dq in pairs:
            if emissions is not None and emissions > 0:
                weights.append(emissions)
                scores.append(dq.value)
        if not weights:
            return None
        return sum(w * s for w, s in zip(weights, scores)) / sum(weights)

    @property
    def error_margin_pct(self) -> Optional[float]:
        """Approximate error margin based on weighted DQ score."""
        score = self.weighted_dq_score
        if score is None:
            return None
        margins = {1: 0.075, 2: 0.15, 3: 0.25, 4: 0.35, 5: 0.45}
        # Interpolate between integer score buckets
        lower = int(score)
        upper = min(lower + 1, 5)
        frac = score - lower
        return margins[lower] * (1 - frac) + margins[upper] * frac


# ---------------------------------------------------------------------------
# Portfolio holding — one row per position
# ---------------------------------------------------------------------------

@dataclass
class PortfolioHolding:
    """
    Represents a single financial institution's exposure to an entity.
    The PCAF attribution factor allocates a proportional share of the
    investee's emissions to the institution.

    Attribution factor = outstanding amount / (equity + debt of investee)
    Financed emissions = attribution factor × entity total emissions
    """
    holding_id: str
    portfolio_id: str
    asset_class: AssetClass
    reporting_date: datetime.date

    # --- Counterparty identification ---
    entity_id: str                  # Links to EmissionsRecord.entity_id
    entity_name: str
    isin: Optional[str] = None
    lei: Optional[str] = None       # Legal Entity Identifier
    country_iso3: Optional[str] = None

    # --- Exposure (in USD) ---
    outstanding_amount_usd: float = 0.0   # Loan balance / market value of holding

    # --- PCAF attribution factor denominators (asset-class specific) ---
    # For listed equity / corp bonds: use EVIC
    evic_usd: Optional[float] = None
    # For business loans / unlisted equity: use total equity + debt
    total_equity_debt_usd: Optional[float] = None
    # For project finance / real estate: use total project value
    total_project_value_usd: Optional[float] = None
    # For mortgages / motor vehicle: use property/vehicle value at origination
    collateral_value_usd: Optional[float] = None
    # For sovereign debt: use government revenue
    government_revenue_usd: Optional[float] = None

    # --- Computed by engine (set after processing) ---
    attribution_factor: Optional[float] = None
    financed_emissions_tco2e: Optional[float] = None
    financed_emissions_dq_score: Optional[float] = None

    @property
    def attribution_denominator(self) -> Optional[float]:
        """
        Returns the correct PCAF denominator for this asset class.
        The denominator determines how much of the investee's emissions
        are proportionally attributed to this institution.
        """
        mapping = {
            AssetClass.LISTED_EQUITY_CORP_BONDS: self.evic_usd,
            AssetClass.BUSINESS_LOANS_UNLISTED_EQUITY: self.total_equity_debt_usd,
            AssetClass.PROJECT_FINANCE: self.total_project_value_usd,
            AssetClass.COMMERCIAL_REAL_ESTATE: self.total_project_value_usd,
            AssetClass.MORTGAGES: self.collateral_value_usd,
            AssetClass.MOTOR_VEHICLE_LOANS: self.collateral_value_usd,
            AssetClass.SOVEREIGN_DEBT: self.government_revenue_usd,
        }
        return mapping.get(self.asset_class)


# ---------------------------------------------------------------------------
# Portfolio — collection of holdings
# ---------------------------------------------------------------------------

@dataclass
class Portfolio:
    """
    A named portfolio of holdings across one or more asset classes.
    The portfolio is the unit of analysis for WACI, carbon intensity,
    de-duplication, and stress testing.
    """
    portfolio_id: str
    portfolio_name: str
    base_currency: str = "USD"
    reporting_date: Optional[datetime.date] = None
    holdings: list[PortfolioHolding] = field(default_factory=list)

    @property
    def total_aum_usd(self) -> float:
        return sum(h.outstanding_amount_usd for h in self.holdings)

    @property
    def asset_class_breakdown(self) -> dict[AssetClass, float]:
        breakdown: dict[AssetClass, float] = {}
        for h in self.holdings:
            breakdown[h.asset_class] = breakdown.get(h.asset_class, 0.0) + h.outstanding_amount_usd
        return breakdown


# ---------------------------------------------------------------------------
# Portfolio metrics (output of the PCAF engine)
# ---------------------------------------------------------------------------

@dataclass
class PortfolioEmissionsResult:
    """
    Computed emissions metrics for a portfolio.
    Implements all four metric types from PCAF / GFANZ Table 10.5.
    """
    portfolio_id: str
    reporting_year: int
    computed_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)

    # --- Absolute emissions (tCO2e) ---
    total_financed_emissions_tco2e: float = 0.0
    scope_1_financed_tco2e: float = 0.0
    scope_2_financed_tco2e: float = 0.0
    scope_3_financed_tco2e: float = 0.0
    scope_3_financed_tco2e_dedup: float = 0.0   # After de-duplication multiplier

    # --- Intensity metrics ---
    # WACI: portfolio-weighted average (tCO2e / $M revenue)
    waci_tco2e_per_mrevenue: Optional[float] = None
    # Economic intensity: total financed emissions / total AUM
    economic_intensity_tco2e_per_musd: Optional[float] = None

    # --- Data quality ---
    portfolio_weighted_dq_score: Optional[float] = None
    pct_holdings_with_reported_data: float = 0.0  # DQ score 1 or 2
    pct_holdings_estimated: float = 0.0           # DQ score 4 or 5

    # --- Coverage ---
    n_holdings_total: int = 0
    n_holdings_with_emissions: int = 0
    aum_coverage_pct: float = 0.0   # % of AUM with any emissions data

    # --- Per asset class breakdown ---
    by_asset_class: dict[str, dict] = field(default_factory=dict)

    # --- Scope 3 de-duplication metadata ---
    dedup_multiplier_applied: Optional[float] = None
    scope_3_raw_tco2e: Optional[float] = None     # Before de-duplication


# ---------------------------------------------------------------------------
# Stress test inputs / outputs
# ---------------------------------------------------------------------------

@dataclass
class CarbonPriceScenario:
    """
    A single NGFS carbon price path for transition risk stress testing.
    Price paths are in USD/tCO2e for each year.
    """
    scenario: NGFSScenario
    scenario_label: str
    price_path: dict[int, float]   # {year: USD/tCO2e}
    source: str = "NGFS Phase 4 (2023)"


@dataclass
class StressTestResult:
    """
    Output of running a carbon price scenario against a portfolio.
    Models EBITDA impact → emissions cost exposure per holding.
    """
    portfolio_id: str
    scenario: NGFSScenario
    horizon_year: int

    # --- Aggregate portfolio impact ---
    total_carbon_cost_usd: float = 0.0        # Total implied carbon cost
    pct_ebitda_at_risk: Optional[float] = None  # Carbon cost / portfolio EBITDA
    high_risk_holdings: list[str] = field(default_factory=list)  # holding_ids

    # --- SBTi pathway alignment ---
    pct_portfolio_paris_aligned: Optional[float] = None   # By AUM weight
    pct_portfolio_misaligned: Optional[float] = None
    aggregate_alignment_gap_tco2e: Optional[float] = None  # Excess emissions vs 1.5°C path


# ---------------------------------------------------------------------------
# Sample CSV column specs (for documentation and validation)
# ---------------------------------------------------------------------------

PORTFOLIO_CSV_COLUMNS = {
    # Required
    "holding_id":             "str   — unique ID for this position",
    "portfolio_id":           "str   — which portfolio this belongs to",
    "entity_name":            "str   — company / asset name",
    "asset_class":            "str   — one of the 7 PCAF asset classes (use enum values)",
    "outstanding_amount_usd": "float — exposure in USD",
    "reporting_date":         "date  — YYYY-MM-DD",

    # Counterparty identifiers (at least one recommended)
    "isin":                   "str   — ISIN (optional)",
    "lei":                    "str   — Legal Entity Identifier (optional)",
    "country_iso3":           "str   — ISO 3166-1 alpha-3 country code",

    # PCAF attribution denominators (asset-class dependent)
    "evic_usd":               "float — Enterprise Value incl. Cash (listed equity / corp bonds)",
    "total_equity_debt_usd":  "float — Total equity + debt (business loans / unlisted equity)",
    "total_project_value_usd":"float — Total project value (project finance / commercial RE)",
    "collateral_value_usd":   "float — Collateral value at origination (mortgages / motor vehicles)",
    "government_revenue_usd": "float — Government revenue (sovereign debt)",
}

EMISSIONS_CSV_COLUMNS = {
    # Required
    "entity_id":              "str   — links to portfolio holding entity_id",
    "reporting_year":         "int   — fiscal year of emissions data",

    # Scope emissions (tCO2e) — provide whatever is available
    "scope_1_emissions":      "float — tCO2e Scope 1 (direct, owned sources)",
    "scope_2_emissions":      "float — tCO2e Scope 2 (purchased energy)",
    "scope_3_upstream":       "float — tCO2e Scope 3 upstream (value chain)",
    "scope_3_downstream":     "float — tCO2e Scope 3 downstream (use of products sold)",

    # Data quality scores (1–5; leave blank to trigger auto-estimation)
    "scope_1_dq_score":       "int   — PCAF DQ score for Scope 1",
    "scope_2_dq_score":       "int   — PCAF DQ score for Scope 2",
    "scope_3_dq_score":       "int   — PCAF DQ score for Scope 3",

    # Financial context (for intensity metrics)
    "revenue_usd":            "float — annual revenue in USD (for WACI)",
    "evic_usd":               "float — EVIC in USD",
    "total_equity_debt_usd":  "float — total equity + debt in USD",

    # Sector / region (for proxy estimation if DQ score missing)
    "gics_sector":            "str   — GICS sector name",
    "gics_industry_group":    "str   — GICS industry group",
    "nace_code":              "str   — NACE Rev. 2 code",
    "country_iso3":           "str   — ISO 3166-1 alpha-3",
}
