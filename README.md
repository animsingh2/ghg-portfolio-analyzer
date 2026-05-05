# GHG Portfolio Analyzer

A Python library for calculating financed emissions across investment portfolios, built to the [PCAF Global GHG Accounting and Reporting Standard (2022)](https://carbonaccountingfinancials.com/standard).

## What it does

- Computes **financed emissions** (Scope 1, 2, 3) using PCAF attribution factors
- Supports all **7 PCAF asset classes** (listed equity, corporate bonds, business loans, project finance, commercial real estate, mortgages, sovereign debt)
- Tracks **data quality scores** (DQ 1–5) per scope with uncertainty bounds
- Calculates intensity metrics including **WACI** (Weighted Average Carbon Intensity)

## Project structure

```
├── models.py                # Core domain objects (EmissionsRecord, PortfolioHolding, etc.)
├── loader.py                # CSV ingestion and validation
├── sample_portfolio.csv     # Example portfolio input
└── sample_emissions.csv     # Example emissions input
```
## Quickstart

```python
from loader import load_portfolio_csv, load_emissions_csv

portfolio = load_portfolio_csv("sample_portfolio.csv")
emissions = load_emissions_csv("sample_emissions.csv")
```

## References

- PCAF Global GHG Accounting and Reporting Standard (2022)
- GHG Protocol Corporate Value Chain (Scope 3) Standard
- GFANZ Transition Finance Metrics Framework (2023)
