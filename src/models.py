"""
GHG Portfolio Analyzer — Core Data Models
==========================================
Implements PCAF-aligned schemas for all 7 asset classes.

References:
  - PCAF Global GHG Accounting and Reporting Standard for the Financial
    Industry, Third Edition (2025)
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
    LISTED_EQUITY_CORP_BONDS      = "listed_equity_and_corporate_bonds"
    BUSINESS_LOANS_UNLISTED_EQUITY = "business_loans_and_unlisted_equity"
    PROJECT_FINANCE               = "project_finance"
    COMMERCIAL_REAL_ESTATE        = "commercial_real_estate"
    MORTGAGES                     = "mortgages"
    MOTOR_VEHICLE_LOANS           = "motor_vehicle_loans"
    SOVEREIGN_DEBT                = "sovereign_debt"


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
    VERIFIED_REPORTED  = 1
    REPORTED_UNAUDITED = 2
    SECTOR_AVERAGE     = 3
    REGIONAL_PROXY     = 4
    ESTIMATED          = 5


class EmissionsEstimationMethod(str, Enum):
    """Fallback estimation hierarchy when reported data is unavailable."""
    REPORTED_VERIFIED  = "reported_verified"       # DQ Score 1
    REPORTED_UNAUDITED = "reported_unaudited"       # DQ Score 2
    EEIO_SPEND_BASED   = "eeio_spend_based"         # DQ Score 3-4
    SECTOR_INTENSITY   = "sector_intensity_revenue" # DQ Score 4
    REGIONAL_PROXY     = "regional_proxy"           # DQ Score 5


class NGFSScenario(str, Enum):
    """NGFS climate scenarios for stress testing."""
    ORDERLY    = "net_zero_2050_orderly"            # Early, predictable policy action
    DISORDERLY = "delayed_transition_disorderly"    # Late, abrupt policy action
    HOT_HOUSE  = "current_policies_hot_house"       # No new climate policies


class EmissionsUnit(str, Enum):
    TCO2E  = "tCO2e"
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
    consistent with PCAF (2025) Figure 10.7.
    """
    entity_id: str       # Internal unique ID (ISIN, loan ID, etc.)
    reporting_year: int

    # --- Absolute emissions (tCO2e) ---
    scope_1_emissions: Optional[float] = None
    scope_2_emissions: Optional[float] = None
    scope_3_upstream_emissions: Optional[float] = None
    scope_3_downstream_emissions: Optional[float] = None

    # --- PCAF Data Quality Scores (1-5) per scope ---
    scope_1_dq_score: DataQualityScore = DataQualityScore.ESTIMATED
    scope_2_dq_score: DataQualityScore = DataQualityScore.ESTIMATED
    scope_3_dq_score: DataQualityScore = DataQualityScore.ESTIMATED

    # --- Estimation methods used (for audit trail) ---
    scope_1_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY
    scope_2_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY
    scope_3_method: EmissionsEstimationMethod = EmissionsEstimationMethod.REGIONAL_PROXY

    # --- Financial context (for intensity metrics) ---
    revenue_usd: Optional[float] = None                 # For WACI calculation
    enterprise_value_incl_cash: Optional[float] = None  # EVIC
    total_equity_and_debt: Optional[float] = None       # Balance-sheet denominator

    # --- Sector / region (for proxy estimation fallback) ---
    gics_sector: Optional[str] = None
    gics_industry_group: Optional[str] = None
    nace_code: Optional[str] = None
    country_iso3: Optional[str] = None

    # --- Scope 3 de-duplication ---
    scope_3_dedup_applied: bool = False
    scope_3_dedup_multiplier: Optional[float] = None  # e.g. 0.205 (MSCI methodology)

    @property
    def total_scope_3(self) -> Optional[float]:
        upstream   = self.scope_3_upstream_emissions or 0.0
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
            (self.total_scope_3,     self.scope_3_dq_score),
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
        lower = int(score)
        upper = min(lower + 1, 5)
        frac  = score - lower
        return margins[lower] * (1 - frac) + margins[upper] * frac


# ---------------------------------------------------------------------------
# Portfolio holding — one row per position
# ---------------------------------------------------------------------------

@dataclass
class PortfolioHolding:
    """
    A single financial institution exposure to a borrower or investee.

    PCAF attribution factor = outstanding amount / PCAF denominator
    Financed emissions      = attribution factor x entity total emissions

    The denominator is asset-class specific per PCAF (2025) §5:

    Asset class                       Denominator
    ─────────────────────────────── ─────────────────────────────────────────
    Listed equity / corp bonds        EVIC (listed) or total equity+debt
                                      (bonds to private companies)
    Business loans / unlisted equity  Total equity+debt (private borrower) or
                                      EVIC (listed borrower)
    Project finance                   Total project equity+debt (SPV with own
                                      balance sheet) OR total project value at
                                      origination (no separate balance sheet)
    Commercial real estate            Property value at origination (frozen)
    Mortgages                         Property value at origination (frozen)
    Motor vehicle loans               Vehicle value at origination (frozen);
                                      100% attribution if unknown
    Sovereign debt                    PPP-adjusted GDP (IMF WEO)
    """

    holding_id:    str
    portfolio_id:  str
    asset_class:   AssetClass
    reporting_date: datetime.date

    # --- Counterparty identification ---
    entity_id:    str                   # Links to EmissionsRecord.entity_id
    entity_name:  str
    isin:         Optional[str] = None
    lei:          Optional[str] = None  # Legal Entity Identifier
    country_iso3: Optional[str] = None

    # --- Exposure ---
    outstanding_amount_usd: float = 0.0

    # ── Listed equity & corporate bonds ──────────────────────────────────
    # PCAF (2025) §5.1
    # Listed companies:         AF = outstanding / evic_usd
    # Bonds to private cos:     AF = outstanding / total_equity_debt_usd
    evic_usd:              Optional[float] = None  # Enterprise Value incl. Cash
    total_equity_debt_usd: Optional[float] = None  # Total equity + debt (balance sheet)

    # ── Business loans & unlisted equity ─────────────────────────────────
    # PCAF (2025) §5.2
    # Private borrower:  AF = outstanding / total_equity_debt_usd  (same field above)
    # Listed borrower:   AF = outstanding / evic_usd               (same field above)
    # Flag to indicate whether the borrower is a listed company:
    borrower_is_listed: bool = False

    # ── Project finance ───────────────────────────────────────────────────
    # PCAF (2025) §5.3
    # Primary (SPV with own balance sheet):
    #   AF = outstanding / project_equity_debt_usd
    # Fallback (no separate balance sheet, e.g. LED retrofit loan):
    #   AF = outstanding / project_value_at_origination_usd  (frozen at origination)
    project_has_balance_sheet:          bool            = True
    project_equity_debt_usd:            Optional[float] = None  # Primary denominator
    project_value_at_origination_usd:   Optional[float] = None  # Fallback denominator

    # ── Commercial real estate ────────────────────────────────────────────
    # PCAF (2025) §5.4
    # AF = outstanding / property_value_at_origination_usd  (frozen at origination)
    # If origination value unavailable, use latest known value and freeze it.
    property_value_at_origination_usd: Optional[float] = None

    # ── Mortgages ─────────────────────────────────────────────────────────
    # PCAF (2025) §5.5  — identical structure to CRE
    # AF = outstanding / mortgage_property_value_at_origination_usd
    mortgage_property_value_at_origination_usd: Optional[float] = None

    # ── Motor vehicle loans ───────────────────────────────────────────────
    # PCAF (2025) §5.6
    # AF = outstanding / vehicle_value_at_origination_usd
    # If unknown, PCAF requires assuming 100% attribution (conservative fallback).
    vehicle_value_at_origination_usd: Optional[float] = None

    # ── Sovereign debt ────────────────────────────────────────────────────
    # PCAF (2025) §5.9
    # AF = outstanding / ppp_adjusted_gdp_usd
    # Source: IMF World Economic Outlook, "GDP based on PPP" series,
    #         current international dollars for the reporting year.
    # Note: government_revenue_usd is retained for stress testing only
    #       (carbon cost as % of government revenue) — NOT used for attribution.
    ppp_adjusted_gdp_usd:   Optional[float] = None  # Attribution denominator
    government_revenue_usd: Optional[float] = None  # Stress test only

    # --- Computed by engine (set after processing) ---
    attribution_factor:          Optional[float] = None
    financed_emissions_tco2e:    Optional[float] = None
    financed_emissions_dq_score: Optional[float] = None

    @property
    def attribution_denominator(self) -> Optional[float]:
        """
        Returns the correct PCAF (2025) denominator for this asset class.

        Implements the full decision tree from PCAF (2025) §5, including
        the two-path project finance logic and the PPP-GDP sovereign rule.
        Returns None when the required denominator field is missing —
        the engine will warn and skip attribution for that holding.
        """
        ac = self.asset_class

        if ac == AssetClass.LISTED_EQUITY_CORP_BONDS:
            # §5.1: listed companies → EVIC; bonds to private cos → equity+debt
            return self.evic_usd

        if ac == AssetClass.BUSINESS_LOANS_UNLISTED_EQUITY:
            # §5.2: listed borrower → EVIC; private borrower → equity+debt
            if self.borrower_is_listed:
                return self.evic_usd
            return self.total_equity_debt_usd

        if ac == AssetClass.PROJECT_FINANCE:
            # §5.3: SPV with balance sheet → project equity+debt (primary)
            #        no balance sheet      → project value at origination (fallback)
            if self.project_has_balance_sheet:
                return self.project_equity_debt_usd
            return self.project_value_at_origination_usd

        if ac == AssetClass.COMMERCIAL_REAL_ESTATE:
            # §5.4: property value frozen at origination
            return self.property_value_at_origination_usd

        if ac == AssetClass.MORTGAGES:
            # §5.5: property value frozen at origination
            return self.mortgage_property_value_at_origination_usd

        if ac == AssetClass.MOTOR_VEHICLE_LOANS:
            # §5.6: vehicle value at origination
            # If None, engine applies 100% attribution (PCAF conservative fallback)
            return self.vehicle_value_at_origination_usd

        if ac == AssetClass.SOVEREIGN_DEBT:
            # §5.9: PPP-adjusted GDP (IMF WEO)
            return self.ppp_adjusted_gdp_usd

        return None


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
    portfolio_id:   str
    portfolio_name: str
    base_currency:  str = "USD"
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
    Implements all four metric types from PCAF (2025) / GFANZ Table 10.5.
    """
    portfolio_id:  str
    reporting_year: int
    computed_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)

    # --- Absolute emissions (tCO2e) ---
    total_financed_emissions_tco2e: float = 0.0
    scope_1_financed_tco2e:         float = 0.0
    scope_2_financed_tco2e:         float = 0.0
    scope_3_financed_tco2e:         float = 0.0
    scope_3_financed_tco2e_dedup:   float = 0.0  # After de-duplication

    # --- Intensity metrics ---
    waci_tco2e_per_mrevenue:        Optional[float] = None  # tCO2e / $M revenue
    economic_intensity_tco2e_per_musd: Optional[float] = None  # tCO2e / $M AUM

    # --- Data quality ---
    portfolio_weighted_dq_score:       Optional[float] = None
    pct_holdings_with_reported_data:   float = 0.0  # DQ score 1 or 2
    pct_holdings_estimated:            float = 0.0  # DQ score 4 or 5

    # --- Coverage ---
    n_holdings_total:        int   = 0
    n_holdings_with_emissions: int = 0
    aum_coverage_pct:        float = 0.0

    # --- Per asset class breakdown ---
    by_asset_class: dict[str, dict] = field(default_factory=dict)

    # --- Scope 3 de-duplication metadata ---
    dedup_multiplier_applied: Optional[float] = None
    scope_3_raw_tco2e:        Optional[float] = None


# ---------------------------------------------------------------------------
# Stress test inputs / outputs
# ---------------------------------------------------------------------------

@dataclass
class CarbonPriceScenario:
    """NGFS carbon price path for transition risk stress testing."""
    scenario:      NGFSScenario
    scenario_label: str
    price_path:    dict[int, float]  # {year: USD/tCO2e}
    source: str = "NGFS Phase 4 (2023)"


@dataclass
class StressTestResult:
    """Output of running a carbon price scenario against a portfolio."""
    portfolio_id: str
    scenario:     NGFSScenario
    horizon_year: int

    total_carbon_cost_usd:          float          = 0.0
    pct_ebitda_at_risk:             Optional[float] = None
    high_risk_holdings:             list[str]       = field(default_factory=list)
    pct_portfolio_paris_aligned:    Optional[float] = None
    pct_portfolio_misaligned:       Optional[float] = None
    aggregate_alignment_gap_tco2e:  Optional[float] = None


# ---------------------------------------------------------------------------
# CSV column specifications
# ---------------------------------------------------------------------------

PORTFOLIO_CSV_COLUMNS = {
    # ── Required for all asset classes ──────────────────────────────────
    "holding_id":             "str   — unique ID for this position",
    "portfolio_id":           "str   — portfolio this holding belongs to",
    "entity_name":            "str   — company, asset, or sovereign name",
    "asset_class":            "str   — one of the 7 PCAF asset class enum values",
    "outstanding_amount_usd": "float — outstanding loan balance or market value (USD)",
    "reporting_date":         "date  — YYYY-MM-DD",

    # ── Counterparty identifiers ─────────────────────────────────────────
    "entity_id":   "str   — links to emissions CSV entity_id (required)",
    "isin":        "str   — ISIN (optional)",
    "lei":         "str   — Legal Entity Identifier (optional)",
    "country_iso3":"str   — ISO 3166-1 alpha-3 country code",

    # ── Listed equity & corporate bonds (§5.1) ───────────────────────────
    "evic_usd":              "float — EVIC: mkt cap ordinary + preferred + book debt + minorities (no cash deduction). Listed cos only.",
    "total_equity_debt_usd": "float — Total equity + debt from balance sheet. Bonds to private cos / private business loans.",

    # ── Business loans & unlisted equity (§5.2) ──────────────────────────
    # uses evic_usd (if borrower_is_listed=true) or total_equity_debt_usd
    "borrower_is_listed": "bool  — true if business loan borrower is a listed company (uses EVIC). Default false.",

    # ── Project finance (§5.3) ────────────────────────────────────────────
    "project_has_balance_sheet":        "bool  — true for SPV/separate legal entity (primary formula). false for embedded projects (fallback). Default true.",
    "project_equity_debt_usd":          "float — Total project equity + debt (PRIMARY: use when project_has_balance_sheet=true)",
    "project_value_at_origination_usd": "float — Total project value at origination, frozen (FALLBACK: use when project_has_balance_sheet=false)",

    # ── Commercial real estate (§5.4) ─────────────────────────────────────
    "property_value_at_origination_usd": "float — Property value at loan/equity origination, frozen. Use latest known value if origination unavailable.",

    # ── Mortgages (§5.5) ──────────────────────────────────────────────────
    "mortgage_property_value_at_origination_usd": "float — Residential property value at origination, frozen.",

    # ── Motor vehicle loans (§5.6) ────────────────────────────────────────
    "vehicle_value_at_origination_usd": "float — Vehicle purchase price at origination. Leave blank to trigger 100% attribution fallback.",

    # ── Sovereign debt (§5.9) ─────────────────────────────────────────────
    "ppp_adjusted_gdp_usd":   "float — PPP-adjusted GDP in current international USD (IMF WEO 'PPPGDP' series). ATTRIBUTION denominator.",
    "government_revenue_usd": "float — Government tax receipts (USD). STRESS TEST only — NOT used for attribution.",
}

EMISSIONS_CSV_COLUMNS = {
    "entity_id":           "str   — links to portfolio holding entity_id",
    "reporting_year":      "int   — fiscal year of emissions data",
    "scope_1_emissions":   "float — tCO2e Scope 1 (direct, owned sources)",
    "scope_2_emissions":   "float — tCO2e Scope 2 (purchased energy)",
    "scope_3_upstream":    "float — tCO2e Scope 3 upstream (value chain)",
    "scope_3_downstream":  "float — tCO2e Scope 3 downstream (use of products sold)",
    "scope_1_dq_score":    "int   — PCAF DQ score for Scope 1 (1-5)",
    "scope_2_dq_score":    "int   — PCAF DQ score for Scope 2 (1-5)",
    "scope_3_dq_score":    "int   — PCAF DQ score for Scope 3 (1-5)",
    "scope_1_method":      "str   — estimation method for Scope 1",
    "scope_2_method":      "str   — estimation method for Scope 2",
    "scope_3_method":      "str   — estimation method for Scope 3",
    "revenue_usd":         "float — annual revenue USD (for WACI)",
    "evic_usd":            "float — EVIC USD (can be supplied here instead of portfolio CSV)",
    "total_equity_debt_usd": "float — total equity + debt USD",
    "gics_sector":         "str   — GICS sector name",
    "gics_industry_group": "str   — GICS industry group",
    "nace_code":           "str   — NACE Rev. 2 code",
    "country_iso3":        "str   — ISO 3166-1 alpha-3",
}
