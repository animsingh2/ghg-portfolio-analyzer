# GHG Portfolio Analyzer

A Python library for computing PCAF-aligned financed emissions across investment portfolios, built to the [PCAF Global GHG Accounting and Reporting Standard, Third Edition (2025)](https://carbonaccountingfinancials.com/standard).

---

## What it does

- Computes financed emissions (Scope 1, 2, 3) using PCAF (2025) attribution factors for all 7 asset classes
- Tracks data quality scores (DQ 1–5) per scope, weighted by outstanding amount per PCAF (2025) Box 6.1-6
- Calculates WACI (Weighted Average Carbon Intensity) and economic intensity metrics
- Applies Scope 3 de-duplication via three methods: none, MSCI fixed (0.205), or portfolio-specific sector overlap
- Stress tests transition risk under three NGFS Phase 4 climate scenarios (Orderly, Disorderly, Hot House World)
- Assesses SBTi 1.5°C pathway alignment with implied portfolio temperature scoring
- Exports results to JSON and formatted Excel (5 sheets)
- Visualises everything in a browser dashboard with per-holding drill-down and AI-generated insights

---

## Project structure

```
ghg-portfolio-analyzer/
├── src/
│   ├── models.py                          # Domain objects — all 7 PCAF asset classes
│   ├── ingestion/loader.py                # CSV ingestion and PCAF denominator validation
│   ├── pcaf_engine/engine.py              # Attribution factors, WACI, intensity metrics
│   ├── deduplication/deduplication.py     # Scope 3 double-counting removal
│   ├── stress_testing/stress_testing.py   # NGFS carbon price stress test
│   ├── stress_testing/pathway_alignment.py # SBTi 1.5°C alignment assessment
│   └── reporting/report.py               # JSON and Excel report builder
├── data/sample/
│   ├── sample_portfolio.csv              # 18-holding example across all 7 asset classes
│   └── sample_emissions.csv             # Matching emissions records with mixed DQ scores
├── reporting/dashboard.html             # Browser dashboard — open directly, no server needed
├── run_test.py                          # End-to-end run script
└── requirements.txt
```

---

## Quick start

```bash
pip install -r requirements.txt
python run_test.py
```

Open `reporting/dashboard.html` in any browser and click **Use sample data** for the interactive dashboard.

---

## PCAF (2025) attribution denominators

The correct denominator varies by asset class per PCAF (2025). The loader validates this and warns when required fields are missing.

| Asset class | Denominator | CSV column |
|---|---|---|
| Listed equity & corporate bonds | EVIC (listed) or total equity+debt (private bonds) | `evic_usd` / `total_equity_debt_usd` |
| Business loans & unlisted equity | Total equity+debt (private) or EVIC (listed borrower) | `total_equity_debt_usd` / `borrower_is_listed` |
| Project finance — SPV | Total project equity+debt | `project_equity_debt_usd` |
| Project finance — no balance sheet | Total project value at origination, frozen | `project_value_at_origination_usd` |
| Commercial real estate | Property value at origination, frozen | `property_value_at_origination_usd` |
| Mortgages | Property value at origination, frozen | `mortgage_property_value_at_origination_usd` |
| Motor vehicle loans | Vehicle value at origination; 100% attribution if unknown | `vehicle_value_at_origination_usd` |
| Sovereign debt | PPP-adjusted GDP (IMF WEO) | `ppp_adjusted_gdp_usd` |

---

## Known limitations

**Sovereign debt pathway alignment**: No SBTi sector pathway exists for national governments. Sovereign holdings show as "no data" in the alignment assessment. The stress test uses government revenue as the denominator for carbon cost as % of revenue, which produces near-zero figures not comparable to corporate holdings.

**Temperature scoring in small portfolios**: The winsorised and no-cap temperature scores are often identical in portfolios with few holdings, because the 95th percentile of the overshoot distribution lands on the same outlier the no-cap score includes. The scores diverge in larger, more diversified portfolios.

**Pathway alignment scope**: The alignment module follows SBTi Corporate Net-Zero Standard (2021) and GFANZ guidance, which build on PCAF accounting but use their own methodologies. Sector benchmarks are calibrated to 2020 base levels and should be refreshed when SBTi publishes updated pathways.

**Stress test scope**: The stress test models transition risk (carbon pricing) only, at a single horizon year. Physical risk, time-path analysis, and macro-financial transmission are yet to be implemented.

---

## References

- PCAF Global GHG Accounting and Reporting Standard, Third Edition (2025)
- GHG Protocol Corporate Value Chain (Scope 3) Standard
- GFANZ Transition Finance Metrics Framework (2023)
- SBTi Corporate Net-Zero Standard v1.1 (2021)
- IEA Net Zero by 2050 (2021)
- NGFS Phase 4 Climate Scenarios (2023)
- IMF World Economic Outlook Database (PPP-adjusted GDP)
