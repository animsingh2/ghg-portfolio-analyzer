import warnings
from src.models import *
from src.ingestion.loader import load_portfolio_csv, load_emissions_csv
from src.pcaf_engine.engine import PCАFEngine, EngineConfig, print_portfolio_summary
from src.deduplication.deduplication import Scope3Deduplicator, DedupMethod, print_dedup_summary
from src.stress_testing.stress_testing import CarbonPriceStressTester, print_stress_summary
from src.stress_testing.pathway_alignment import (
    PathwayAlignmentAssessor, TemperatureScoreMethod, print_alignment_summary
)
from src.reporting.report import ReportBuilder

portfolio = load_portfolio_csv("data/sample/sample_portfolio.csv")
emissions = load_emissions_csv("data/sample/sample_emissions.csv")

print(f"Loaded {len(portfolio.holdings)} holdings")
print(f"Loaded {len(emissions)} emissions records")

# Module 2 — PCAF emissions engine
engine = PCАFEngine(config=EngineConfig())
result, holding_results = engine.run(portfolio, emissions)
print_portfolio_summary(result, holding_results)

# Module 3 — Scope 3 de-duplication
# Change DedupMethod to NONE or MSCI_FIXED to switch approaches
dedup = Scope3Deduplicator(method=DedupMethod.PORTFOLIO_SPECIFIC).run(
    result, holding_results, emissions, portfolio
)
print_dedup_summary(dedup)

# Module 4a — Carbon price stress test
stress = CarbonPriceStressTester().run_all_scenarios(
    portfolio, holding_results, emissions, horizon_year=2030
)
print_stress_summary(stress)

# Module 4b — SBTi pathway alignment
# Choose your primary temperature score method:
#   TemperatureScoreMethod.NO_CAP        — raw, outliers dominate
#   TemperatureScoreMethod.OVERSHOOT_CAP — MSCI standard, 10× cap
#   TemperatureScoreMethod.WINSORISE     — 95th percentile cap
alignment = PathwayAlignmentAssessor(
    temperature_method=TemperatureScoreMethod.OVERSHOOT_CAP,
    overshoot_cap=10.0,
).run(portfolio, holding_results, emissions, assessment_year=2022, target_year=2030)
print_alignment_summary(alignment)

builder = ReportBuilder(
    engine_result=result,
    holding_results=holding_results,
    portfolio=portfolio,
    dedup_result=dedup,
    stress_results=stress,
    alignment_result=alignment,
)

builder.to_json("reports/PORT_ALPHA_2022.json")
builder.to_excel("reports/PORT_ALPHA_2022.xlsx")