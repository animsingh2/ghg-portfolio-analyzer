"""
Module 1 — Data Ingestion
=========================
Loads portfolio and emissions data from CSV, validates schema,
normalises types, and returns typed domain objects ready for
the PCAF engine.

Supported inputs
----------------
- CSV upload (local file path)
- pandas DataFrame (for notebook / API use)

Validation covers
-----------------
- Required columns presence
- Asset-class-specific attribution denominator checks
- DQ score range (1–5)
- Emissions non-negativity
- Date parsing
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
# Attribution denominator requirements by asset class
# ---------------------------------------------------------------------------

# Maps asset class → which CSV column must be populated as the PCAF denominator
REQUIRED_DENOMINATOR: dict[AssetClass, str] = {
    AssetClass.LISTED_EQUITY_CORP_BONDS:    "evic_usd",
    AssetClass.BUSINESS_LOANS_UNLISTED_EQUITY: "total_equity_debt_usd",
    AssetClass.PROJECT_FINANCE:             "total_project_value_usd",
    AssetClass.COMMERCIAL_REAL_ESTATE:      "total_project_value_usd",
    AssetClass.MORTGAGES:                   "collateral_value_usd",
    AssetClass.MOTOR_VEHICLE_LOANS:         "collateral_value_usd",
    AssetClass.SOVEREIGN_DEBT:              "government_revenue_usd",
}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ValidationWarning(UserWarning):
    """Non-fatal data quality issue — row is kept but flagged."""


class ValidationError(ValueError):
    """Fatal schema error — ingestion cannot continue."""


def _check_required_columns(df: pd.DataFrame, required: list[str], source: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValidationError(
            f"{source}: missing required columns: {missing}\n"
            f"Expected columns: {list(PORTFOLIO_CSV_COLUMNS.keys())}"
        )


def _parse_asset_class(value: str) -> Optional[AssetClass]:
    try:
        return AssetClass(value.strip().lower())
    except ValueError:
        valid = [e.value for e in AssetClass]
        warnings.warn(
            f"Unknown asset class '{value}'. Valid values: {valid}",
            ValidationWarning,
            stacklevel=3,
        )
        return None


def _parse_dq_score(value) -> Optional[DataQualityScore]:
    if pd.isna(value):
        return None
    try:
        return DataQualityScore(int(value))
    except (ValueError, KeyError):
        warnings.warn(
            f"Invalid DQ score '{value}'. Must be 1–5. Defaulting to 5 (estimated).",
            ValidationWarning,
            stacklevel=3,
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

    Parameters
    ----------
    path : path to the CSV file
    portfolio_id : overrides the portfolio_id column if provided
    portfolio_name : display name for the portfolio

    Returns
    -------
    Portfolio with validated PortfolioHolding objects.
    Rows with fatal errors are skipped and logged.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Portfolio CSV not found: {path}")

    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = df.columns.str.strip().str.lower()

    # Validate required columns
    required = ["holding_id", "entity_name", "asset_class",
                "outstanding_amount_usd", "reporting_date"]
    _check_required_columns(df, required, source=str(path))

    holdings: list[PortfolioHolding] = []
    skipped = 0

    for _, row in df.iterrows():
        holding_id = str(row["holding_id"]).strip()

        # Parse asset class
        asset_class = _parse_asset_class(str(row.get("asset_class", "")))
        if asset_class is None:
            logger.warning("Skipping row %s — invalid asset class.", holding_id)
            skipped += 1
            continue

        # Parse outstanding amount
        outstanding = _float_or_none(row.get("outstanding_amount_usd"))
        if outstanding is None or outstanding <= 0:
            logger.warning("Skipping row %s — invalid outstanding_amount_usd.", holding_id)
            skipped += 1
            continue

        # Parse reporting date
        try:
            rdate = pd.to_datetime(row["reporting_date"]).date()
        except Exception:
            rdate = datetime.date.today()
            warnings.warn(
                f"Could not parse reporting_date for {holding_id}; using today.",
                ValidationWarning,
            )

        # Check asset-class-specific denominator
        denom_col = REQUIRED_DENOMINATOR.get(asset_class)
        denom_val = _float_or_none(row.get(denom_col)) if denom_col else None
        if denom_col and denom_val is None:
            warnings.warn(
                f"Holding {holding_id} ({asset_class.value}): "
                f"attribution denominator '{denom_col}' is missing. "
                f"Attribution factor cannot be computed; emissions will be estimated.",
                ValidationWarning,
            )

        pid = portfolio_id or str(row.get("portfolio_id", "UNKNOWN")).strip()

        holding = PortfolioHolding(
            holding_id=holding_id,
            portfolio_id=pid,
            asset_class=asset_class,
            reporting_date=rdate,
            entity_id=(str(row["isin"]).strip() if "isin" in df.columns and not pd.isna(row.get("isin")) else str(row.get("entity_id", holding_id)).strip()),
            entity_name=str(row["entity_name"]).strip(),
            isin=str(row["isin"]).strip() if "isin" in df.columns and not pd.isna(row.get("isin")) else None,
            lei=str(row["lei"]).strip() if "lei" in df.columns and not pd.isna(row.get("lei")) else None,
            country_iso3=str(row.get("country_iso3", "")).strip() or None,
            outstanding_amount_usd=outstanding,
            evic_usd=_float_or_none(row.get("evic_usd")),
            total_equity_debt_usd=_float_or_none(row.get("total_equity_debt_usd")),
            total_project_value_usd=_float_or_none(row.get("total_project_value_usd")),
            collateral_value_usd=_float_or_none(row.get("collateral_value_usd")),
            government_revenue_usd=_float_or_none(row.get("government_revenue_usd")),
        )
        holdings.append(holding)

    if skipped:
        logger.warning("Ingestion complete: %d rows skipped due to errors.", skipped)

    pid = portfolio_id or (holdings[0].portfolio_id if holdings else "UNKNOWN")
    pname = portfolio_name or f"Portfolio {pid}"

    return Portfolio(
        portfolio_id=pid,
        portfolio_name=pname,
        holdings=holdings,
    )


# ---------------------------------------------------------------------------
# Emissions data ingestion
# ---------------------------------------------------------------------------

def load_emissions_csv(path: Union[str, Path]) -> dict[str, EmissionsRecord]:
    """
    Load emissions data from CSV and return a dict keyed by entity_id.

    Parameters
    ----------
    path : path to the emissions CSV file

    Returns
    -------
    dict mapping entity_id → EmissionsRecord
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Emissions CSV not found: {path}")

    df = pd.read_csv(path, skipinitialspace=True)
    df.columns = df.columns.str.strip().str.lower()

    required = ["entity_id", "reporting_year"]
    _check_required_columns(df, required, source=str(path))

    records: dict[str, EmissionsRecord] = {}

    for _, row in df.iterrows():
        entity_id = str(row["entity_id"]).strip()

        try:
            year = int(row["reporting_year"])
        except (ValueError, TypeError):
            logger.warning("Skipping entity %s — invalid reporting_year.", entity_id)
            continue

        # Parse DQ scores — default to 5 (estimated) if missing
        s1_dq = _parse_dq_score(row.get("scope_1_dq_score")) or DataQualityScore.ESTIMATED
        s2_dq = _parse_dq_score(row.get("scope_2_dq_score")) or DataQualityScore.ESTIMATED
        s3_dq = _parse_dq_score(row.get("scope_3_dq_score")) or DataQualityScore.ESTIMATED

        # Parse estimation methods
        s1_method = _parse_method(row.get("scope_1_method")) or EmissionsEstimationMethod.REGIONAL_PROXY
        s2_method = _parse_method(row.get("scope_2_method")) or EmissionsEstimationMethod.REGIONAL_PROXY
        s3_method = _parse_method(row.get("scope_3_method")) or EmissionsEstimationMethod.REGIONAL_PROXY

        record = EmissionsRecord(
            entity_id=entity_id,
            reporting_year=year,
            scope_1_emissions=_float_or_none(row.get("scope_1_emissions")),
            scope_2_emissions=_float_or_none(row.get("scope_2_emissions")),
            scope_3_upstream_emissions=_float_or_none(row.get("scope_3_upstream")),
            scope_3_downstream_emissions=_float_or_none(row.get("scope_3_downstream")),
            scope_1_dq_score=s1_dq,
            scope_2_dq_score=s2_dq,
            scope_3_dq_score=s3_dq,
            scope_1_method=s1_method,
            scope_2_method=s2_method,
            scope_3_method=s3_method,
            revenue_usd=_float_or_none(row.get("revenue_usd")),
            enterprise_value_incl_cash=_float_or_none(row.get("evic_usd")),
            total_equity_and_debt=_float_or_none(row.get("total_equity_debt_usd")),
            gics_sector=str(row.get("gics_sector", "")).strip() or None,
            gics_industry_group=str(row.get("gics_industry_group", "")).strip() or None,
            nace_code=str(row.get("nace_code", "")).strip() or None,
            country_iso3=str(row.get("country_iso3", "")).strip() or None,
        )
        records[entity_id] = record

    logger.info("Loaded %d emissions records from %s", len(records), path)
    return records


# ---------------------------------------------------------------------------
# DataFrame export (for notebooks / dashboards)
# ---------------------------------------------------------------------------

def portfolio_to_dataframe(portfolio: Portfolio) -> pd.DataFrame:
    """Convert a Portfolio object back to a flat DataFrame for inspection."""
    rows = []
    for h in portfolio.holdings:
        rows.append({
            "holding_id": h.holding_id,
            "entity_name": h.entity_name,
            "asset_class": h.asset_class.value,
            "outstanding_amount_usd": h.outstanding_amount_usd,
            "country_iso3": h.country_iso3,
            "attribution_denominator": h.attribution_denominator,
            "attribution_factor": h.attribution_factor,
            "financed_emissions_tco2e": h.financed_emissions_tco2e,
            "financed_emissions_dq_score": h.financed_emissions_dq_score,
        })
    return pd.DataFrame(rows)


def emissions_to_dataframe(records: dict[str, EmissionsRecord]) -> pd.DataFrame:
    """Convert emissions records to a flat DataFrame for inspection."""
    rows = []
    for eid, r in records.items():
        rows.append({
            "entity_id": eid,
            "reporting_year": r.reporting_year,
            "scope_1_tco2e": r.scope_1_emissions,
            "scope_2_tco2e": r.scope_2_emissions,
            "scope_3_total_tco2e": r.total_scope_3,
            "total_tco2e": r.total_emissions,
            "weighted_dq_score": r.weighted_dq_score,
            "error_margin_pct": r.error_margin_pct,
            "revenue_usd": r.revenue_usd,
            "gics_sector": r.gics_sector,
            "country_iso3": r.country_iso3,
        })
    return pd.DataFrame(rows)
