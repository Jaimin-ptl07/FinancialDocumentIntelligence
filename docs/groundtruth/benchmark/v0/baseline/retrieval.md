# Retrieval eval

- **benchmark_version**: `v0`
- **git_commit**: `594fa8483b8c5d264c3bf38073cc6cdfbd559ad2`
- **endpoint**: `http://localhost:9514`
- **mode**: `retrieval`
- **started_at**: `2026-11-26T14:56:05+00:00`
- **items run**: 86 of 96
- **skipped (no gold evidence)**: 10

Recall@K = items with at least one gold (doc_key, page) in the top K hits, over items run (a multi-Report item needs a gold page of every Report); MRR = mean 1/rank of the first gold hit, 0 when absent. A failed retrieval call counts against every column.

| kind | run | failed | recall@5 | recall@10 | MRR |
|---|---:|---:|---:|---:|---:|
| calculation | 10 | 0 | 20.0% | 40.0% | 0.074 |
| cross_company | 6 | 0 | 0.0% | 0.0% | 0.000 |
| fact_lookup | 59 | 0 | 13.6% | 40.7% | 0.115 |
| research | 11 | 0 | 54.5% | 72.7% | 0.437 |
| **overall** | 86 | 0 | 18.6% | 41.9% | 0.143 |

## No gold page in the top 10

| item | hits |
|---|---:|
| `calculation:TCS-FY2025-26-consolidated:effective_tax_rate` | 10 |
| `calculation:TCS-FY2025-26-consolidated:total_liabilities` | 10 |
| `calculation:TCS-FY2025-26-consolidated:current_ratio` | 10 |
| `calculation:TCS-FY2025-26-consolidated:total_lease_liabilities` | 10 |
| `calculation:REMC-FY2024-25:net_profit_margin` | 10 |
| `calculation:REMC-FY2024-25:effective_tax_rate` | 10 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_income` | 10 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_expenses` | 10 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:other_comprehensive_income` | 10 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:total_comprehensive_income` | 10 |
| `cross_company:TCS-RIL-FY2025-26-consolidated:basic_eps` | 10 |
| `cross_company:TCS-FY2025-26-REMC-FY2024-25:basic_eps` | 10 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:CT` | 10 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:DT` | 10 |
| `fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:TOTRNOTINR` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:CT` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:DT` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSB` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:EPSD` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSP` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:RFSS` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:SOP` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | 10 |
| `fact_lookup:RIL-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:INVT` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETDTA` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:NETGW` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-assets:TOTCAR` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTEQ` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTEQNR` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-equity:TOTOTEQ` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:NETDTL` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-liabilities:STTRP` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:CT` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:DT` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSB` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSD` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:OTCI` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTEXPR` | 10 |
| `fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:TOTRNOTINR` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:CT` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:DT` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSB` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:EPSD` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:OTCI` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTEXPR` | 10 |
| `fact_lookup:TCS-FY2025-26-standalone-profit_and_loss:TOTRNOTINR` | 10 |
| `research:TCS-FY2025-26-consolidated:other_income_note` | 10 |
| `research:REMC-FY2024-25-unknown:other_income_note` | 10 |
| `research:REMC-FY2024-25-unknown:revenue_from_operations_note` | 10 |
