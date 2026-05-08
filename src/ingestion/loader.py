"""
GHG Portfolio Analyzer — Data Ingestion
========================================
Loads portfolio and emissions data from CSV, validates schema,
normalises types, and returns typed domain objects ready for
the PCAF engine.

PCAF (2025) attribution denominator decision tree is enforced here:
each asset class is validated against its required denominator field(s)
and a clear warning is emitted when the required field is absent.
"""

from __future__ import annotations

import datetime
import logging
import warnings
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from src.models import (
    AssetClass,
    DataQualityScore,
    EmissionsEstimationMethod,
    EmissionsRecord,
    Portfolio,
    PortfolioHolding,
    PORTFOLIO_CSV_COLUMNS,
    EMISSIONS_CSV_COLUMNS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PCAF (2025) denominator requirements by asset class
# Maps asset class → human-readable description of what is required
# ---------------------------------------------------------------------------

DENOMINATOR_GUIDANCE: dict[AssetClass, str] = {
    AssetClass.LISTED_EQUITY_CORP_BONDS:
        "evic_usd (listed companies) OR total_equity_debt_usd (bonds to private companies)",
    AssetClass.BUSINESS_LOANS_UNLISTED_EQUITY:
        "total_equity_debt_usd (private borrower) OR evic_usd (listed borrower, set borrower_is_listed=true)",
    AssetClass.PROJECT_FINANCE:
        "project_equity_debt_usd (project_has_balance_sheet=true) OR "
        "project_value_at_origination_usd (project_has_balance_sheet=false)",
    AssetClass.COMMERCIAL_REAL_ESTATE:
        "property_value_at_origination_usd (frozen at origination)",
    AssetClass.MORTGAGES:
        "mortgage_property_value_at_origination_usd (frozen at origination)",
    AssetClass.MOTOR_VEHICLE_LOANS:
        "vehicle_value_at_origination_usd (frozen at origination; "
        "omit to apply 100% attribution fallback per PCAF §5.6)",
    AssetClass.SOVEREIGN_DEBT:
        "ppp_adjusted_gdp_usd (IMF WEO PPP-adjusted GDP, NOT government revenue)",
}


# ---------------------------------------------------------------------------
# Custom warning / error types
# ---------------------------------------------------------------------------

class ValidationWarning(UserWarning):
    """Non-fatal data quality issue — row is kept but flagged."""


class ValidationError(ValueError):
    """Fatal schema error — ingestion cannot continue."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_required_columns(
    df: pd.DataFrame, required: list[str], source: str
) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValidationError(
            f"{source}: missing required columns: {missing}"
        )


def _parse_asset_class(value: str) -> Optional[AssetClass]:
    try:
        return AssetClass(value.strip().lower())
    except ValueError:
        valid = [e.value for e in AssetClass]
        warnings.warn(
            f"Unknown asset class '{value}'. Valid values: {valid}",
            ValidationWarning, stacklevel=3,
        )
        return None


def _parse_dq_score(value) -> Optional[DataQualityScore]:
    if pd.isna(value):
        return None
    try:
        return DataQualityScore(int(value))
    except (ValueError, KeyError):
        warnings.warn(
            f"Invalid DQ score '{value}'. Must be 1-5. Defaulting to 5.",
            ValidationWarning, stacklevel=3,
        )
        return DataQualityScore.ESTIMATED


def _parse_method(value) -> Optional[EmissionsEstimationMethod]:
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        return EmissionsEstimationMethod(str(value).strip())
    except ValueError:
        return None


def _float_or_none(value) -> Optional[float]:
    if pd.isna(value):
        return None
    try:
        v = float(value)
        return v if v >= 0 else None
    except (ValueError, TypeError):
        return None


def _bool_field(row: pd.Series, col: str, default: bool) -> bool:
    """Parse a boolean column that may be True/False/true/false/1/0 or absent."""
    val = row.get(col)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return default


def _resolve_entity_id(row: pd.Series, holding_id: str) -> str:
    """
    Resolve entity ID for linking to emissions records.

    Priority:
      1. entity_id column (explicit — works for all asset classes)
      2. isin column (publicly listed securities)
      3. holding_id fallback (last resort)
    """
    for col in ("entity_id", "isin"):
        val = row.get(col)
        if val is not None and not pd.isna(val):
            return str(val).strip()
    return holding_id


# ---------------------------------------------------------------------------
# Portfolio ingestion
# ---------------------------------------------------------------------------

def load_portfolio_csv(
    path: Union[str, Path],
    portfolio_id: Optional[str] = None,
    portfolio_name: Optional[str] = None,
) -> Portfolio:
    """
    Load a portfolio from CSV and return a typed Portfolio object.

    The loader enforces PCAF (2025) §5 denominator requirements per
    asset class and emits clear warnings when required fields are absent.

    Motor vehicle loans with no vehicle_value_at_origination_usd will
    have attribution_factor set to 1.0 (100%) by the engine, per PCAF
    §5.6 conservative fallback.

    Parameters
    ----------
    path : Path to the portfolio CSV file.
    portfolio_id : Overrides the portfolio_id column when provided.
    portfolio_name : Display name for the portfolio.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Portfolio CSV not found: {path}")

    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = df.columns.str.strip().str.lower()

    required = [
        "holding_id", "entity_name", "asset_class",
        "outstanding_amount_usd", "reporting_date",
    ]
    _check_required_columns(df, required, source=str(path))

    holdings: list[PortfolioHolding] = []
    skipped = 0

    for _, row in df.iterrows():
        holding_id = str(row["holding_id"]).strip()

        asset_class = _parse_asset_class(str(row.get("asset_class", "")))
        if asset_class is None:
            logger.warning("Skipping %s — invalid asset class.", holding_id)
            skipped += 1
            continue

        outstanding = _float_or_none(row.get("outstanding_amount_usd"))
        if outstanding is None or outstanding <= 0:
            logger.warning("Skipping %s — invalid outstanding_amount_usd.", holding_id)
            skipped += 1
            continue

        try:
            rdate = pd.to_datetime(row["reporting_date"]).date()
        except Exception:
            rdate = datetime.date.today()
            warnings.warn(
                f"Could not parse reporting_date for {holding_id}; using today.",
                ValidationWarning,
            )

        pid = portfolio_id or str(row.get("portfolio_id", "UNKNOWN")).strip()

        # ── Parse all denominator fields ──────────────────────────────────
        evic               = _float_or_none(row.get("evic_usd"))
        total_eq_debt      = _float_or_none(row.get("total_equity_debt_usd"))
        borrower_listed    = _bool_field(row, "borrower_is_listed", default=False)
        proj_has_bs        = _bool_field(row, "project_has_balance_sheet", default=True)
        proj_eq_debt       = _float_or_none(row.get("project_equity_debt_usd"))
        proj_val_orig      = _float_or_none(row.get("project_value_at_origination_usd"))
        prop_val_orig      = _float_or_none(row.get("property_value_at_origination_usd"))
        mort_prop_val_orig = _float_or_none(row.get("mortgage_property_value_at_origination_usd"))
        veh_val_orig       = _float_or_none(row.get("vehicle_value_at_origination_usd"))
        ppp_gdp            = _float_or_none(row.get("ppp_adjusted_gdp_usd"))
        govt_rev           = _float_or_none(row.get("government_revenue_usd"))

        # ── Validate denominator presence and warn if missing ────────────
        _validate_denominator(
            holding_id, asset_class, borrower_listed, proj_has_bs,
            evic, total_eq_debt, proj_eq_debt, proj_val_orig,
            prop_val_orig, mort_prop_val_orig, veh_val_orig, ppp_gdp,
        )

        isin = None
        if "isin" in df.columns and not pd.isna(row.get("isin")):
            isin = str(row["isin"]).strip()

        lei = None
        if "lei" in df.columns and not pd.isna(row.get("lei")):
            lei = str(row["lei"]).strip()

        holding = PortfolioHolding(
            holding_id=holding_id,
            portfolio_id=pid,
            asset_class=asset_class,
            reporting_date=rdate,
            entity_id=_resolve_entity_id(row, holding_id),
            entity_name=str(row["entity_name"]).strip(),
            isin=isin,
            lei=lei,
            country_iso3=str(row.get("country_iso3", "")).strip() or None,
            outstanding_amount_usd=outstanding,
            evic_usd=evic,
            total_equity_debt_usd=total_eq_debt,
            borrower_is_listed=borrower_listed,
            project_has_balance_sheet=proj_has_bs,
            project_equity_debt_usd=proj_eq_debt,
            project_value_at_origination_usd=proj_val_orig,
            property_value_at_origination_usd=prop_val_orig,
            mortgage_property_value_at_origination_usd=mort_prop_val_orig,
            vehicle_value_at_origination_usd=veh_val_orig,
            ppp_adjusted_gdp_usd=ppp_gdp,
            government_revenue_usd=govt_rev,
        )
        holdings.append(holding)

    if skipped:
        logger.warning("Ingestion: %d row(s) skipped.", skipped)

    pid = portfolio_id or (holdings[0].portfolio_id if holdings else "UNKNOWN")
    return Portfolio(
        portfolio_id=pid,
        portfolio_name=portfolio_name or f"Portfolio {pid}",
        holdings=holdings,
    )


def _validate_denominator(
    holding_id: str,
    asset_class: AssetClass,
    borrower_is_listed: bool,
    project_has_balance_sheet: bool,
    evic: Optional[float],
    total_eq_debt: Optional[float],
    proj_eq_debt: Optional[float],
    proj_val_orig: Optional[float],
    prop_val_orig: Optional[float],
    mort_prop_val_orig: Optional[float],
    veh_val_orig: Optional[float],
    ppp_gdp: Optional[float],
) -> None:
    """Emit a warning when the PCAF-required denominator field is absent."""
    ac = asset_class
    missing: Optional[str] = None

    if ac == AssetClass.LISTED_EQUITY_CORP_BONDS:
        if evic is None:
            missing = "evic_usd"

    elif ac == AssetClass.BUSINESS_LOANS_UNLISTED_EQUITY:
        if borrower_is_listed and evic is None:
            missing = "evic_usd (borrower_is_listed=true requires evic_usd)"
        elif not borrower_is_listed and total_eq_debt is None:
            missing = "total_equity_debt_usd"

    elif ac == AssetClass.PROJECT_FINANCE:
        if project_has_balance_sheet and proj_eq_debt is None:
            missing = "project_equity_debt_usd (project_has_balance_sheet=true)"
        elif not project_has_balance_sheet and proj_val_orig is None:
            missing = "project_value_at_origination_usd (project_has_balance_sheet=false)"

    elif ac == AssetClass.COMMERCIAL_REAL_ESTATE:
        if prop_val_orig is None:
            missing = "property_value_at_origination_usd"

    elif ac == AssetClass.MORTGAGES:
        if mort_prop_val_orig is None:
            missing = "mortgage_property_value_at_origination_usd"

    elif ac == AssetClass.MOTOR_VEHICLE_LOANS:
        if veh_val_orig is None:
            warnings.warn(
                f"Holding {holding_id} (motor_vehicle_loans): "
                "vehicle_value_at_origination_usd is missing. "
                "PCAF §5.6 requires 100% attribution as conservative fallback.",
                ValidationWarning,
            )
            return

    elif ac == AssetClass.SOVEREIGN_DEBT:
        if ppp_gdp is None:
            missing = (
                "ppp_adjusted_gdp_usd (IMF WEO PPP-adjusted GDP). "
                "Note: government_revenue_usd is for stress testing only, "
                "NOT used for attribution per PCAF (2025) §5.9."
            )

    if missing:
        warnings.warn(
            f"Holding {holding_id} ({ac.value}): "
            f"attribution denominator missing — {missing}. "
            f"Attribution factor cannot be computed. "
            f"Required: {DENOMINATOR_GUIDANCE[ac]}",
            ValidationWarning,
        )


# ---------------------------------------------------------------------------
# Emissions data ingestion
# ---------------------------------------------------------------------------

def load_emissions_csv(path: Union[str, Path]) -> dict[str, EmissionsRecord]:
    """
    Load emissions data from CSV and return a dict keyed by entity_id.

    Duplicate entity_ids: last row wins (with a warning).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Emissions CSV not found: {path}")

    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = df.columns.str.strip().str.lower()

    _check_required_columns(df, ["entity_id", "reporting_year"], source=str(path))

    records: dict[str, EmissionsRecord] = {}

    for _, row in df.iterrows():
        entity_id = str(row["entity_id"]).strip()

        try:
            year = int(row["reporting_year"])
        except (ValueError, TypeError):
            logger.warning("Skipping entity %s — invalid reporting_year.", entity_id)
            continue

        if entity_id in records:
            warnings.warn(
                f"Duplicate entity_id '{entity_id}' in emissions CSV; last row wins.",
                ValidationWarning,
            )

        s1_dq = _parse_dq_score(row.get("scope_1_dq_score")) or DataQualityScore.ESTIMATED
        s2_dq = _parse_dq_score(row.get("scope_2_dq_score")) or DataQualityScore.ESTIMATED
        s3_dq = _parse_dq_score(row.get("scope_3_dq_score")) or DataQualityScore.ESTIMATED

        s1_m = _parse_method(row.get("scope_1_method")) or EmissionsEstimationMethod.REGIONAL_PROXY
        s2_m = _parse_method(row.get("scope_2_method")) or EmissionsEstimationMethod.REGIONAL_PROXY
        s3_m = _parse_method(row.get("scope_3_method")) or EmissionsEstimationMethod.REGIONAL_PROXY

        records[entity_id] = EmissionsRecord(
            entity_id=entity_id,
            reporting_year=year,
            scope_1_emissions=_float_or_none(row.get("scope_1_emissions")),
            scope_2_emissions=_float_or_none(row.get("scope_2_emissions")),
            scope_3_upstream_emissions=_float_or_none(row.get("scope_3_upstream")),
            scope_3_downstream_emissions=_float_or_none(row.get("scope_3_downstream")),
            scope_1_dq_score=s1_dq,
            scope_2_dq_score=s2_dq,
            scope_3_dq_score=s3_dq,
            scope_1_method=s1_m,
            scope_2_method=s2_m,
            scope_3_method=s3_m,
            revenue_usd=_float_or_none(row.get("revenue_usd")),
            enterprise_value_incl_cash=_float_or_none(row.get("evic_usd")),
            total_equity_and_debt=_float_or_none(row.get("total_equity_debt_usd")),
            gics_sector=str(row.get("gics_sector", "")).strip() or None,
            gics_industry_group=str(row.get("gics_industry_group", "")).strip() or None,
            nace_code=str(row.get("nace_code", "")).strip() or None,
            country_iso3=str(row.get("country_iso3", "")).strip() or None,
        )

    logger.info("Loaded %d emissions records from %s", len(records), path)
    return records


# ---------------------------------------------------------------------------
# DataFrame export helpers
# ---------------------------------------------------------------------------

def portfolio_to_dataframe(portfolio: Portfolio) -> pd.DataFrame:
    """Convert a Portfolio to a flat DataFrame for inspection or export."""
    return pd.DataFrame([
        {
            "holding_id":               h.holding_id,
            "entity_name":              h.entity_name,
            "asset_class":              h.asset_class.value,
            "outstanding_amount_usd":   h.outstanding_amount_usd,
            "country_iso3":             h.country_iso3,
            "attribution_denominator":  h.attribution_denominator,
            "attribution_factor":       h.attribution_factor,
            "financed_emissions_tco2e": h.financed_emissions_tco2e,
            "financed_emissions_dq_score": h.financed_emissions_dq_score,
        }
        for h in portfolio.holdings
    ])


def emissions_to_dataframe(records: dict[str, EmissionsRecord]) -> pd.DataFrame:
    """Convert emissions records to a flat DataFrame for inspection or export."""
    return pd.DataFrame([
        {
            "entity_id":        eid,
            "reporting_year":   r.reporting_year,
            "scope_1_tco2e":    r.scope_1_emissions,
            "scope_2_tco2e":    r.scope_2_emissions,
            "scope_3_total_tco2e": r.total_scope_3,
            "total_tco2e":      r.total_emissions,
            "weighted_dq_score": r.weighted_dq_score,
            "revenue_usd":      r.revenue_usd,
            "gics_sector":      r.gics_sector,
            "country_iso3":     r.country_iso3,
        }
        for eid, r in records.items()
    ])
