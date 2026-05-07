# GHG Portfolio Analyzer

A Python library for computing **PCAF-aligned financed emissions** across all seven asset classes, fully aligned with the **PCAF Global Standard, Third Edition (2025)**.

---

## What it does

- **Loads** portfolio and emissions data from CSV with full PCAF (2025) schema validation
- **Attributes** emissions per holding using the correct PCAF (2025) §5 denominator for each asset class
- **Scores** data quality per PCAF's 1–5 DQ framework with uncertainty bounds
- **Computes** absolute financed emissions (tCO2e), WACI, and economic intensity
- **De-duplicates** Scope 3 via none, MSCI fixed (0.205), or portfolio-specific sector overlap
- **Stress tests** transition risk under three NGFS Phase 4 scenarios
- **Assesses** SBTi 1.5°C pathway alignment with implied temperature scoring
- **Exports** full results to JSON and formatted Excel (5 sheets)
- **Visualises** everything in a browser dashboard with per-holding drill-down and AI insights

---

## Project structure

```
ghg-portfolio-analyzer/
├── src/
│   ├── models.py                        # Domain objects — all 7 PCAF asset classes
│   ├── ingestion/loader.py              # CSV ingestion and PCAF denominator validation
│   ├── pcaf_engine/engine.py            # Attribution factors, WACI, intensity metrics
│   ├── deduplication/deduplication.py   # Scope 3 double-counting removal
│   ├── stress_testing/stress_testing.py # NGFS carbon price stress test
│   ├── stress_testing/pathway_alignment.py # SBTi 1.5°C alignment
│   └── reporting/report.py             # JSON and Excel report builder
├── data/sample/
│   ├── sample_portfolio.csv            # 18-holding example, all 7 PCAF asset classes
│   └── sample_emissions.csv            # Matching emissions records
├── reporting/dashboard.html            # Browser dashboard (open directly, no server)
├── reports/                            # Generated outputs (gitignored)
├── run_test.py
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

Each asset class uses a specific denominator per PCAF (2025) §5. The portfolio CSV must include the correct column(s) for each holding.

### Listed equity & corporate bonds (§5.1)

| Holding type | Denominator | CSV column |
|---|---|---|
| Listed company | EVIC (market cap ordinary + preferred + book debt + minorities; no cash deduction) | `evic_usd` |
| Bond to private company | Total equity + debt (balance sheet) | `total_equity_debt_usd` |

### Business loans & unlisted equity (§5.2)

| Borrower type | Denominator | CSV columns |
|---|---|---|
| Private company | Total equity + debt (balance sheet) | `total_equity_debt_usd`, `borrower_is_listed=false` |
| Listed company | EVIC | `evic_usd`, `borrower_is_listed=true` |

### Project finance (§5.3)

| Project type | Denominator | CSV columns |
|---|---|---|
| SPV / separate legal entity | Total project equity + debt | `project_equity_debt_usd`, `project_has_balance_sheet=true` |
| No separate balance sheet (e.g. LED retrofit) | Total project value at origination, frozen | `project_value_at_origination_usd`, `project_has_balance_sheet=false` |

### Commercial real estate (§5.4)

Property value at origination, frozen. Use latest known value if origination value is unavailable, then freeze it.

CSV column: `property_value_at_origination_usd`

### Mortgages (§5.5)

Residential property value at origination, frozen. Identical structure to CRE.

CSV column: `mortgage_property_value_at_origination_usd`

### Motor vehicle loans (§5.6)

Vehicle purchase price at origination (equity + debt at time of transaction), frozen.

CSV column: `vehicle_value_at_origination_usd`

If this column is blank, the engine applies **100% attribution** as the PCAF conservative fallback — meaning all of the vehicle's emissions are attributed to the lender.

### Sovereign debt (§5.9)

PPP-adjusted GDP in current international dollars from the IMF World Economic Outlook database.

CSV column: `ppp_adjusted_gdp_usd`

**How to get the value:** Go to [IMF WEO Data](https://www.imf.org/en/Publications/WEO/weo-database/), select "GDP based on PPP valuation of country GDP" (series code `PPPGDP`), choose the country and reporting year. The unit is billions of international dollars — multiply by 1,000,000,000 before entering in the CSV.

> **Important:** `government_revenue_usd` is retained in the CSV for the stress test module only (carbon cost as % of government revenue). It is **not** used for attribution. Using government revenue as the attribution denominator was acceptable under the 2022 standard but is not compliant with PCAF (2025) §5.9.

---

## Entity ID linkage

Holdings link to emissions records via `entity_id`. Resolution order:
1. `entity_id` column — explicit, required for private companies and sovereigns
2. `isin` column — fallback for publicly listed securities
3. `holding_id` — last resort

---

## Data quality scores

| Score | How produced | Error margin | Suitable for |
|---|---|---|---|
| 1 | Audited and verified | ±5–10% | Regulatory disclosure |
| 2 | Reported, unaudited | ±10–20% | Regulatory disclosure |
| 3 | Sector-average proxy | ±20–30% | Internal screening |
| 4 | Regional proxy | ±30–40% | Indicative only |
| 5 | Estimated | ±40–50% | Order of magnitude only |

---

## Known limitations

**Temperature score methodology**

In small portfolios the winsorised and no-cap temperature scores are often identical — when only a handful of holdings have revenue data, the 95th percentile of the overshoot distribution lands on the same extreme outlier. The scores diverge meaningfully in larger, more diversified portfolios.

**Sovereign debt pathway alignment**

Sovereign borrowers fall back to the default cross-sector benchmark in the pathway alignment module because no sovereign-specific SBTi pathway exists. Comparing government emissions intensity against a corporate revenue benchmark is methodologically weak — most frameworks exclude sovereign debt from temperature scoring. The stress test handles sovereigns separately by flagging that carbon cost as % of government revenue is not comparable to corporate holdings.

**Scope 3 for motor vehicles**

PCAF (2025) §5.6 requires only Scope 1 (fuel combustion) and Scope 2 (electricity for EVs) for motor vehicle loans. Scope 3 from vehicle production may optionally be reported as a lump sum in the initial financing year only. The engine includes whatever Scope 3 data is in the emissions CSV but does not enforce this production-year treatment.

---

## Standards references

- PCAF Global GHG Accounting and Reporting Standard, Third Edition (2025)
- GHG Protocol Corporate Value Chain (Scope 3) Standard
- GFANZ Transition Finance Metrics Framework (2023)
- SBTi Corporate Net-Zero Standard v1.1 (2021)
- IEA Net Zero by 2050 (2021)
- NGFS Phase 4 Climate Scenarios (2023)
- IMF World Economic Outlook Database (PPP-adjusted GDP)
